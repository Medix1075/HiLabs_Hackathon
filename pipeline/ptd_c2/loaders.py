"""Load the C1 table and the raw index files (for the out-of-network file URLs C1 dropped)."""
from __future__ import annotations

import csv
import json
import re
from collections import defaultdict
from pathlib import Path

import ijson

from .normalize import norm_plan_id, norm_plan_name, norm_url
from .stream import open_stream

# Column aliases, compared after lowercasing and dropping spaces/underscores/hyphens.
C1_ALIASES = {
    "brand": ["brand", "brandname", "issuer", "source", "indexbrand"],
    "plan_name": ["planname", "plan"],
    "plan_id": ["planid"],
    "market_type": ["markettype", "planmarkettype", "market"],
    "rate_file_url": ["ratefileurl", "ratefile", "innetworkfileurl", "innetworkurl", "fileurl", "url", "location"],
    # optional
    "plan_id_type": ["planidtype"],
    "issuer_name": ["issuername"],
    "plan_sponsor_name": ["plansponsorname", "sponsor"],
    "oon_file_url": ["allowedamountfile", "allowedamountfileurl", "oonfileurl", "outofnetworkfileurl"],
}
REQUIRED = ["brand", "plan_name", "plan_id", "market_type", "rate_file_url"]


def _key(header: str) -> str:
    return re.sub(r"[\s_\-]", "", str(header or "").lower())


def _map_headers(headers: list[str]) -> dict[str, str]:
    lookup = {_key(h): h for h in headers if h is not None}
    mapping = {}
    for field, aliases in C1_ALIASES.items():
        for alias in aliases:
            if alias in lookup:
                mapping[field] = lookup[alias]
                break
    missing = [f for f in REQUIRED if f not in mapping]
    if missing:
        raise ValueError(f"C1 table is missing columns {missing}; found headers {headers}")
    return mapping


def _read_records(path: Path, sheet: str | None) -> list[dict]:
    suffix = path.suffix.lower()
    if suffix in (".csv", ".tsv"):
        with open(path, newline="", encoding="utf-8-sig") as fh:
            return list(csv.DictReader(fh, delimiter="\t" if suffix == ".tsv" else ","))
    if suffix in (".xlsx", ".xlsm"):
        import openpyxl

        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb[sheet] if sheet else wb.worksheets[0]
        rows = ws.iter_rows(values_only=True)
        headers = [str(h) if h is not None else None for h in next(rows)]
        return [dict(zip(headers, r)) for r in rows if any(v is not None for v in r)]
    if suffix == ".jsonl":
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]
    if suffix == ".json":
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else data.get("rows", [])
    raise ValueError(f"Unsupported C1 file type: {path}")


def load_c1(path: str | Path, sheet: str | None = None) -> tuple[list[dict], dict]:
    """Returns (rows, stats). Each row has the canonical fields plus `row_id` and normalised keys.

    Exact duplicates of (brand, normalised plan name, plan id, rate file URL) are dropped and counted.
    """
    path = Path(path)
    records = _read_records(path, sheet)
    if not records:
        raise ValueError(f"C1 table {path} is empty")
    mapping = _map_headers(list(records[0].keys()))
    rows, seen, dupes = [], set(), 0
    for rec in records:
        row = {field: (rec.get(src) if rec.get(src) is not None else "") for field, src in mapping.items()}
        row = {k: (str(v).strip() if not isinstance(v, str) else v.strip()) for k, v in row.items()}
        row["plan_id"] = norm_plan_id(row["plan_id"])
        row["rate_file_url"] = norm_url(row["rate_file_url"])
        row["plan_name_norm"] = norm_plan_name(row["plan_name"])
        key = (row["brand"].lower(), row["plan_name_norm"], row["plan_id"], row["rate_file_url"])
        if key in seen:
            dupes += 1
            continue
        seen.add(key)
        row["row_id"] = len(rows) + 1
        rows.append(row)
    return rows, {"input_rows": len(records), "duplicates_dropped": dupes, "rows": len(rows),
                  "column_mapping": mapping}


def _brand_hint(src: str) -> str:
    name = src.rsplit("/", 1)[-1].lower()
    m = re.search(r"_([a-z0-9\-]+?)_index", name)
    return m.group(1).split("-")[0] if m else ""


def load_oon_map(index_sources: list[str], probe_cfg: dict) -> tuple[dict, list[dict]]:
    """Stream each raw index file and map plans to their allowed_amount_file URL.

    Returns ({"by_plan_file": {(name, id, url): {oon...}}, "by_plan": {(name, id): {oon...}}}, log rows).
    """
    by_plan_file: dict[tuple, set] = defaultdict(set)
    by_plan: dict[tuple, set] = defaultdict(set)
    log = []
    for src in index_sources:
        n_struct = n_plans = n_with_oon = 0
        status = "ok"
        try:
            with open_stream(src, timeout=probe_cfg["timeout_seconds"], retries=probe_cfg["retries"],
                             user_agent=probe_cfg["user_agent"]) as (fh, _):
                for struct in ijson.items(fh, "reporting_structure.item"):
                    n_struct += 1
                    aaf = struct.get("allowed_amount_file") or {}
                    oon = norm_url(aaf.get("location") if isinstance(aaf, dict) else None)
                    files = [norm_url(f.get("location")) for f in struct.get("in_network_files") or []]
                    for plan in struct.get("reporting_plans") or []:
                        n_plans += 1
                        name = norm_plan_name(plan.get("plan_name"))
                        pid = norm_plan_id(plan.get("plan_id"))
                        if oon:
                            n_with_oon += 1
                        by_plan[(name, pid)].add(oon)
                        for url in files:
                            by_plan_file[(name, pid, url)].add(oon)
        except Exception as exc:  # keep going; the supporting signal is optional
            status = f"error: {exc.__class__.__name__}: {exc}"
        log.append({"index_source": src, "brand_hint": _brand_hint(src), "status": status,
                    "reporting_structures": n_struct, "plans": n_plans, "plans_with_oon_file": n_with_oon})
    return {"by_plan_file": by_plan_file, "by_plan": by_plan}, log


def discover_index_sources(paths: list[str]) -> list[str]:
    """Expand directories into their *.json / *.json.gz files; keep URLs as given."""
    out = []
    for p in paths:
        pp = Path(p)
        if pp.is_dir():
            out += sorted(str(f) for f in pp.iterdir() if f.name.endswith((".json", ".json.gz")))
        else:
            out.append(p)
    return out
