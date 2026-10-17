"""Step 1 (Challenge C1) - Stream-parse Centene TOC index files into one flat table.

Every index file follows the CMS Transparency-in-Coverage *table-of-contents*
schema::

    {
      "reporting_entity_name": "...",
      "reporting_structure": [
        {
          "reporting_plans":  [{"plan_name", "plan_id", "plan_id_type", "plan_market_type"}, ...],
          "in_network_files": [{"description", "location"}, ...],
          "allowed_amount_file": {...}
        },
        ...
      ]
    }

Each (plan x in-network file) pair inside a ``reporting_structure`` becomes one row:

    brand | plan name | plan ID | market type | rate file URL

Scalability:
* Index files are parsed with ``ijson`` one ``reporting_structure`` item at a time,
  so a multi-GB index never sits in memory.
* Downloads are streamed to disk in chunks (or parsed straight off the socket
  with ``--no-cache``).
* Deduplication is done by SQLite (``UNIQUE`` over all output columns +
  ``INSERT OR IGNORE``), so the seen-set lives on disk rather than in RAM.

Usage::

    python -m ptd_pipeline.index_parser                       # uses data/toc_urls.json
    python -m ptd_pipeline.index_parser --brand Fidelis       # only some brands
"""

from __future__ import annotations

import argparse
import csv
import gzip
import logging
import sqlite3
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

import ijson
import requests

from .scraper import DEFAULT_MANIFEST, TocLink, build_session, load_manifest

log = logging.getLogger("ptd_pipeline.index_parser")

DEFAULT_RAW_DIR = Path("data") / "raw"
DEFAULT_DB = Path("data") / "ptd.sqlite"
DEFAULT_TABLE_CSV = Path("data") / "index_table.csv"
DEFAULT_RATE_FILES_CSV = Path("data") / "rate_files.csv"

OUTPUT_COLUMNS = ("brand", "plan name", "plan ID", "market type", "rate file URL")
DB_COLUMNS = ("brand", "plan_name", "plan_id", "market_type", "rate_file_url")

CHUNK_SIZE = 1 << 20  # 1 MiB download chunks
INSERT_BATCH = 10_000
GZIP_MAGIC = b"\x1f\x8b"

Row = Tuple[str, str, str, str, str]


