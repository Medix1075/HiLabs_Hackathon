"""End-to-end entry point: Step 0 (scrape TOC URLs) -> Step 1 (parse index files).

    python -m ptd_pipeline
    python -m ptd_pipeline --brand "Health Net" --brand Ambetter
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import List, Optional

import requests

from .index_parser import add_step1_args, filter_links, run_step1
from .scraper import DEFAULT_MANIFEST, SOURCE_URL, ScrapeError, build_manifest, scrape, write_manifest

log = logging.getLogger("ptd_pipeline")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Centene PTD pipeline: scrape TOC URLs, then build the index table.")
    parser.add_argument("--url", default=SOURCE_URL, help="Centene price transparency page")
    parser.add_argument("--html-file", type=Path, help="Use a saved copy of the page instead of fetching it")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST, help="Where to write the Step 0 manifest")
    add_step1_args(parser)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    log.info("=== Step 0: scraping TOC index URLs ===")
    try:
        links = scrape(args.url, args.html_file)
    except (requests.RequestException, ScrapeError, OSError) as exc:
        log.error("Scrape failed: %s", exc)
        return 1
    write_manifest(build_manifest(links, args.url), args.manifest)
    for link in links:
        log.info("  %s%-11s %s", "  - " if link.is_sub_index else "", link.brand, link.url)
    log.info("Found %d index files -> %s", len(links), args.manifest)

    selected = filter_links(links, args.brand)
    if not selected:
        log.error("No index files match the requested brand filter.")
        return 1

    log.info("=== Step 1: parsing %d index file(s) ===", len(selected))
    report = run_step1(selected, args.output, args.rate_files_output, args.db, args.raw_dir,
                       cache=not args.no_cache, refresh=args.refresh)
    return 2 if report.failures else 0


if __name__ == "__main__":
    sys.exit(main())
