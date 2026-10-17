"""Stream a rate file into the normalised rate store, in parallel.

1. A remote file is downloaded once into the cache (rate files are read more than once).
2. One process reads provider_references (the provider groups: NPIs and a TIN per group).
3. N processes read in_network at the same time. Each parses the whole stream with ijson's C
   backend, but only builds rows for every Nth item, so the Python work is split N ways.
4. Each process writes its own SQLite shard (no locking); the shards are merged at the end.

Nothing is loaded whole: ijson yields one provider reference or one in_network item at a time.
"""
from __future__ import annotations

import json
import multiprocessing
import os
import sqlite3
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

import ijson
import requests

from ptd_c2.stream import is_remote, open_stream
from ptd_c3.codes import norm_code
from ptd_c3.scan import npi_is_valid

from .db import INDEXES, connect, init_c4, stable_id

TOP_FIELDS = ("reporting_entity_name", "reporting_entity_type", "last_updated_on", "version")


# ------------------------------------------------------------------ download cache
def cache_file(url: str, sc: dict, log=print) -> str:
    """Return a local path for the rate file, downloading it once if it is remote."""
    if not is_remote(url):
        return urlparse(url).path if url.startswith("file:") else url
    cache = Path(sc["cache_dir"])
    cache.mkdir(parents=True, exist_ok=True)
    name = Path(urlparse(url).path).name or f"rate_file_{stable_id(url) & 0xffffffff}.json"
    dest = cache / name
    headers = {"User-Agent": sc["user_agent"]}
    if dest.exists() and sc.get("reuse_cache", True):
        try:
            head = requests.head(url, timeout=30, headers=headers, allow_redirects=True)
            size = int(head.headers.get("Content-Length", -1))
        except requests.RequestException:
            size = -1
        if size in (-1, dest.stat().st_size):
            log(f"[c4]   using cached copy {dest}")
            return str(dest)
    part = dest.with_suffix(dest.suffix + ".part")
    last = None
    for attempt in range(sc["retries"]):
        try:
            t0 = time.time()
            with requests.get(url, stream=True, timeout=sc["timeout_seconds"], headers=headers) as resp:
                resp.raise_for_status()
                with open(part, "wb") as fh:
                    for chunk in resp.iter_content(1 << 20):
                        fh.write(chunk)
            os.replace(part, dest)
            log(f"[c4]   downloaded {dest.stat().st_size:,} bytes in {time.time() - t0:.1f}s -> {dest}")
            return str(dest)
        except requests.HTTPError:
            raise
        except requests.RequestException as exc:
            last = exc
            time.sleep(2 ** attempt)
    raise last


# ------------------------------------------------------------------ shard writer
class Shard:
    def __init__(self, path: Path, batch: int):
        self.path, self.batch = path, batch
        if path.exists():
            path.unlink()
        self.con = connect(path)
        init_c4(self.con)
        self.con.execute("CREATE TABLE c4_refset_stats_raw (file_id INTEGER, refset_id INTEGER, size INTEGER, "
                         "rows INTEGER, capitation_rows INTEGER)")
        self.buf: dict[str, list] = {}

    SQL = {
        "refs": "INSERT OR IGNORE INTO c4_refs (file_id, ref_id, network_name, n_groups, source) VALUES (?,?,?,?,?)",
        "members": "INSERT INTO c4_ref_members VALUES (?,?,?,?,?)",
        "refset": "INSERT OR IGNORE INTO c4_refset_members VALUES (?,?,?)",
        "stats": "INSERT INTO c4_refset_stats_raw VALUES (?,?,?,?,?)",
        "codes": "INSERT OR IGNORE INTO c4_codes VALUES (?,?,?)",
        "prices": "INSERT INTO c4_prices VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
    }

    def add(self, kind: str, row: tuple) -> None:
        b = self.buf.setdefault(kind, [])
        b.append(row)
        if len(b) >= self.batch:
            self.flush(kind)

    def flush(self, kind: str | None = None) -> None:
        for k in ([kind] if kind else list(self.buf)):
            if self.buf.get(k):
                self.con.executemany(self.SQL[k], self.buf[k])
                self.buf[k] = []
        self.con.commit()

    def close(self) -> None:
        self.flush()
        self.con.close()


def _group_rows(file_id: int, ref_id: str, groups) -> list[tuple]:
    """(file_id, ref_id, npi, tin, tin_type) for each NPI of each group; npi None for TIN-only groups."""
    rows = []
    for g in groups or []:
        if not isinstance(g, dict):
            continue
        tin = g.get("tin") if isinstance(g.get("tin"), dict) else {}
        tin_value = str(tin.get("value") or "").strip()
        tin_type = str(tin.get("type") or "").lower()
        npis = g.get("npi") if isinstance(g.get("npi"), list) else [g.get("npi")]
        valid = [int(n) for n in npis if n not in (None, "", 0, "0") and npi_is_valid(n)]
        if not valid:
            rows.append((file_id, ref_id, None, tin_value, tin_type))
        for n in dict.fromkeys(valid):
            rows.append((file_id, ref_id, n, tin_value, tin_type))
    return rows


