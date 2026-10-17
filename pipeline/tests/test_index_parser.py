import csv
import gzip
import io
import json

from ptd_pipeline.index_parser import (
    OUTPUT_COLUMNS,
    export_rate_files,
    export_table,
    init_db,
    insert_rows,
    iter_index_rows,
    open_local,
)

TOC = {
    "reporting_entity_name": "Test Plan Co",
    "reporting_entity_type": "health insurance issuer",
    "reporting_structure": [
        {
            "reporting_plans": [
                {"plan_name": "Gold PPO", "plan_id_type": "HIOS", "plan_id": "11111CA001", "plan_market_type": "group"},
                {"plan_name": "Silver PPO", "plan_id_type": "HIOS", "plan_id": "11111CA002", "plan_market_type": "group"},
            ],
            "in_network_files": [
                {"description": "in-network a", "location": "https://x.test/a.json.gz"},
                {"description": "in-network b", "location": "https://x.test/b.json.gz"},
            ],
            "allowed_amount_file": {"description": "oon", "location": "https://x.test/oon.json"},
        },
        {
            # Same plan + same file repeated in another structure -> duplicate row
            "reporting_plans": [
                {"plan_name": "Gold PPO", "plan_id_type": "HIOS", "plan_id": "11111CA001", "plan_market_type": "group"},
            ],
            "in_network_files": [{"description": "dup", "location": "https://x.test/a.json.gz"}],
        },
        {
            # No in-network files -> contributes nothing
            "reporting_plans": [{"plan_name": "OON only", "plan_id": "9", "plan_market_type": "individual"}],
            "allowed_amount_file": {"location": "https://x.test/oon2.json"},
        },
    ],
}


def _stream(obj):
    return io.BytesIO(json.dumps(obj).encode("utf-8"))


def test_flattening_yields_plan_by_file_rows():
    rows = list(iter_index_rows(_stream(TOC), "Ambetter"))
    assert len(rows) == 5  # 2 plans x 2 files + 1 duplicate
    assert ("Ambetter", "Gold PPO", "11111CA001", "group", "https://x.test/a.json.gz") in rows
    assert all("oon" not in r[4] for r in rows)  # allowed-amount files are not rate files


def test_dedup_and_export(tmp_path):
    conn = init_db(tmp_path / "t.sqlite")
    seen, inserted = insert_rows(conn, iter_index_rows(_stream(TOC), "Ambetter"), "idx1")
    # The same index listed again (e.g. parent + sub-index overlap) adds nothing new
    seen2, inserted2 = insert_rows(conn, iter_index_rows(_stream(TOC), "Ambetter"), "idx2")
    assert (seen, inserted) == (5, 4)
    assert (seen2, inserted2) == (5, 0)

    out = tmp_path / "table.csv"
    assert export_table(conn, out) == 4
    with open(out, newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        assert tuple(next(reader)) == OUTPUT_COLUMNS
        assert len(list(reader)) == 4

    rate_out = tmp_path / "rates.csv"
    assert export_rate_files(conn, rate_out) == 2
    with open(rate_out, newline="", encoding="utf-8") as fh:
        rows = {r["rate file URL"]: int(r["plan count"]) for r in csv.DictReader(fh)}
    assert rows == {"https://x.test/a.json.gz": 2, "https://x.test/b.json.gz": 2}
    conn.close()


def test_gzipped_index_is_detected(tmp_path):
    path = tmp_path / "idx.json"  # gzip content without a .gz suffix
    path.write_bytes(gzip.compress(json.dumps(TOC).encode("utf-8")))
    with open_local(path) as fh:
        assert len(list(iter_index_rows(fh, "Fidelis"))) == 5
