"""Provider tags: the hook for grading one slice of a file (e.g. only its PPO providers).

C2 found that Health Net's CA rate file mixes PPO and HMO providers with a blank network_name,
so for now C3 grades every file as a whole (the "ALL" bucket). Once a later step can tell which
providers are PPO, pass a tag table and C3 also grades each tag separately, in the same pass.

A tag table is a CSV/XLSX with an `npi` and/or `tin` column and a `tag` column. A provider group
takes the tags of its TIN and of any of its NPIs; a group with none is "untagged".
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

ALL = "ALL"
UNTAGGED = "untagged"


class ProviderTagger:
    """Default tagger: no tags, so every price row falls only in the ALL bucket."""

    enabled = False

    def group_tags(self, npis, tin: str | None) -> frozenset:
        return frozenset()


class TableTagger(ProviderTagger):
    enabled = True

    def __init__(self, by_npi: dict[str, set], by_tin: dict[str, set]):
        self.by_npi, self.by_tin = by_npi, by_tin

    def group_tags(self, npis, tin):
        tags = set(self.by_tin.get(str(tin), ())) if tin is not None else set()
        for n in npis:
            tags |= self.by_npi.get(str(n), set())
        return frozenset(tags) or frozenset([UNTAGGED])


def _key(h) -> str:
    return re.sub(r"[\s_\-]", "", str(h or "").lower())


def load_tag_table(path: str | Path) -> TableTagger:
    path = Path(path)
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        import openpyxl

        ws = openpyxl.load_workbook(path, read_only=True, data_only=True).worksheets[0]
        it = ws.iter_rows(values_only=True)
        headers = list(next(it))
        records = [dict(zip(headers, r)) for r in it]
    else:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            records = list(csv.DictReader(fh))
    if not records:
        raise ValueError(f"tag table {path} is empty")
    cols = {_key(h): h for h in records[0]}
    tag_col = cols.get("tag") or cols.get("providertag") or cols.get("network")
    if not tag_col or not ({"npi", "tin"} & set(cols)):
        raise ValueError(f"tag table {path} needs a 'tag' column and an 'npi' and/or 'tin' column")
    by_npi: dict[str, set] = {}
    by_tin: dict[str, set] = {}
    for rec in records:
        tag = str(rec.get(tag_col) or "").strip()
        if not tag:
            continue
        for col, target in (("npi", by_npi), ("tin", by_tin)):
            v = rec.get(cols[col]) if col in cols else None
            if v not in (None, ""):
                target.setdefault(str(v).strip().split(".")[0], set()).add(tag)
    return TableTagger(by_npi, by_tin)
