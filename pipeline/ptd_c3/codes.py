"""Expected billing codes (the HiLabs specialty -> billing code Excel), for code coverage.

The organisers have not sent this file yet. Until they do, code coverage is "not measured"
and its weight is spread over the other metrics. Any CSV/XLSX with a code column works;
a specialty column, if present, adds coverage per specialty.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

CODE_ALIASES = ["billingcode", "code", "cptcode", "cpt", "hcpcs", "hcpcscode", "procedurecode", "cpthcpcs"]
SPECIALTY_ALIASES = ["specialty", "speciality", "specialtyname", "taxonomy", "providerspecialty"]


def _key(h) -> str:
    return re.sub(r"[\s_\-/]", "", str(h or "").lower())


def norm_code(code) -> str:
    s = str(code or "").strip().upper()
    return s[:-2] if re.fullmatch(r"\d+\.0", s) else s


def load_expected_codes(path: str | Path, sheet: str | None = None) -> dict[str, set]:
    """Returns {specialty or "(all)": set of codes}; "(all)" always holds every code."""
    path = Path(path)
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        import openpyxl

        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb[sheet] if sheet else wb.worksheets[0]
        it = ws.iter_rows(values_only=True)
        headers = list(next(it))
        records = [dict(zip(headers, r)) for r in it]
    else:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            records = list(csv.DictReader(fh))
    if not records:
        raise ValueError(f"expected-code file {path} is empty")
    cols = {_key(h): h for h in records[0] if h is not None}
    code_col = next((cols[a] for a in CODE_ALIASES if a in cols), None)
    if code_col is None:
        raise ValueError(f"expected-code file {path} has no billing code column; headers {list(records[0])}")
    spec_col = next((cols[a] for a in SPECIALTY_ALIASES if a in cols), None)
    out: dict[str, set] = {"(all)": set()}
    for rec in records:
        code = norm_code(rec.get(code_col))
        if not code:
            continue
        out["(all)"].add(code)
        if spec_col and rec.get(spec_col):
            out.setdefault(str(rec[spec_col]).strip(), set()).add(code)
    return out
