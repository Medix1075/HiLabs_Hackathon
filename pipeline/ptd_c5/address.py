"""Address standardisation, so the same place matches across NPPES, CMS and the payer directory.

A local, USPS Publication 28 style standardiser (no network): upper case, punctuation removed,
street suffixes and directionals abbreviated (STREET -> ST, NORTH -> N), unit designators split
off into their own field (SUITE 4, STE 4, #4 -> STE 4), ZIP cut to 5 digits, and the two address
lines swapped when line 1 is a building name and line 2 is the street.

Two keys come out of it:
  site_key  house number + street + ZIP5. Suites in one building are the same site: a provider
            in suite 200 and its group's front desk in suite 100 practise at the same place.
  full_key  site_key + unit, for display de-duplication.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

SUFFIX = {
    "STREET": "ST", "STR": "ST", "AVENUE": "AVE", "AV": "AVE", "AVEN": "AVE", "BOULEVARD": "BLVD", "BOUL": "BLVD",
    "DRIVE": "DR", "DRV": "DR", "ROAD": "RD", "LANE": "LN", "COURT": "CT", "PLACE": "PL", "PARKWAY": "PKWY",
    "PKY": "PKWY", "HIGHWAY": "HWY", "CIRCLE": "CIR", "TERRACE": "TER", "SQUARE": "SQ", "TRAIL": "TRL",
    "CENTER": "CTR", "CENTRE": "CTR", "PLAZA": "PLZ", "EXPRESSWAY": "EXPY", "FREEWAY": "FWY", "MOUNT": "MT",
    "MOUNTAIN": "MTN", "POINT": "PT", "ROUTE": "RTE", "STATION": "STA", "VALLEY": "VLY", "VISTA": "VIS",
    "HEIGHTS": "HTS", "CREEK": "CRK", "RIDGE": "RDG", "SPRINGS": "SPGS", "ALLEY": "ALY", "CROSSING": "XING",
    "LOOP": "LOOP", "WAY": "WAY", "PATH": "PATH", "WALK": "WALK", "PIKE": "PIKE", "GROVE": "GRV", "PARK": "PARK",
    "CAMINO": "CAMINO", "PASEO": "PASEO",
}
DIRECTION = {"NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W", "NORTHEAST": "NE", "NORTHWEST": "NW",
             "SOUTHEAST": "SE", "SOUTHWEST": "SW"}
ORDINAL = {"FIRST": "1ST", "SECOND": "2ND", "THIRD": "3RD", "FOURTH": "4TH", "FIFTH": "5TH", "SIXTH": "6TH",
           "SEVENTH": "7TH", "EIGHTH": "8TH", "NINTH": "9TH", "TENTH": "10TH"}
UNIT = {"SUITE": "STE", "STE": "STE", "UNIT": "UNIT", "APT": "APT", "APARTMENT": "APT", "BLDG": "BLDG",
        "BUILDING": "BLDG", "FLOOR": "FL", "FL": "FL", "FLR": "FL", "ROOM": "RM", "RM": "RM", "DEPT": "DEPT",
        "DEPARTMENT": "DEPT", "OFFICE": "OFC", "OFC": "OFC", "SPC": "SPC", "SPACE": "SPC", "#": "STE", "NO": "STE"}
UNIT_RX = re.compile(r"\b(" + "|".join(sorted((re.escape(u) for u in UNIT if u != "#"), key=len, reverse=True))
                     + r")\b\.?\s*#?\s*([A-Z0-9\-]+)\s*$|#\s*([A-Z0-9\-]+)\s*$")
PO_BOX_RX = re.compile(r"\bP\s*O\s*BOX\b|\bPOST OFFICE BOX\b|\bPOB\b|^BOX \d+")


@dataclass
class Address:
    line1: str
    unit: str
    city: str
    state: str
    zip5: str
    po_box: bool
    raw: str

    @property
    def site_key(self) -> str:
        m = re.match(r"^(\d+[A-Z]?)\s+(.*)$", self.line1)
        if m:
            return f"{m.group(1)}|{m.group(2)}|{self.zip5 or self.city}"
        return f"|{self.line1}|{self.zip5 or self.city}"

    @property
    def full_key(self) -> str:
        return f"{self.site_key}|{self.unit}"

    def display(self) -> str:
        street = f"{self.line1} {self.unit}".strip()
        tail = f"{self.state} {self.zip5}".strip()
        return ", ".join(p for p in (street, self.city, tail) if p)


def _clean(s) -> str:
    s = str(s or "").upper().replace("&", " AND ")
    s = re.sub(r"[^\w#/\- ]", " ", s)
    s = s.replace("_", " ")
    return re.sub(r"\s+", " ", s).strip()


def _words(line: str) -> str:
    out = []
    for w in line.split(" "):
        if w in ORDINAL:
            w = ORDINAL[w]
        elif w in DIRECTION:
            w = DIRECTION[w]
        elif w in SUFFIX:
            w = SUFFIX[w]
        out.append(w)
    return " ".join(out)


def _split_unit(line: str) -> tuple[str, str]:
    m = UNIT_RX.search(line)
    if not m:
        return line, ""
    if m.group(3):
        return line[:m.start()].strip(), f"STE {m.group(3)}"
    return line[:m.start()].strip(), f"{UNIT.get(m.group(1), m.group(1))} {m.group(2)}"


def _looks_like_unit(line: str) -> bool:
    return bool(re.fullmatch(r"(" + "|".join(re.escape(u) for u in UNIT if u != "#") + r")\.?\s*#?\s*[A-Z0-9\-]+|#\s*[A-Z0-9\-]+",
                             line))


def standardize(line1, line2="", city="", state="", zip_code="") -> Address:
    raw = ", ".join(str(x) for x in (line1, line2, city, state, zip_code) if x)
    l1, l2 = _clean(line1), _clean(line2)
    # "MEDICAL PLAZA" / "123 MAIN ST": the street is on line 2
    if l2 and not re.match(r"^\d", l1) and re.match(r"^\d+\s+\w", l2) and not _looks_like_unit(l2):
        l1, l2 = l2, l1
    l1, unit = _split_unit(l1)
    if l2:
        if _looks_like_unit(l2):
            unit = unit or _split_unit(l2)[1] or l2
        elif not unit:
            rest, u2 = _split_unit(l2)
            unit = u2
    l1 = _words(l1)
    unit = re.sub(r"\s+", " ", unit)
    z = re.sub(r"\D", "", str(zip_code or ""))
    if len(z) in (4, 8):  # a leading zero lost in a spreadsheet
        z = "0" + z
    z = z[:5]
    return Address(line1=l1, unit=unit, city=_clean(city), state=_clean(state)[:2], zip5=z,
                   po_box=bool(PO_BOX_RX.search(l1)), raw=raw)
