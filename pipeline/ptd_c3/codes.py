"""Expected billing codes, for the code coverage metric.

The main source is the HiLabs workbook "Rate Benchmarking Individual Weights" (sheets
"Spec-Proc Weights" and "Specialty Weights"). From it, each code gets a weight:

    weight(code) = sum over specialties S of  specialty_weight(S) * proc_weight(S, code) / sum of proc_weight(S, *)

so each specialty contributes its own volume, spread over its codes by their volume. Coverage is
then reported both as a plain share of codes present and as a weighted share, and the weighted
share is the score: missing a top Primary Care code costs more than missing a rare one.
Specialties with no specialty weight (in the 2026 workbook: six facility types such as Skilled
Nursing and Urgent Care, since the weights are for individual providers) count in the plain
share and the per-specialty rows but not in the weighted share.

Any other CSV/XLSX with a code column also works: then every code weighs the same.
"""
from __future__ import annotations

import csv
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

CODE_ALIASES = ["proccd", "billingcode", "code", "cptcode", "cpt", "hcpcs", "hcpcscode", "procedurecode", "cpthcpcs"]
SPEC_CODE_ALIASES = ["speccd", "speccode", "specialtycode"]
SPEC_NAME_ALIASES = ["specdescription", "specdescr", "specialty", "speciality", "specialtyname", "providerspecialty"]
PROC_WEIGHT_ALIASES = ["specprocweight", "procweight", "codeweight", "weight"]
SPEC_WEIGHT_ALIASES = ["specialtyweight", "specweight"]


def _key(h) -> str:
    return re.sub(r"[\s_\-/]", "", str(h or "").lower())


def norm_code(code) -> str:
    s = str(code or "").strip().upper()
    return s[:-2] if re.fullmatch(r"\d+\.0", s) else s


def _pick(cols: dict, aliases: list[str]):
    return next((cols[a] for a in aliases if a in cols), None)


@dataclass
class ExpectedCodes:
    codes: set = field(default_factory=set)
    by_specialty: dict = field(default_factory=dict)        # specialty label -> set of codes
    weights: dict = field(default_factory=dict)             # code -> weight (empty = unweighted)
    spec_weights: dict = field(default_factory=dict)        # specialty label -> {code: weight}
    unweighted_specialties: list = field(default_factory=list)
    source: str = ""

    def weighted_coverage(self, present: set, spec: str | None = None) -> float | None:
        w = self.spec_weights.get(spec, {}) if spec else self.weights
        total = sum(w.values())
        return sum(v for c, v in w.items() if c in present) / total if total else None


def _sheets(path: Path, sheet: str | None) -> list[tuple[str, list[dict]]]:
    is_xlsx = path.suffix.lower() in (".xlsx", ".xlsm") or zipfile.is_zipfile(path)
    if not is_xlsx:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            return [(path.name, list(csv.DictReader(fh)))]
    import openpyxl

    with open(path, "rb") as fh:  # file object, so files saved without an .xlsx extension also load
        wb = openpyxl.load_workbook(fh, read_only=True, data_only=True)
        out = []
        for ws in ([wb[sheet]] if sheet else wb.worksheets):
            it = ws.iter_rows(values_only=True)
            headers = list(next(it, []))
            out.append((ws.title, [dict(zip(headers, r)) for r in it if any(v is not None for v in r)]))
    return out


def load_expected_codes(path: str | Path, sheet: str | None = None) -> ExpectedCodes:
    path = Path(path)
    sheets = _sheets(path, sheet)
    code_sheet = spec_sheet = None
    for name, recs in sheets:
        cols = {_key(h): h for h in (recs[0] if recs else {}) if h is not None}
        if code_sheet is None and _pick(cols, CODE_ALIASES) and not _key(name).startswith("taxonomy"):
            code_sheet = (name, recs, cols)
        if spec_sheet is None and _pick(cols, SPEC_WEIGHT_ALIASES):
            spec_sheet = (name, recs, cols)
    if code_sheet is None:
        raise ValueError(f"expected-code file {path} has no sheet with a billing code column")
    name, recs, cols = code_sheet
    code_col = _pick(cols, CODE_ALIASES)
    spec_code_col, spec_name_col = _pick(cols, SPEC_CODE_ALIASES), _pick(cols, SPEC_NAME_ALIASES)
    pw_col = _pick({k: v for k, v in cols.items() if v not in (code_col,)}, PROC_WEIGHT_ALIASES)

    spec_weight: dict[str, float] = {}
    if spec_sheet:
        _, srecs, scols = spec_sheet
        sc, sw = _pick(scols, SPEC_CODE_ALIASES), _pick(scols, SPEC_WEIGHT_ALIASES)
        for r in srecs:
            if sc and r.get(sc) is not None and isinstance(r.get(sw), (int, float)):
                spec_weight[str(r[sc]).strip()] = float(r[sw])

    exp = ExpectedCodes(source=f"{path.name} [{name}]")
    proc_w: dict[str, dict[str, float]] = {}
    spec_key_to_label: dict[str, str] = {}
    for rec in recs:
        code = norm_code(rec.get(code_col))
        if not code:
            continue
        exp.codes.add(code)
        skey = str(rec.get(spec_code_col) or rec.get(spec_name_col) or "").strip() if (spec_code_col or spec_name_col) else ""
        if skey:
            label = f"{skey} {str(rec.get(spec_name_col) or '').strip()}".strip() if spec_code_col else skey
            spec_key_to_label[skey] = label
            exp.by_specialty.setdefault(label, set()).add(code)
            w = rec.get(pw_col) if pw_col else None
            proc_w.setdefault(skey, {})[code] = float(w) if isinstance(w, (int, float)) else 1.0

    if pw_col and spec_weight:
        for skey, codes in proc_w.items():
            label = spec_key_to_label[skey]
            total = sum(codes.values())
            exp.spec_weights[label] = dict(codes)
            if skey not in spec_weight:
                exp.unweighted_specialties.append(label)
                continue
            if total <= 0:
                continue
            for c, w in codes.items():
                exp.weights[c] = exp.weights.get(c, 0.0) + spec_weight[skey] * w / total
    elif pw_col:  # code weights but no specialty weights: sum them
        for skey, codes in proc_w.items():
            exp.spec_weights[spec_key_to_label[skey]] = dict(codes)
            for c, w in codes.items():
                exp.weights[c] = exp.weights.get(c, 0.0) + w
    return exp
