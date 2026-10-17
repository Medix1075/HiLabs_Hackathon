"""Specialty for every NPI, and whether each billed code fits that specialty.

Order of evidence (first that answers wins, and the source is recorded):

  1. taxonomy_primary    NPPES primary taxonomy -> HiLabs "Taxonomy-Spec Xwalk"
  2. taxonomy_secondary  any other NPPES taxonomy of the NPI -> the same crosswalk
  3. medicare_specialty  CMS Doctors & Clinicians primary specialty, matched to a HiLabs specialty name
  4. inferred_from_codes the HiLabs specialty whose code profile ("Spec-Proc Weights") best matches the
                         codes the provider has rates for (see infer_from_codes)
  5. nucc                NUCC classification of the primary taxonomy (organisations and specialties the
                         crosswalk does not cover, e.g. hospitals, labs, group practices)
  6. Unknown

The crosswalk and code profiles are for individual providers, so for Type 2 NPIs step 4 is skipped.
"""
from __future__ import annotations

import csv
import re
from collections import defaultdict
from pathlib import Path


def _tokens(s) -> frozenset:
    stop = {"and", "of", "the", "medicine", "general", "provider", "physician", "specialist"}
    return frozenset(w for w in re.sub(r"[^a-z0-9 ]", " ", str(s or "").lower()).split() if w not in stop)


class Specialties:
    def __init__(self):
        self.by_taxonomy: dict[str, tuple[str, str]] = {}     # taxonomy -> (spec_cd, spec name)
        self.names: dict[str, str] = {}                        # spec_cd -> name
        self.codes: dict[str, dict[str, float]] = defaultdict(dict)  # spec_cd -> {code: weight}
        self.spec_weight: dict[str, float] = {}
        self.nucc: dict[str, str] = {}                         # taxonomy -> NUCC display name

    @property
    def loaded(self) -> bool:
        return bool(self.by_taxonomy or self.codes)

    def code_expected(self, spec_cd: str, code: str) -> bool | None:
        return None if spec_cd not in self.codes else code in self.codes[spec_cd]

    def match_name(self, name: str) -> str | None:
        """A free-text specialty (e.g. Medicare's 'CARDIOLOGY') -> HiLabs spec code, by token match."""
        t = _tokens(name)
        if not t:
            return None
        best, best_score = None, 0.0
        for cd, n in self.names.items():
            u = _tokens(n)
            if not u:
                continue
            score = len(t & u) / len(t | u)
            if score > best_score:
                best, best_score = cd, score
        return best if best_score >= 0.5 else None

    def infer_from_codes(self, codes: set[str], min_codes: int = 3, min_f: float = 0.25):
        """Best-matching specialty for a set of billed codes.

        coverage  = share of the specialty's code volume (SPEC_PROC_WEIGHT) the provider has rates for
        precision = share of the provider's in-scope codes that belong to the specialty
        F         = harmonic mean of the two. The specialty with the highest F wins if F >= min_f.
        """
        known = {c for c in codes if any(c in w for w in self.codes.values())}
        if len(known) < min_codes:
            return None
        best = None
        for cd, w in self.codes.items():
            total = sum(w.values())
            hit = known & w.keys()
            if not hit or not total:
                continue
            cov = sum(w[c] for c in hit) / total
            prec = len(hit) / len(known)
            f = 2 * cov * prec / (cov + prec)
            if best is None or f > best[1]:
                best = (cd, f, cov, prec)
        if best and best[1] >= min_f:
            return best
        return None


def load_specialties(workbook: str | Path | None, nucc: str | Path | None = None) -> Specialties:
    sp = Specialties()
    if workbook:
        import openpyxl

        with open(workbook, "rb") as fh:
            wb = openpyxl.load_workbook(fh, read_only=True, data_only=True)
            for ws in wb.worksheets:
                it = ws.iter_rows(values_only=True)
                head = [re.sub(r"[^a-z]", "", str(h or "").lower()) for h in next(it, [])]
                rows = [dict(zip(head, r)) for r in it if any(v is not None for v in r)]
                if "taxonomycode" in head:
                    for r in rows:
                        tax = str(r.get("taxonomycode") or "").strip().upper()
                        if tax and tax not in sp.by_taxonomy:
                            sp.by_taxonomy[tax] = (str(r.get("speccd")).strip(), str(r.get("specdescr") or "").strip())
                            sp.names.setdefault(str(r.get("speccd")).strip(), str(r.get("specdescr") or "").strip())
                elif "proccd" in head:
                    for r in rows:
                        cd = str(r.get("speccd")).strip()
                        code = str(r.get("proccd") or "").strip().upper()
                        w = r.get("specprocweight")
                        if code:
                            sp.codes[cd][code] = float(w) if isinstance(w, (int, float)) else 1.0
                            sp.names.setdefault(cd, str(r.get("specdescription") or "").strip())
                elif "specialtyweight" in head:
                    for r in rows:
                        if isinstance(r.get("specialtyweight"), (int, float)):
                            sp.spec_weight[str(r.get("speccode")).strip()] = float(r["specialtyweight"])
                            sp.names.setdefault(str(r.get("speccode")).strip(), str(r.get("specialty") or "").strip())
    if nucc:
        with open(nucc, newline="", encoding="utf-8-sig", errors="replace") as fh:
            for r in csv.DictReader(fh):
                k = {re.sub(r"[^a-z]", "", key.lower()): v for key, v in r.items() if key}
                code = (k.get("code") or "").strip().upper()
                name = (k.get("displayname") or " - ".join(x for x in (k.get("classification"), k.get("specialization"))
                                                           if x)).strip()
                if code:
                    sp.nucc[code] = name
    return sp


def assign(npi_type: str, primary: str, taxonomies: list[str], dac_spec: str, codes: set[str],
           sp: Specialties) -> dict:
    """Returns {spec_cd, specialty, specialty_source, specialty_note}."""
    def out(cd, name, src, note=""):
        return {"spec_cd": cd or "", "specialty": name or "Unknown", "specialty_source": src, "specialty_note": note}

    if primary and primary.upper() in sp.by_taxonomy:
        cd, name = sp.by_taxonomy[primary.upper()]
        return out(cd, name, "taxonomy_primary", primary)
    for t in taxonomies:
        if t.upper() in sp.by_taxonomy:
            cd, name = sp.by_taxonomy[t.upper()]
            return out(cd, name, "taxonomy_secondary", t)
    if dac_spec:
        cd = sp.match_name(dac_spec)
        if cd:
            return out(cd, sp.names[cd], "medicare_specialty", dac_spec)
    if str(npi_type) != "2":
        inf = sp.infer_from_codes(codes)
        if inf:
            cd, f, cov, prec = inf
            return out(cd, sp.names.get(cd, cd), "inferred_from_codes",
                       f"F={f:.2f} (covers {cov:.0%} of the specialty's code volume; {prec:.0%} of the provider's codes)")
    if primary and primary.upper() in sp.nucc:
        return out("", sp.nucc[primary.upper()], "nucc", primary)
    if dac_spec:
        return out("", dac_spec.title(), "medicare_specialty_unmatched", dac_spec)
    if primary:
        return out("", f"Taxonomy {primary}", "taxonomy_unmapped", primary)
    return out("", "Unknown", "none")