# --------------------------------------------------------------------------- #
# Fetching / opening index files as byte streams
# --------------------------------------------------------------------------- #
def download(link: TocLink, raw_dir: Path, session: requests.Session, refresh: bool = False) -> Path:
    """Stream ``link.url`` to ``raw_dir`` (skip if already cached)."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    target = raw_dir / link.filename
    if target.exists() and target.stat().st_size > 0 and not refresh:
        log.info("Using cached %s", target)
        return target

    tmp = target.with_suffix(target.suffix + ".part")
    log.info("Downloading %s", link.url)
    with session.get(link.url, stream=True, timeout=(30, 300)) as resp:
        resp.raise_for_status()
        resp.raw.decode_content = True  # undo any Content-Encoding transparently
        with open(tmp, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                if chunk:
                    fh.write(chunk)
    tmp.replace(target)  # atomic: partial downloads never look complete
    return target


@contextmanager
def open_local(path: Path) -> Iterator[IO[bytes]]:
    with open(path, "rb") as probe:
        is_gzip = probe.read(2) == GZIP_MAGIC
    fh = gzip.open(path, "rb") if is_gzip else open(path, "rb")
    try:
        yield fh
    finally:
        fh.close()


@contextmanager
def open_remote(url: str, session: requests.Session) -> Iterator[IO[bytes]]:
    """Parse straight off the socket - nothing is written to disk."""
    with session.get(url, stream=True, timeout=(30, 300)) as resp:
        resp.raise_for_status()
        resp.raw.decode_content = True
        stream: IO[bytes] = resp.raw
        if url.lower().endswith(".gz"):
            stream = gzip.GzipFile(fileobj=resp.raw)  # type: ignore[assignment]
        yield stream


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def _clean(value: object) -> str:
    return "" if value is None else str(value).strip()


def iter_index_rows(stream: IO[bytes], brand: str) -> Iterator[Row]:
    """Yield one row per (reporting plan x in-network rate file) from a TOC stream."""
    for structure in ijson.items(stream, "reporting_structure.item"):
        plans = structure.get("reporting_plans") or []
        files = structure.get("in_network_files") or []
        if isinstance(files, dict):  # tolerate a single object instead of a list
            files = [files]
        urls = [_clean(f.get("location")) for f in files if isinstance(f, dict)]
        urls = [u for u in urls if u]
        if not urls:
            continue
        for plan in plans:
            name = _clean(plan.get("plan_name"))
            plan_id = _clean(plan.get("plan_id"))
            market = _clean(plan.get("plan_market_type"))
            for url in urls:
                yield (brand, name, plan_id, market, url)


# --------------------------------------------------------------------------- #
# Storage (SQLite-backed dedup)
# --------------------------------------------------------------------------- #
def init_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("DROP TABLE IF EXISTS index_rows")  # every run reflects the current month
    conn.execute(
        """
        CREATE TABLE index_rows (
            brand          TEXT NOT NULL,
            plan_name      TEXT NOT NULL,
            plan_id        TEXT NOT NULL,
            market_type    TEXT NOT NULL,
            rate_file_url  TEXT NOT NULL,
            source_index   TEXT NOT NULL,
            UNIQUE (brand, plan_name, plan_id, market_type, rate_file_url)
        )
        """
    )
    return conn


def insert_rows(conn: sqlite3.Connection, rows: Iterable[Row], source_index: str) -> Tuple[int, int]:
    """Insert rows in batches; return (rows_seen, rows_inserted)."""
    sql = (
        "INSERT OR IGNORE INTO index_rows "
        "(brand, plan_name, plan_id, market_type, rate_file_url, source_index) "
        "VALUES (?, ?, ?, ?, ?, ?)"
    )
    seen = inserted = 0
    batch: List[tuple] = []

    def flush() -> None:
        nonlocal inserted
        before = conn.total_changes
        conn.executemany(sql, batch)
        conn.commit()
        inserted += conn.total_changes - before
        batch.clear()

    for row in rows:
        batch.append(row + (source_index,))
        seen += 1
        if len(batch) >= INSERT_BATCH:
            flush()
    if batch:
        flush()
    return seen, inserted


def export_table(conn: sqlite3.Connection, path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    cursor = conn.execute(
        f"SELECT {', '.join(DB_COLUMNS)} FROM index_rows "
        "ORDER BY brand, plan_name, plan_id, rate_file_url"
    )
    count = 0
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(OUTPUT_COLUMNS)
        for row in cursor:  # cursor iterates lazily - no fetchall()
            writer.writerow(row)
            count += 1
    return count


def export_rate_files(conn: sqlite3.Connection, path: Path) -> int:
    """Unique rate-file URLs with how many plans reference each (for downstream download)."""
    cursor = conn.execute(
        """
        SELECT rate_file_url, brand, COUNT(DISTINCT plan_id || '|' || plan_name) AS plan_count
        FROM index_rows
        GROUP BY rate_file_url, brand
        ORDER BY brand, rate_file_url
        """
    )
    count = 0
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(("rate file URL", "brand", "plan count"))
        for row in cursor:
            writer.writerow(row)
            count += 1
    return count


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
@dataclass
class ParseReport:
    rows_seen: int = 0
    rows_unique: int = 0
    per_index: Dict[str, Tuple[int, int]] = field(default_factory=dict)
    failures: Dict[str, str] = field(default_factory=dict)


def parse_indexes(
    links: Sequence[TocLink],
    db_path: Path = DEFAULT_DB,
    raw_dir: Path = DEFAULT_RAW_DIR,
    cache: bool = True,
    refresh: bool = False,
    session: Optional[requests.Session] = None,
) -> Tuple[sqlite3.Connection, ParseReport]:
    session = session or build_session()
    conn = init_db(db_path)
    report = ParseReport()

    for link in links:
        log.info("Parsing [%s] %s", link.brand, link.filename)
        try:
            if cache:
                opener = open_local(download(link, raw_dir, session, refresh))
            else:
                opener = open_remote(link.url, session)
            with opener as stream:
                seen, inserted = insert_rows(conn, iter_index_rows(stream, link.brand), link.url)
        except (requests.RequestException, ijson.JSONError, OSError, ValueError) as exc:
            log.error("Failed on %s: %s", link.url, exc)
            report.failures[link.url] = f"{exc.__class__.__name__}: {exc}"
            continue

        report.per_index[link.url] = (seen, inserted)
        report.rows_seen += seen
        report.rows_unique += inserted
        log.info("  %s rows read, %s new unique rows", f"{seen:,}", f"{inserted:,}")
        if seen == 0:
            log.warning("  No in-network rows found in %s", link.filename)

    return conn, report


def run_step1(
    links: Sequence[TocLink],
    output: Path = DEFAULT_TABLE_CSV,
    rate_files_output: Path = DEFAULT_RATE_FILES_CSV,
    db_path: Path = DEFAULT_DB,
    raw_dir: Path = DEFAULT_RAW_DIR,
    cache: bool = True,
    refresh: bool = False,
) -> ParseReport:
    conn, report = parse_indexes(links, db_path, raw_dir, cache, refresh)
    try:
        table_rows = export_table(conn, output)
        rate_files = export_rate_files(conn, rate_files_output)
        by_brand = conn.execute(
            "SELECT brand, COUNT(*), COUNT(DISTINCT rate_file_url) FROM index_rows GROUP BY brand ORDER BY brand"
        ).fetchall()
    finally:
        conn.close()

    dupes = report.rows_seen - report.rows_unique
    log.info("Rows read: %s | duplicates removed: %s | unique rows: %s",
             f"{report.rows_seen:,}", f"{dupes:,}", f"{table_rows:,}")
    for brand, rows, urls in by_brand:
        log.info("  %-12s %10s rows  %8s unique rate files", brand, f"{rows:,}", f"{urls:,}")
    log.info("Wrote %s and %s (%s unique rate files)", output, rate_files_output, f"{rate_files:,}")
    if report.failures:
        log.error("%d index file(s) failed: %s", len(report.failures), ", ".join(report.failures))
    return report


def filter_links(links: Sequence[TocLink], brands: Optional[Sequence[str]]) -> List[TocLink]:
    if not brands:
        return list(links)
    wanted = {b.lower() for b in brands}
    return [l for l in links if l.brand.lower() in wanted or l.issuer.lower() in wanted]


def add_step1_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--output", type=Path, default=DEFAULT_TABLE_CSV, help="Unified table CSV (default: %(default)s)")
    parser.add_argument("--rate-files-output", type=Path, default=DEFAULT_RATE_FILES_CSV,
                        help="Unique rate-file URL list (default: %(default)s)")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite working DB (default: %(default)s)")
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR, help="Index download cache (default: %(default)s)")
    parser.add_argument("--no-cache", action="store_true", help="Stream directly from HTTP; do not save index files")
    parser.add_argument("--refresh", action="store_true", help="Re-download index files even if cached")
    parser.add_argument("--brand", action="append", help="Only process this brand (repeatable), e.g. --brand Fidelis")
    parser.add_argument("-v", "--verbose", action="store_true")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Step 1: parse TOC index files into one deduplicated table.")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST,
                        help="Manifest from Step 0 (default: %(default)s)")
    add_step1_args(parser)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if not args.manifest.exists():
        log.error("Manifest %s not found. Run `python -m ptd_pipeline.scraper` first "
                  "(or `python -m ptd_pipeline` to run both steps).", args.manifest)
        return 1

    links = filter_links(load_manifest(args.manifest), args.brand)
    if not links:
        log.error("No index files match the requested brand filter.")
        return 1

    report = run_step1(links, args.output, args.rate_files_output, args.db, args.raw_dir,
                       cache=not args.no_cache, refresh=args.refresh)
    return 2 if report.failures else 0


if __name__ == "__main__":
    sys.exit(main())
