"""Record a directory website's own search results while someone searches it in a real browser.

Payer directories (Health Net's, and the Cigna-hosted copy that works outside California) are
JavaScript apps: the page asks the site's own API for results and draws them. Instead of scraping the
drawn page, this opens the site in Chrome/Edge/Chromium with Playwright and saves every JSON response
the app receives, together with the request that produced it, to a JSONL file. You choose the network
(e.g. the PPO) and search as usual; close the browser when done.

The saved requests are also what `replay` re-sends with other ZIP codes or pages, so one manual search
is enough to collect a whole region.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

DROP_HEADERS = {"content-length", "host", "cookie", ":authority", ":method", ":path", ":scheme"}


def launch(pw, channel: str | None, executable: str | None, headless: bool):
    """Prefer an installed Chrome / Edge (no download needed), else Playwright's own Chromium."""
    kw = {"headless": headless}
    if executable:
        return pw.chromium.launch(executable_path=executable, **kw)
    for ch in ([channel] if channel else ["chrome", "msedge"]):
        try:
            return pw.chromium.launch(channel=ch, **kw)
        except Exception:  # channel not installed: try the next one
            continue
    return pw.chromium.launch(**kw)


def capture(url: str, out: Path, network_name: str, channel: str | None = None, executable: str | None = None,
            headless: bool = False, max_minutes: float = 180, stop_after_seconds: float | None = None,
            log=print) -> dict:
    from playwright.sync_api import sync_playwright

    out.parent.mkdir(parents=True, exist_ok=True)
    stats = {"responses": 0, "json_responses": 0}
    fh = open(out, "a", encoding="utf-8")

    def on_response(resp):
        stats["responses"] += 1
        try:
            ctype = (resp.headers or {}).get("content-type", "")
            if "json" not in ctype and not resp.url.split("?")[0].endswith(".json"):
                return
            body = resp.json()
        except Exception:
            return
        req = resp.request
        try:
            headers = {k: v for k, v in req.all_headers().items() if k.lower() not in DROP_HEADERS}
        except Exception:
            headers = {}
        rec = {"t": datetime.now(timezone.utc).isoformat(), "url": resp.url, "method": req.method,
               "post_data": req.post_data, "status": resp.status, "request_headers": headers,
               "network_name": network_name, "page_url": url, "body": body}
        fh.write(json.dumps(rec, default=str) + "\n")
        fh.flush()
        stats["json_responses"] += 1

    with sync_playwright() as pw:
        browser = launch(pw, channel, executable, headless)
        ctx = browser.new_context()
        ctx.on("response", on_response)
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=120000)
        log(f"[dir] browser open at {url}. Choose the network ({network_name!r}), search, page through the "
            "results, then close the browser window. Every JSON response is saved to " + str(out))
        t0 = time.time()
        while True:
            if stop_after_seconds is not None and time.time() - t0 >= stop_after_seconds:
                break
            if time.time() - t0 >= max_minutes * 60:
                log("[dir] time limit reached; stopping")
                break
            try:
                if not ctx.pages:
                    break
                ctx.pages[0].wait_for_timeout(500)
            except Exception:  # the user closed the window
                break
        try:
            cookies = ctx.cookies()
            out.with_suffix(".cookies.json").write_text(json.dumps(cookies))
            browser.close()
        except Exception:
            pass
    fh.close()
    log(f"[dir] saved {stats['json_responses']:,} JSON responses (of {stats['responses']:,}) to {out}")
    return stats
