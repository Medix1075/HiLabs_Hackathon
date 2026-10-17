"""The rate store shared by C4 to C7: one SQLite file (default out/rates.sqlite).

C4 writes the file's structure in normalised form, close to how the rate file itself is shaped:

  c4_files            one row per rate file
  c4_refs             one row per provider reference (provider_group_id), with its network tag
  c4_ref_members      reference -> (NPI, TIN) pairs, one row per NPI (npi NULL for TIN-only groups)
  c4_refset_members   a "refset" is the list of references one negotiated_rates entry points at
  c4_refset_stats     price rows per refset and how many are capitation (all codes, for network evidence)
  c4_codes            billing code descriptions
  c4_prices           one row per negotiated_prices entry for an in-scope code
  c4_pairs            (NPI, TIN) -> signature: the set of references the pair belongs to
  c4_sig_rates        the clean rates, one per signature x code x variant

Rates are picked once per signature, not once per NPI: every NPI that sits in exactly the same
references gets exactly the same prices, so the work grows with the number of provider groups,
not with NPIs x codes. The fan-out to NPIs happens only when rows are written (view c4_rates).
"""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

DEFAULT_DB = Path("out/rates.sqlite")

SCHEMA = """
CREATE TABLE IF NOT EXISTS c4_files (
    file_id INTEGER PRIMARY KEY, url TEXT, local_path TEXT, last_updated_on TEXT, version TEXT,
    reporting_entity_name TEXT, needs_network_filter INTEGER, c2_network_label TEXT,
    c2_ppo_network_names TEXT, c3_score REAL, c3_grade TEXT, status TEXT, error TEXT,
    items INTEGER, items_in_scope INTEGER, price_rows INTEGER, seconds REAL);
CREATE TABLE IF NOT EXISTS c4_refs (
    file_id INTEGER, ref_id TEXT, network_name TEXT, n_groups INTEGER, source TEXT,
    tag TEXT, tag_confidence TEXT, ppo_evidence REAL, non_ppo_evidence REAL, evidence TEXT,
    PRIMARY KEY (file_id, ref_id));
CREATE TABLE IF NOT EXISTS c4_ref_members (
    file_id INTEGER, ref_id TEXT, npi INTEGER, tin TEXT, tin_type TEXT);
CREATE TABLE IF NOT EXISTS c4_refset_members (
    file_id INTEGER, refset_id INTEGER, ref_id TEXT, PRIMARY KEY (file_id, refset_id, ref_id));
CREATE TABLE IF NOT EXISTS c4_refset_stats (
    file_id INTEGER, refset_id INTEGER, size INTEGER, rows INTEGER, capitation_rows INTEGER);
CREATE TABLE IF NOT EXISTS c4_codes (
    code_type TEXT, code TEXT, description TEXT, PRIMARY KEY (code_type, code));
CREATE TABLE IF NOT EXISTS c4_prices (
    file_id INTEGER, refset_id INTEGER, code_type TEXT, code TEXT, arrangement TEXT,
    negotiated_type TEXT, rate REAL, expiration_date TEXT, billing_class TEXT, setting TEXT,
    pos TEXT, modifier TEXT);
CREATE TABLE IF NOT EXISTS c4_pairs (
    file_id INTEGER, npi INTEGER, tin TEXT, tin_type TEXT, sig_id INTEGER, PRIMARY KEY (file_id, npi, tin));
CREATE TABLE IF NOT EXISTS c4_sig_members (file_id INTEGER, sig_id INTEGER, ref_id TEXT);
CREATE TABLE IF NOT EXISTS c4_sig_rates (
    file_id INTEGER, sig_id INTEGER, code_type TEXT, code TEXT, negotiated_type TEXT,
    billing_class TEXT, setting TEXT, pos TEXT, modifier TEXT, rate REAL, expiration_date TEXT,
    network_tag TEXT, network_confidence TEXT, candidates INTEGER, distinct_rates INTEGER,
    rate_min REAL, rate_max REAL, pick_rule TEXT);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS ix_ref_members ON c4_ref_members (file_id, ref_id);
CREATE INDEX IF NOT EXISTS ix_refset_by_ref ON c4_refset_members (file_id, ref_id);
CREATE INDEX IF NOT EXISTS ix_prices_refset ON c4_prices (file_id, refset_id);
CREATE INDEX IF NOT EXISTS ix_refset_stats ON c4_refset_stats (file_id, refset_id);
CREATE INDEX IF NOT EXISTS ix_sig_members ON c4_sig_members (file_id, sig_id);
CREATE INDEX IF NOT EXISTS ix_sig_rates ON c4_sig_rates (file_id, sig_id);
CREATE INDEX IF NOT EXISTS ix_pairs_sig ON c4_pairs (file_id, sig_id);
"""

# One row per NPI x TIN x code x variant: the signature rates fanned out to every pair.
RATES_VIEW = """
CREATE VIEW IF NOT EXISTS c4_rates AS
SELECT p.npi, p.tin, p.tin_type, r.*
FROM c4_pairs p JOIN c4_sig_rates r ON r.file_id = p.file_id AND r.sig_id = p.sig_id;
"""


def connect(path: str | Path = DEFAULT_DB, fast: bool = True) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=60)
    if fast:  # the store is rebuilt from the rate files on every run, so durability is not needed
        con.execute("PRAGMA journal_mode=OFF")
        con.execute("PRAGMA synchronous=OFF")
        con.execute("PRAGMA temp_store=FILE")
        con.execute("PRAGMA cache_size=-200000")
    return con


def init_c4(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)


def finish_c4(con: sqlite3.Connection) -> None:
    con.executescript(INDEXES + RATES_VIEW)
    con.commit()


def drop_c4(con: sqlite3.Connection) -> None:
    for (name, kind) in con.execute("SELECT name, type FROM sqlite_master WHERE name LIKE 'c4_%' "
                                    "AND type IN ('table', 'view')").fetchall():
        con.execute(f"DROP {kind.upper()} IF EXISTS {name}")
    con.commit()


def stable_id(*parts) -> int:
    """A signed 64-bit id from the parts: the same input gives the same id in every process."""
    h = hashlib.blake2b("\x1f".join(map(str, parts)).encode(), digest_size=8).digest()
    return int.from_bytes(h, "big", signed=True)


def table_exists(con: sqlite3.Connection, name: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (name,)).fetchone() is not None
