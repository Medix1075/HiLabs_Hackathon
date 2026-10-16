"""Small, deterministic normalisers shared by every step."""
from __future__ import annotations

import re

_STRAY = "`'\"‘’“”"
US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS",
    "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY",
    "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC",
}
_HIOS_RX = re.compile(r"^\d{5}([A-Z]{2})\d{3,}")
_FILE_STATE_RX = re.compile(r"[-_]([a-z]{2})[-_](?:in-network|allowed-amounts|oon)", re.IGNORECASE)


def norm_brand(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def norm_plan_name(value: str | None) -> str:
    """Trim, strip stray quote/backtick characters, collapse whitespace, uppercase."""
    s = (value or "").strip().strip(_STRAY).strip()
    s = re.sub(r"\s+", " ", s)
    return s.upper()


def norm_plan_id(value) -> str:
    if value is None:
        return ""
    s = str(value).strip()
    if s.endswith(".0") and s[:-2].isdigit():  # Excel turned an EIN into a float
        s = s[:-2]
    return s


def norm_url(value: str | None) -> str:
    return (value or "").strip()


def state_from_name(norm_name: str) -> str | None:
    """'CA L PPO ...' -> 'CA'. Only the first token counts, to avoid matching words like 'IN'.

    'SC' is skipped: in Health Net names it is the SmartCare product code, not South Carolina.
    """
    first = norm_name.split(" ", 1)[0] if norm_name else ""
    return first if first in US_STATES and first != "SC" else None


def state_from_plan_id(plan_id: str) -> str | None:
    """HIOS ids carry the state in characters 6-7: 67138CA060 -> CA."""
    m = _HIOS_RX.match(plan_id.upper())
    return m.group(1) if m and m.group(1) in US_STATES else None


def state_from_file_url(url: str) -> str | None:
    """Centene file names look like ..._ambetter-ok_in-network.json -> OK."""
    m = _FILE_STATE_RX.search(url.rsplit("/", 1)[-1])
    if m and m.group(1).upper() in US_STATES:
        return m.group(1).upper()
    return None
