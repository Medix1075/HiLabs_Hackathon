"""Step 0 - Dynamic scraper for Centene's Transparency-in-Coverage TOC (index) files.

The monthly index file names embed a publication date
(e.g. ``2026-09-28_ambetter_index.json``), so nothing here hardcodes a JSON URL.
Instead we:

1. Fetch https://www.centene.com/price-transparency-files.html
2. Locate the heading whose text is "Links to the download TOC files"
3. Walk the document forward from that heading collecting ``<a href>`` links and
   stop at the next heading ("Centene Employees"). The employee section is thus
   excluded *structurally* -- important because it also contains a "HEALTH NET"
   link, so filtering by brand name alone would be wrong.
4. Keep only ``.json`` / ``.json.gz`` links, resolve them to absolute URLs, derive
   the brand from the link label, and record parent/sub-index relationships
   (QualChoice sub-indexes inherit their parent's brand).

Usage::

    python -m ptd_pipeline.scraper                       # scrape the live page
    python -m ptd_pipeline.scraper --verify              # also HTTP-check every URL
    python -m ptd_pipeline.scraper --html-file page.html # parse a saved page
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger("ptd_pipeline.scraper")

SOURCE_URL = "https://www.centene.com/price-transparency-files.html"
DEFAULT_MANIFEST = Path("data") / "toc_urls.json"

TOC_HEADING_PATTERN = re.compile(r"links\s+to\s+the\s+download\s+toc\s+files", re.IGNORECASE)
HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")
JSON_PATH_PATTERN = re.compile(r"\.json(\.gz)?$", re.IGNORECASE)
FILE_DATE_PATTERN = re.compile(r"^(\d{4}-\d{2}-\d{2})_(.+?)(?:_index)?\.json(?:\.gz)?$", re.IGNORECASE)
BRAND_LABEL_PATTERN = re.compile(r"index\s+file\s+for\s+(.+?)(?:\s+plans?)?\s*$", re.IGNORECASE)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 ptd-pipeline/0.1"
)


class ScrapeError(RuntimeError):
    """Raised when the page structure no longer matches what we expect."""


@dataclass(frozen=True)
class TocLink:
    brand: str
    label: str
    url: str
    filename: str
    issuer: str
    file_date: Optional[str]
    is_sub_index: bool
    parent_url: Optional[str]


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def build_session(retries: int = 3, backoff: float = 1.0) -> requests.Session:
    """Session with retry/backoff for transient 429/5xx and connection errors."""
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    retry = Retry(
        total=retries,
        backoff_factor=backoff,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "HEAD"),
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def fetch_html(url: str = SOURCE_URL, session: Optional[requests.Session] = None, timeout: int = 30) -> str:
    session = session or build_session()
    log.info("Fetching %s", url)
    resp = session.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.text


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def _find_toc_heading(soup: BeautifulSoup) -> Tag:
    for heading in soup.find_all(HEADING_TAGS):
        if TOC_HEADING_PATTERN.search(heading.get_text(" ", strip=True)):
            return heading

    # Fallback: heading text present but not inside an <hN> (e.g. <p><strong>...).
    text_node = soup.find(string=TOC_HEADING_PATTERN)
    if text_node is not None:
        block = text_node.find_parent(["p", "div", "strong", "b"]) or text_node.parent
        if isinstance(block, Tag):
            log.warning("TOC heading is not an <h*> tag; falling back to <%s>", block.name)
            return block

    raise ScrapeError(
        "Could not find the 'Links to the download TOC files' heading. "
        "The Centene page layout may have changed."
    )


def _is_json_link(href: str) -> bool:
    return bool(JSON_PATH_PATTERN.search(urlparse(href).path))


def _describe_file(url: str) -> Tuple[str, str, Optional[str]]:
    """Return (filename, issuer_token, file_date) parsed from the URL's file name."""
    filename = unquote(Path(urlparse(url).path).name)
    match = FILE_DATE_PATTERN.match(filename)
    if match:
        file_date, rest = match.group(1), match.group(2)
        issuer = rest.split("_", 1)[0].lower()
    else:
        file_date = None
        issuer = re.split(r"[_\-.]", filename, maxsplit=1)[0].lower()
    return filename, issuer, file_date


def _brand_from_label(label: str, fallback: str) -> str:
    match = BRAND_LABEL_PATTERN.search(label)
    return match.group(1).strip() if match else fallback.title()


def _parent_json_link(anchor: Tag, section_anchor_ids: set) -> Optional[Tag]:
    """If ``anchor`` sits in a nested list, return the JSON link of the enclosing <li>."""
    own_li = anchor.find_parent("li")
    if own_li is None:
        return None
    outer_li = own_li.find_parent("li")
    if outer_li is None:
        return None
    for candidate in outer_li.find_all("a", href=True):
        if candidate is anchor or id(candidate) not in section_anchor_ids:
            continue
        # The parent link must belong directly to outer_li, not to a sibling sub-item.
        if candidate.find_parent("li") is outer_li and _is_json_link(candidate["href"]):
            return candidate
    return None


