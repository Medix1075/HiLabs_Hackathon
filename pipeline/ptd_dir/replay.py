"""Re-send one captured directory search with other values (ZIP codes, pages, NPIs ...).

A manual search in the browser shows how the site's API is called. `replay` takes that request (by
default the captured response with the most providers in it) and sends it again for every value of
the parameters you vary, e.g. every ZIP code where C4's providers practise, and every result page. A
parameter is found by name wherever it sits: the query string, a JSON body (at any depth) or a form
body. Responses are appended in the capture format, so `build` reads both the same way.

Be polite: requests are spaced (`--delay`), paging stops at the first page with no providers, and a
run is capped (`--max-requests`). Check the site's terms of use before a large run.
"""
from __future__ import annotations

import itertools
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests

from .parse import providers_in, read_capture

PAGE_WORDS = ("page", "offset", "start", "skip", "from")


def parse_vary(spec: str) -> tuple[str, list[str]]:
    """'zip=@zips.txt' | 'page=1..20' | 'npi=1234567890,1234567891'."""
    name, _, vals = spec.partition("=")
    if not name or not vals:
        raise ValueError(f"--vary needs name=values, got {spec!r}")
    if vals.startswith("@"):
        values = [v.strip() for v in Path(vals[1:]).read_text().splitlines() if v.strip() and not v.startswith("#")]
    elif re.fullmatch(r"\d+\.\.\d+", vals):
        a, b = map(int, vals.split(".."))
        values = [str(i) for i in range(a, b + 1)]
    else:
        values = [v.strip() for v in vals.split(",") if v.strip()]
    return name.strip(), values


def _set_json(obj, name: str, value):
    """Replace every value under key `name` (any depth). Keeps numbers numeric."""
    hit = False
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == name and not isinstance(v, (dict, list)):
                obj[k] = int(value) if isinstance(v, int) and str(value).isdigit() else value
                hit = True
            else:
                hit |= _set_json(v, name, value)
    elif isinstance(obj, list):
        for x in obj:
            hit |= _set_json(x, name, value)
    return hit


def build_request(rec: dict, values: dict) -> tuple[str, str | None, list[str]]:
    """(url, body, names not found) for one combination of values."""
    u = urlparse(rec["url"])
    q = parse_qsl(u.query, keep_blank_values=True)
    body = rec.get("post_data")
    missing = []
    body_json = None
    if body:
        try:
            body_json = json.loads(body)
        except (TypeError, json.JSONDecodeError):
            body_json = None
    form = parse_qsl(body, keep_blank_values=True) if body and body_json is None and "=" in body else None
    for name, val in values.items():
        found = False
        if any(k == name for k, _ in q):
            q = [(k, val if k == name else v) for k, v in q]
            found = True
        if body_json is not None and _set_json(body_json, name, val):
            found = True
        if form is not None and any(k == name for k, _ in form):
            form = [(k, val if k == name else v) for k, v in form]
            found = True
        if not found:
            missing.append(name)
    url = urlunparse(u._replace(query=urlencode(q)))
    if body_json is not None:
        body = json.dumps(body_json)
    elif form is not None:
        body = urlencode(form)
    return url, body, missing


def pick_request(captures: list[Path], index: int | None = None) -> dict:
    recs = [r for p in captures for r in read_capture(p)]
    if not recs:
        raise ValueError("no captured responses")
    if index is not None:
        return recs[index]
    return max(recs, key=lambda r: len(providers_in(r.get("body"))))


def replay(captures: list[Path], out: Path, vary: list[str], index: int | None = None, delay: float = 1.0,
           max_requests: int = 5000, timeout: int = 60, log=print) -> dict:
    rec = pick_request(captures, index)
    specs = [parse_vary(v) for v in vary]
    page = next(((n, v) for n, v in specs if any(w in n.lower() for w in PAGE_WORDS)), None)
    outer = [(n, v) for n, v in specs if page is None or n != page[0]]
    log(f"[dir] replaying {rec['method']} {rec['url'][:120]} ({len(providers_in(rec.get('body')))} providers in the "
        f"captured response); varying {', '.join(n for n, _ in specs) or 'nothing'}")
    session = requests.Session()
    session.headers.update(rec.get("request_headers") or {})
    cookie_file = None
    for p in captures:
        c = p.with_suffix(".cookies.json")
        if c.exists():
            cookie_file = c
    if cookie_file:
        for c in json.loads(cookie_file.read_text()):
            session.cookies.set(c["name"], c["value"], domain=c.get("domain"), path=c.get("path", "/"))
    stats = {"requests": 0, "providers": 0, "errors": 0, "missing_params": []}
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "a", encoding="utf-8") as fh:
        for combo in itertools.product(*[v for _, v in outer]) if outer else [()]:
            base = dict(zip([n for n, _ in outer], combo))
            for pv in (page[1] if page else [None]):
                if stats["requests"] >= max_requests:
                    log(f"[dir] stopped at --max-requests {max_requests}")
                    return stats
                values = {**base, **({page[0]: pv} if page else {})}
                url, body, missing = build_request(rec, values)
                if missing and not stats["missing_params"]:
                    stats["missing_params"] = missing
                    log(f"[dir] WARNING: parameter(s) {missing} not found in the captured request; check the names")
                try:
                    resp = session.request(rec["method"], url, data=body, timeout=timeout)
                    stats["requests"] += 1
                    data = resp.json()
                except Exception as exc:
                    stats["errors"] += 1
                    log(f"[dir]   {values}: {exc.__class__.__name__}")
                    time.sleep(delay)
                    continue
                n = len(providers_in(data))
                stats["providers"] += n
                fh.write(json.dumps({"t": datetime.now(timezone.utc).isoformat(), "url": url, "method": rec["method"],
                                     "post_data": body, "status": resp.status_code, "values": values,
                                     "network_name": rec.get("network_name", ""), "body": data}, default=str) + "\n")
                time.sleep(delay)
                if page and n == 0:
                    break  # past the last page
            log(f"[dir]   {base or 'request'}: done ({stats['providers']:,} provider rows so far)")
    log(f"[dir] replay: {stats['requests']:,} requests, {stats['providers']:,} provider rows, {stats['errors']} errors")
    return stats
