"""Test per i nuovi parametri delle strategie:
- fasce generiche del Purge Adattivo (+ migrazione legacy)
- bucket a dimensione arbitraria e aggregazioni scelte
- deduplica con preservazione periodica configurabile
"""

import os
import sys
import tempfile
import sqlite3
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "rootfs", "opt", "histolite"))

import strategies as st
from database import HaDatabase, _aggregate_bucket, _resolve_bucket_seconds


# ---------------------------------------------------------------------------
# Helper di parsing
# ---------------------------------------------------------------------------

def test_parse_interval_to_seconds():
    assert st.parse_interval_to_seconds({"every": 15, "unit": "minute"}, 3600) == 900
    assert st.parse_interval_to_seconds({"every": 2, "unit": "hour"}, 3600) == 7200
    assert st.parse_interval_to_seconds({"every": 1, "unit": "week"}, 3600) == 604800
    assert st.parse_interval_to_seconds({"every": 0, "unit": "day"}, 86400) == 0
    assert st.parse_interval_to_seconds("hour", 3600) == 3600
    assert st.parse_interval_to_seconds("day", 3600) == 86400
    assert st.parse_interval_to_seconds(None, 1234) == 1234
    assert st.parse_interval_to_seconds("900", 3600) == 900


def test_resolve_bucket_seconds():
    assert _resolve_bucket_seconds(900, None, 3600) == 900
    assert _resolve_bucket_seconds(None, "day", 3600) == 86400
    assert _resolve_bucket_seconds(None, "hour", 3600) == 3600
    assert _resolve_bucket_seconds(None, None, 3600) == 3600
    assert _resolve_bucket_seconds(0, None, 3600) == 3600  # 0 non valido -> default


def test_normalize_tiers_passthrough():
    tiers = st.normalize_tiers({"tiers": [
        {"after_days": 30, "action": "flatten", "bucket": {"every": 2, "unit": "hour"}, "agg": "median"},
        {"after_days": 10, "action": "delete"},
    ]})
    assert [t["after_days"] for t in tiers] == [10, 30]          # ordinate
    assert tiers[1] == {"after_days": 30, "action": "flatten",
                        "bucket_seconds": 7200, "agg": "median", "agg_pct": 95.0}


def test_normalize_tiers_legacy_migration():
    # Come li mandava la vecchia UI: threshold_4_days = 36500 -> nessuna fascia delete
    tiers = st.normalize_tiers({
        "threshold_1_days": 7, "threshold_2_days": 30,
        "threshold_3_days": 365, "threshold_4_days": 36500,
    })
    assert [(t["after_days"], t["action"], t.get("bucket_seconds")) for t in tiers] == [
        (7, "flatten", 3600), (30, "flatten", 86400),
    ]
    # Config molto vecchia senza threshold_4_days -> delete a threshold_3
    tiers2 = st.normalize_tiers({"threshold_1_days": 7, "threshold_2_days": 30, "threshold_3_days": 90})
    assert tiers2[-1] == {"after_days": 90, "action": "delete"}


# ---------------------------------------------------------------------------
# Aggregazioni
# ---------------------------------------------------------------------------

def _rows(*pairs):
    return [{"state": str(s), "ts": t} for s, t in pairs]


def test_aggregate_bucket_numeric():
    rows = _rows((10, 0), (20, 10), (30, 20), (40, 30))
    assert _aggregate_bucket(rows, "mean") == "25"
    assert _aggregate_bucket(rows, "median") == "25"
    assert _aggregate_bucket(rows, "min") == "10"
    assert _aggregate_bucket(rows, "max") == "40"
    assert _aggregate_bucket(rows, "first") == "10"
    assert _aggregate_bucket(rows, "last") == "40"
    # percentile 75 di [10,20,30,40] con interpolazione lineare = 32.5
    assert _aggregate_bucket(rows, "percentile", 75) == "32.5"
    # time_weighted_mean: pesi 10,10,10,1 -> (100+200+300+40)/31
    assert _aggregate_bucket(rows, "time_weighted_mean") == "20.6452"


def test_aggregate_bucket_mode_and_non_numeric():
    assert _aggregate_bucket(_rows(("on", 1), ("on", 2), ("off", 3)), "mode") == "on"
    # aggregazione numerica su stati testuali -> None (nessun aggiornamento)
    assert _aggregate_bucket(_rows(("on", 1), ("off", 2)), "mean") is None
    # stati non numerici ignorati da 'mode'
    assert _aggregate_bucket(_rows(("unavailable", 1), ("unknown", 2)), "mode") is None


# ---------------------------------------------------------------------------
# End-to-end su SQLite in-memory con schema moderno
# ---------------------------------------------------------------------------

def _make_modern_db(with_attrs=False):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE states ("
        "state_id INTEGER PRIMARY KEY, entity_id TEXT, state TEXT, metadata_id INTEGER, "
        "last_updated_ts REAL, old_state_id INTEGER, attributes_id INTEGER)"
    )
    conn.execute("CREATE TABLE states_meta (metadata_id INTEGER PRIMARY KEY, entity_id TEXT)")
    conn.execute("INSERT INTO states_meta VALUES (1, 'sensor.x'), (2, 'sensor.y')")
    if with_attrs:
        conn.execute("CREATE TABLE state_attributes (attributes_id INTEGER PRIMARY KEY, shared_attrs TEXT)")
        conn.executemany("INSERT INTO state_attributes VALUES (?, '{}')", [(100,), (101,), (200,)])
    conn.commit()
    conn.close()
    return path