def parse_toc_links(html: str, base_url: str = SOURCE_URL) -> List[TocLink]:
    """Extract TOC index-file links from the Centene page HTML."""
    soup = BeautifulSoup(html, "html.parser")
    heading = _find_toc_heading(soup)

    section_anchors: List[Tag] = []
    for element in heading.find_all_next():
        if not isinstance(element, Tag):
            continue
        if element.name in HEADING_TAGS:
            log.debug("TOC section ends at <%s>: %s", element.name, element.get_text(" ", strip=True))
            break
        if element.name == "a" and element.has_attr("href"):
            section_anchors.append(element)

    anchor_ids = {id(a) for a in section_anchors}
    links: List[TocLink] = []
    brand_by_url: Dict[str, str] = {}
    seen = set()
    for anchor in section_anchors:
        href = anchor["href"].strip()
        if not _is_json_link(href):
            log.debug("Skipping non-JSON link in TOC section: %s", href)
            continue
        url = urljoin(base_url, href)
        if url in seen:
            continue
        seen.add(url)

        parent = _parent_json_link(anchor, anchor_ids)
        parent_url = urljoin(base_url, parent["href"].strip()) if parent is not None else None
        filename, issuer, file_date = _describe_file(url)
        label = anchor.get_text(" ", strip=True)

        if parent_url:  # sub-indexes inherit the parent's issuer/brand
            issuer = _describe_file(parent_url)[1]
            brand = brand_by_url.get(parent_url) or issuer.title()
        else:
            brand = _brand_from_label(label, issuer)
        brand_by_url[url] = brand

        links.append(
            TocLink(
                brand=brand,
                label=label,
                url=url,
                filename=filename,
                issuer=issuer,
                file_date=file_date,
                is_sub_index=parent_url is not None,
                parent_url=parent_url,
            )
        )

    if not links:
        raise ScrapeError("Found the TOC heading but no JSON links beneath it.")
    return links


# --------------------------------------------------------------------------- #
# Verification / manifest I/O
# --------------------------------------------------------------------------- #
def verify_links(links: List[TocLink], session: Optional[requests.Session] = None, timeout: int = 30) -> dict:
    """HEAD each URL (falling back to a streamed GET) and return {url: status}."""
    session = session or build_session()
    results = {}
    for link in links:
        try:
            resp = session.head(link.url, timeout=timeout, allow_redirects=True)
            status = resp.status_code
            if status >= 400:
                with session.get(link.url, timeout=timeout, stream=True) as get_resp:
                    status = get_resp.status_code
            results[link.url] = status
        except requests.RequestException as exc:
            results[link.url] = f"error: {exc.__class__.__name__}"
    return results


def build_manifest(links: List[TocLink], source_url: str, verification: Optional[dict] = None) -> dict:
    records = []
    for link in links:
        record = asdict(link)
        if verification is not None:
            record["http_status"] = verification.get(link.url)
        records.append(record)
    return {
        "source_url": source_url,
        "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "count": len(records),
        "top_level_count": sum(1 for r in records if not r["is_sub_index"]),
        "sub_index_count": sum(1 for r in records if r["is_sub_index"]),
        "links": records,
    }


def write_manifest(manifest: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def load_manifest(path: Path) -> List[TocLink]:
    data = json.loads(path.read_text(encoding="utf-8"))
    fields = TocLink.__dataclass_fields__
    return [TocLink(**{k: v for k, v in rec.items() if k in fields}) for rec in data["links"]]


def scrape(url: str = SOURCE_URL, html_file: Optional[Path] = None) -> List[TocLink]:
    html = html_file.read_text(encoding="utf-8") if html_file else fetch_html(url)
    return parse_toc_links(html, base_url=url)


def _print_summary(links: List[TocLink], verification: Optional[dict]) -> None:
    for link in links:
        prefix = "    - " if link.is_sub_index else "* "
        status = f"  [{verification.get(link.url)}]" if verification else ""
        print(f"{prefix}{link.brand:<11} {link.file_date or '-':<10}  {link.url}{status}")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Step 0: scrape Centene TOC index-file URLs.")
    parser.add_argument("--url", default=SOURCE_URL, help="Page to scrape (default: %(default)s)")
    parser.add_argument("--html-file", type=Path, help="Parse a saved HTML file instead of fetching")
    parser.add_argument("--output", type=Path, default=DEFAULT_MANIFEST, help="Manifest path (default: %(default)s)")
    parser.add_argument("--verify", action="store_true", help="HTTP-check every scraped URL")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        links = scrape(args.url, args.html_file)
    except (requests.RequestException, ScrapeError, OSError) as exc:
        log.error("Scrape failed: %s", exc)
        return 1

    verification = verify_links(links) if args.verify else None
    manifest = build_manifest(links, args.url, verification)
    write_manifest(manifest, args.output)

    _print_summary(links, verification)
    log.info(
        "Wrote %d links (%d index, %d sub-index) to %s",
        manifest["count"], manifest["top_level_count"], manifest["sub_index_count"], args.output,
    )

    if verification and any(not (isinstance(s, int) and s < 400) for s in verification.values()):
        log.error("One or more URLs failed verification.")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