def _network_name(ref: dict) -> str:
    nn = ref.get("network_name")
    names = nn if isinstance(nn, list) else [nn]
    return "|".join(str(n).strip() for n in names if str(n or "").strip())


# ------------------------------------------------------------------ pass A: provider references
def read_refs(file_id: int, path: str, shard_path: str, sc: dict) -> dict:
    t0 = time.time()
    shard = Shard(Path(shard_path), sc["batch_rows"])
    top, n_refs, n_rows, fetched, skipped = {}, 0, 0, 0, 0
    with open_stream(path) as (fh, _):
        for prefix, event, value in ijson.parse(fh):
            if prefix in TOP_FIELDS and event in ("string", "number"):
                top[prefix] = value
            elif event == "start_array" and prefix in ("provider_references", "in_network"):
                break
    with open_stream(path) as (fh, _):
        for ref in ijson.items(fh, "provider_references.item"):
            ref_id = str(ref.get("provider_group_id"))
            groups, source = ref.get("provider_groups"), "inline"
            if groups is None and ref.get("location"):
                source = "location"
                if fetched < sc["max_location_fetches"]:
                    fetched += 1
                    try:
                        with open_stream(ref["location"], timeout=sc["timeout_seconds"],
                                         user_agent=sc["user_agent"]) as (lf, _):
                            groups = list(ijson.items(lf, "provider_groups.item"))
                    except Exception:  # an unreachable location leaves the reference empty, and is counted
                        groups = None
                else:
                    skipped += 1
            rows = _group_rows(file_id, ref_id, groups)
            shard.add("refs", (file_id, ref_id, _network_name(ref), len(groups or []), source))
            for r in rows:
                shard.add("members", r)
            n_refs += 1
            n_rows += len(rows)
    shard.close()
    return {"top": top, "refs": n_refs, "member_rows": n_rows, "location_fetched": fetched,
            "location_skipped": skipped, "seconds": round(time.time() - t0, 2)}


# ------------------------------------------------------------------ pass B: in_network (sharded)
def read_items(file_id: int, path: str, shard_path: str, k: int, n: int, codes: list | None, sc: dict,
               max_items: int | None = None) -> dict:
    t0 = time.time()
    shard = Shard(Path(shard_path), sc["batch_rows"])
    code_set = set(codes) if codes is not None else None
    seen_refsets: set[int] = set()
    stats: dict[int, list] = {}
    items = in_scope = rows = 0
    progress = sc.get("progress_every_items") or 0
    with open_stream(path) as (fh, _):
        for i, item in enumerate(ijson.items(fh, "in_network.item", use_float=True)):
            if max_items is not None and i >= max_items:
                break
            if i % n != k:
                continue
            items += 1
            if progress and k == 0 and i and i % progress < n:
                print(f"[c4]   ~{i:,} in_network items read ... {Path(path).name}", flush=True)
            code_type = str(item.get("billing_code_type") or "").upper()
            code = norm_code(item.get("billing_code"))
            arrangement = str(item.get("negotiation_arrangement") or "").lower()
            keep = code_set is None or code in code_set
            if keep:
                in_scope += 1
                shard.add("codes", (code_type, code, str(item.get("description") or "")[:300]))
            for nr in item.get("negotiated_rates") or []:
                if not isinstance(nr, dict):
                    continue
                refs = [str(r) for r in (nr.get("provider_references") or [])]
                inline = nr.get("provider_groups")
                if inline:
                    ref_id = f"inline:{stable_id(json.dumps(inline, sort_keys=True, default=str))}"
                    shard.add("refs", (file_id, ref_id, "", len(inline), "inline_rate"))
                    for r in _group_rows(file_id, ref_id, inline):
                        shard.add("members", r)
                    refs.append(ref_id)
                if not refs:
                    continue
                refs = sorted(set(refs))
                rsid = stable_id(*refs)
                if rsid not in seen_refsets:
                    seen_refsets.add(rsid)
                    for r in refs:
                        shard.add("refset", (file_id, rsid, r))
                prices = [p for p in (nr.get("negotiated_prices") or []) if isinstance(p, dict)]
                st = stats.setdefault(rsid, [len(refs), 0, 0])
                st[1] += len(prices)
                if arrangement == "capitation":
                    st[2] += len(prices)
                if not keep:
                    continue
                for p in prices:
                    rate = p.get("negotiated_rate")
                    try:
                        rate = float(rate) if rate is not None else None
                    except (TypeError, ValueError):
                        rate = None
                    shard.add("prices", (
                        file_id, rsid, code_type, code, arrangement,
                        str(p.get("negotiated_type") or "").lower(), rate, str(p.get("expiration_date") or ""),
                        str(p.get("billing_class") or "").lower(), str(p.get("setting") or "").lower(),
                        ",".join(sorted(str(s) for s in (p.get("service_code") or []))),
                        ",".join(sorted(str(m) for m in (p.get("billing_code_modifier") or []))),
                    ))
                    rows += 1
    for rsid, (size, r, cap) in stats.items():
        shard.add("stats", (file_id, rsid, size, r, cap))
    shard.close()
    return {"shard": k, "items": items, "items_in_scope": in_scope, "price_rows": rows,
            "refsets": len(seen_refsets), "seconds": round(time.time() - t0, 2)}