def _orphan_attr_count(path):
    conn = sqlite3.connect(path)
    n = conn.execute(
        "SELECT COUNT(*) FROM state_attributes WHERE attributes_id NOT IN "
        "(SELECT attributes_id FROM states WHERE attributes_id IS NOT NULL)"
    ).fetchone()[0]
    conn.close()
    return n


def _insert(path, points):
    conn = sqlite3.connect(path)
    conn.executemany(
        "INSERT INTO states (entity_id, state, metadata_id, last_updated_ts) VALUES (NULL, ?, 1, ?)",
        points,
    )
    conn.commit()
    conn.close()


def _states(path, where=""):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        f"SELECT state, last_updated_ts FROM states WHERE metadata_id = 1 {where} ORDER BY last_updated_ts"
    ).fetchall()]
    conn.close()
    return rows


def test_flatten_entity_bucket_and_agg():
    path = _make_modern_db()
    try:
        old = int(time.time()) - 40 * 86400
        old -= old % 900  # allinea a un confine di bucket da 900s
        # 2 bucket da 900s: [10,12,14,16] e [20,22,24,26]
        pts = [(str(v), old + i * 225) for i, v in enumerate([10, 12, 14, 16, 20, 22, 24, 26])]
        _insert(path, pts)
        db = HaDatabase(path)

        dry = db.flatten_entity("sensor.x", older_than_days=1, bucket_seconds=900,
                                agg="median", dry_run=True)
        # niente chiave "deleted" in dry-run (contratto usato da AdaptivePurge)
        assert dry["estimated_deleted"] == 6 and dry.get("deleted", 0) == 0

        res = db.flatten_entity("sensor.x", older_than_days=1, bucket_seconds=900, agg="median")
        assert res["deleted"] == 6
        remaining = _states(path)
        assert [r["state"] for r in remaining] == ["13", "23"]  # mediane dei due bucket
    finally:
        os.remove(path)


def test_deduplicate_keep_interval():
    path = _make_modern_db()
    try:
        old = time.time() - 40 * 86400
        # stesso valore '5' ogni 10 min per ~3 ore
        _insert(path, [("5", old + i * 600) for i in range(18)])
        db = HaDatabase(path)

        # keep_interval 1h -> resta ~1 record per fascia oraria (>=3, <18)
        res = db.deduplicate_entity("sensor.x", older_than_days=1, keep_interval_seconds=3600)
        left = len(_states(path))
        assert 3 <= left <= 5 and res["deleted"] == 18 - left

        # deduplica pura -> un solo record
        db.deduplicate_entity("sensor.x", older_than_days=1, keep_interval_seconds=0)
        assert len(_states(path)) == 1
    finally:
        os.remove(path)


def test_peak_decimate_non_max_agg():
    path = _make_modern_db()
    try:
        old = int(time.time()) - 40 * 86400
        old -= old % 3600  # tutti i punti in un solo bucket orario
        _insert(path, [(str(v), old + i * 200) for i, v in enumerate([1, 2, 3, 4, 5, 6])])
        db = HaDatabase(path)
        res = db.peak_decimate_entity("sensor.x", older_than_days=1, bucket_seconds=3600,
                                      agg="mean", keep_resets=False)
        assert res["deleted"] == 5
        assert len(_states(path)) == 1
        assert _states(path)[0]["state"] == "3.5"  # media di 1..6
    finally:
        os.remove(path)


def test_orphan_attributes_targeted_cleanup():
    path = _make_modern_db(with_attrs=True)
    try:
        old = int(time.time()) - 40 * 86400
        old -= old % 900
        conn = sqlite3.connect(path)
        # attr 100 condiviso da molte righe; attr 101 usato da UNA sola riga vecchia
        for i in range(8):
            conn.execute(
                "INSERT INTO states (state, metadata_id, last_updated_ts, attributes_id) "
                "VALUES (?, 1, ?, ?)",
                (str(10 + i), old + i * 225, 101 if i == 3 else 100),
            )
        # attr 200: solo su sensor.y -> non deve essere toccato
        conn.execute("INSERT INTO states (state, metadata_id, last_updated_ts, attributes_id) "
                     "VALUES ('y', 2, ?, 200)", (old,))
        conn.commit()
        conn.close()

        db = HaDatabase(path)
        dry = db.flatten_entity("sensor.x", older_than_days=1, bucket_seconds=900,
                                agg="median", dry_run=True)
        assert dry["attr_estimated"] == 1                       # solo attr 101

        res = db.flatten_entity("sensor.x", older_than_days=1, bucket_seconds=900, agg="median")
        assert res["attr_deleted"] == 1
        assert _orphan_attr_count(path) == 0

        conn = sqlite3.connect(path)
        left = sorted(r[0] for r in conn.execute("SELECT attributes_id FROM state_attributes"))
        conn.close()
        assert left == [100, 200]                               # 101 rimossa, 200 intatta
    finally:
        os.remove(path)


def test_orphan_attributes_opt_out():
    path = _make_modern_db(with_attrs=True)
    try:
        old = int(time.time()) - 40 * 86400
        conn = sqlite3.connect(path)
        for i in range(6):
            conn.execute(
                "INSERT INTO states (state, metadata_id, last_updated_ts, attributes_id) "
                "VALUES (?, 1, ?, ?)",
                (str(i), old + i * 300, 101 if i == 2 else 100),
            )
        conn.commit()
        conn.close()
        db = HaDatabase(path)
        db.purge_entity("sensor.x", older_than_days=1, cleanup_attributes=False)
        conn = sqlite3.connect(path)
        left = {r[0] for r in conn.execute("SELECT attributes_id FROM state_attributes")}
        conn.close()
        # cleanup disattivato: 100 e 101 restano anche se ora orfane
        assert {100, 101} <= left
    finally:
        os.remove(path)


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("strategies tests passed")
