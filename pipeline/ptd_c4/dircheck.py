"""Check every NPI of the rate file on Health Net's Find a Provider site, as part of C4 (--check-directory).

After the rate file is read, C4 takes every NPI in its provider references (or the `--npis` subset),
searches each on findaprovider.healthnetcalifornia.com under the Group PPO network (ptd_dir.healthnet),
and uses what the site lists as directory evidence when it tags the references.

- Several browsers search side by side (`--check-workers`); each writes its own capture and coverage file
  (`<check-out>_w<k>.jsonl`, `<check-out>_w<k>.coverage.jsonl`).
- Every `--save-every` NPIs (default 50) the captures are turned into the directory table, the references
  are re-tagged with it and c4_ppo_check.csv is rewritten with every NPI checked so far. The rate columns
  of that table fill in at the end, when the rates are extracted.
- Stopping is safe: the next run skips every NPI already in a coverage file and carries on.
"""
from __future__ import annotations

import csv
import glob
import json
import multiprocessing as mp
import time
from pathlib import Path

from ptd_dir.parse import ROW_FIELDS, parse_record


def targets(con, npis: set | None = None) -> list[dict]:
    """Every NPI of the rate store, with its first TIN and that TIN's business name (for the name fallback)."""
    cols = [r[1] for r in con.execute("PRAGMA table_info(c4_ref_members)")]
    name = "tin_name" if "tin_name" in cols else "''"
    out = {}
    for npi, tin, tname in con.execute(f"SELECT npi, tin, {name} FROM c4_ref_members WHERE npi IS NOT NULL "
                                       f"ORDER BY npi, ({name} = ''), tin"):
        n = str(npi)
        if n not in out and (npis is None or n in npis):
            out[n] = {"npi": n, "tin": tin or "", "tin_name": tname or ""}
    return list(out.values())


def coverage_paths(base: Path) -> list[str]:
    return sorted(glob.glob(f"{base}_w*.coverage.jsonl"))


def checked_npis(paths: list[str]) -> set[str]:
    done = set()
    for p in paths:
        for line in Path(p).read_text(encoding="utf-8").splitlines():
            try:
                done.add(str(json.loads(line)["npi"]))
            except (json.JSONDecodeError, KeyError):
                continue
    return done


class CaptureRows:
    """Directory rows from the capture files, read incrementally (only lines added since the last call)."""

    def __init__(self, base: Path):
        self.base, self.offsets, self.seen, self.rows = base, {}, set(), []

    def update(self) -> list[dict]:
        for p in sorted(glob.glob(f"{self.base}_w*.jsonl")):
            if p.endswith(".coverage.jsonl"):
                continue
            with open(p, encoding="utf-8") as fh:
                fh.seek(self.offsets.get(p, 0))
                while True:
                    pos, line = fh.tell(), fh.readline()
                    if not line:
                        break
                    if not line.endswith("\n"):  # a worker is still writing this line
                        fh.seek(pos)
                        break
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    for r in parse_record(rec):
                        key = (r["npi"], r["name"].upper(), r["address_line1"].upper(), r["zip"], r["network_name"])
                        if key not in self.seen:
                            self.seen.add(key)
                            self.rows.append(r)
                self.offsets[p] = fh.tell()
        return self.rows


def write_table(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=ROW_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(r for r in rows if r.get("npi"))


def _worker(shard: list[dict], out: str, kw: dict) -> None:
    from ptd_dir.healthnet import check

    check(shard, Path(out), log=lambda m: print(m, flush=True), **kw)


MP_CONTEXT = "spawn"  # a fresh interpreter per browser (Playwright is not fork-safe)
POLL_SECONDS = 10.0


def _worker_entry(shard: list[dict], out: str, kw: dict) -> None:
    _worker(shard, out, kw)  # looked up at call time, so tests can swap in a fake site


def run(todo: list[dict], base: Path, workers: int, save_every: int, checkpoint, log=print, poll: float | None = None,
        **kw) -> int:
    """Search `todo` with `workers` browsers; call `checkpoint()` after every `save_every` NPIs and at the end.
    Returns how many NPIs the coverage files hold when done."""
    base.parent.mkdir(parents=True, exist_ok=True)
    workers = max(1, min(workers, len(todo) or 1))
    start = len(checked_npis(coverage_paths(base)))
    procs = []
    if todo:
        ctx = mp.get_context(MP_CONTEXT)
        for k in range(workers):
            shard = todo[k::workers]
            if shard:
                p = ctx.Process(target=_worker_entry, args=(shard, f"{base}_w{k}.jsonl", kw), daemon=True)
                p.start()
                procs.append(p)
    mark = start + save_every
    try:
        while any(p.is_alive() for p in procs):
            time.sleep(poll or POLL_SECONDS)
            n = len(checked_npis(coverage_paths(base)))
            if n >= mark:
                log(f"[c4]   directory check: {n - start:,} of {len(todo):,} NPIs searched; saving")
                checkpoint()
                while mark <= n:
                    mark += save_every
    except KeyboardInterrupt:
        log("[c4]   directory check stopped; rerun the same command to carry on")
        for p in procs:
            p.terminate()
        raise
    for p in procs:
        p.join()
    n = len(checked_npis(coverage_paths(base)))
    checkpoint()
    return n