# ------------------------------------------------------------------ merge
def merge_shards(con: sqlite3.Connection, shard_paths: list[Path]) -> None:
    con.execute("CREATE TEMP TABLE IF NOT EXISTS stats_raw (file_id INTEGER, refset_id INTEGER, size INTEGER, "
                "rows INTEGER, capitation_rows INTEGER)")
    for sp in shard_paths:
        con.execute("ATTACH DATABASE ? AS s", (str(sp),))
        con.execute("INSERT OR IGNORE INTO c4_refs (file_id, ref_id, network_name, n_groups, source) "
                    "SELECT file_id, ref_id, network_name, n_groups, source FROM s.c4_refs")
        for t in ("c4_refset_members", "c4_codes"):
            con.execute(f"INSERT OR IGNORE INTO {t} SELECT * FROM s.{t}")
        con.execute("INSERT INTO c4_prices SELECT * FROM s.c4_prices")
        con.execute("INSERT INTO c4_ref_members SELECT DISTINCT * FROM s.c4_ref_members")
        con.execute("INSERT INTO temp.stats_raw SELECT * FROM s.c4_refset_stats_raw")
        con.commit()
        con.execute("DETACH DATABASE s")
        sp.unlink()
    con.execute("INSERT INTO c4_refset_stats SELECT file_id, refset_id, MAX(size), SUM(rows), SUM(capitation_rows) "
                "FROM temp.stats_raw GROUP BY file_id, refset_id")
    con.execute("DROP TABLE temp.stats_raw")
    # inline groups can be seen by several shards: keep one copy of each member row
    con.execute("DELETE FROM c4_ref_members WHERE rowid NOT IN (SELECT MIN(rowid) FROM c4_ref_members "
                "GROUP BY file_id, ref_id, npi, tin, tin_type)")
    con.executescript(INDEXES)
    con.execute("ANALYZE")
    con.commit()


def ingest_file(con: sqlite3.Connection, db_path: Path, file_id: int, url: str, cfg: dict, codes: list | None,
                log=print, max_items: int | None = None) -> dict:
    sc = cfg["stream"]
    t0 = time.time()
    path = cache_file(url, sc, log)
    workers = sc.get("workers") or max(1, (os.cpu_count() or 2) - 1)
    work = db_path.parent / f".c4_work_{file_id}"
    work.mkdir(parents=True, exist_ok=True)
    ref_shard = work / "refs.sqlite"
    item_shards = [work / f"items_{k}.sqlite" for k in range(workers)]
    log(f"[c4]   reading {Path(path).name} with {workers} in_network worker(s) + 1 reference reader")
    # spawn (not fork): the same behaviour on Linux, macOS and Windows, and safe in threaded parents
    with ProcessPoolExecutor(max_workers=workers + 1, mp_context=multiprocessing.get_context("spawn")) as pool:
        f_refs = pool.submit(read_refs, file_id, path, str(ref_shard), sc)
        f_items = [pool.submit(read_items, file_id, path, str(item_shards[k]), k, workers, codes, sc, max_items)
                   for k in range(workers)]
        refs_info = f_refs.result()
        items_info = [f.result() for f in f_items]
    merge_shards(con, [ref_shard] + item_shards)
    try:
        work.rmdir()
    except OSError:
        pass
    info = {
        "local_path": path, "top": refs_info["top"], "refs": refs_info["refs"],
        "location_fetched": refs_info["location_fetched"], "location_skipped": refs_info["location_skipped"],
        "items": sum(i["items"] for i in items_info), "items_in_scope": sum(i["items_in_scope"] for i in items_info),
        "price_rows": sum(i["price_rows"] for i in items_info), "workers": workers,
        "seconds": round(time.time() - t0, 2),
    }
    return info
