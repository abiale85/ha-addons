"""Test per la pagina Utility: salute statistiche + entità orfane / ri-associazione."""

import os
import sys
import tempfile
import sqlite3
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "rootfs", "opt", "histolite"))

from database import HaDatabase


def _make_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    c = sqlite3.connect(path)
    c.execute(
        "CREATE TABLE states (state_id INTEGER PRIMARY KEY, entity_id TEXT, state TEXT, "
        "metadata_id INTEGER, last_updated_ts REAL, old_state_id INTEGER, attributes_id INTEGER)"
    )
    c.execute("CREATE TABLE states_meta (metadata_id INTEGER PRIMARY KEY, entity_id TEXT)")
    c.execute(
        "CREATE TABLE statistics_meta (id INTEGER PRIMARY KEY, statistic_id TEXT, source TEXT, "
        "unit_of_measurement TEXT, has_mean INTEGER, has_sum INTEGER, name TEXT)"
    )
    for tbl in ("statistics", "statistics_short_term"):
        c.execute(
            f"CREATE TABLE {tbl} (id INTEGER PRIMARY KEY, created_ts REAL, metadata_id INTEGER, "
            f"start_ts REAL, mean REAL, min REAL, max REAL, last_reset_ts REAL, state REAL, sum REAL)"
        )
    c.commit()
    c.close()
    return path


def _sql(path, q, params=()):
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute(q, params).fetchall()]
    c.close()
    return rows


def _scalar(path, q, params=()):
    return list(_sql(path, q, params)[0].values())[0]


# ---------------------------------------------------------------------------
# Salute statistiche
# ---------------------------------------------------------------------------

def _seed_stats_problems(path):
    now = time.time()
    c = sqlite3.connect(path)
    c.execute("INSERT INTO states_meta VALUES (1, 'sensor.live')")
    c.execute("INSERT INTO statistics_meta VALUES (1, 'sensor.live', 'recorder', 'C', 1, 0, NULL)")
    c.execute("INSERT INTO statistics_meta VALUES (5, 'sensor.gone', 'recorder', 'C', 1, 0, NULL)")
    c.execute("INSERT INTO statistics_meta VALUES (9, 'sensor.ext', 'mqtt', NULL, 0, 1, NULL)")
    # righe valide
    for i in range(4):
        c.execute("INSERT INTO statistics (metadata_id, start_ts, mean) VALUES (1, ?, ?)", (1000 + i * 3600, i))
    # duplicati (1, 100) x3  -> 2 extra
    for m in (1, 2, 3):
        c.execute("INSERT INTO statistics (metadata_id, start_ts, mean) VALUES (1, 100, ?)", (m,))
    # riga orfana (nessuna meta 999)
    c.execute("INSERT INTO statistics (metadata_id, start_ts, mean) VALUES (999, 7000, 1)")
    c.execute("INSERT INTO statistics_short_term (metadata_id, start_ts, mean) VALUES (999, 7000, 1)")
    # righe della meta orfana 5
    c.execute("INSERT INTO statistics (metadata_id, start_ts, mean) VALUES (5, 8000, 1)")
    c.execute("INSERT INTO statistics (metadata_id, start_ts, mean) VALUES (5, 8100, 1)")
    # riga futura
    c.execute("INSERT INTO statistics (metadata_id, start_ts, mean) VALUES (1, ?, 1)", (now + 10 * 86400,))
    c.commit()
    c.close()


def test_check_statistics_health():
    path = _make_db()
    try:
        _seed_stats_problems(path)
        h = HaDatabase(path).check_statistics_health()
        assert h["available"] is True
        assert h["orphan_rows"]["statistics"] == 1
        assert h["orphan_rows"]["statistics_short_term"] == 1
        assert [m["statistic_id"] for m in h["orphan_meta"]] == ["sensor.gone"]
        assert h["orphan_meta"][0]["rows_statistics"] == 2
        assert h["duplicates"]["statistics"] == {"groups": 1, "extra_rows": 2}
        assert h["future_rows"]["statistics"] == 1
    finally:
        os.remove(path)


def test_repair_statistics_dry_then_apply():
    path = _make_db()
    try:
        _seed_stats_problems(path)
        db = HaDatabase(path)
        actions = ["orphan_rows", "orphan_meta", "duplicates", "future_rows"]

        before = _scalar(path, "SELECT COUNT(*) FROM statistics")
        dry = db.repair_statistics(actions, dry_run=True)
        assert dry["dry_run"] is True
        assert dry["actions"]["orphan_rows"]["statistics"] == 1
        assert dry["actions"]["duplicates"]["statistics"] == 2
        assert dry["actions"]["future_rows"]["statistics"] == 1
        assert dry["actions"]["orphan_meta"]["meta"] == 1
        assert _scalar(path, "SELECT COUNT(*) FROM statistics") == before  # niente scritture

        db.repair_statistics(actions, dry_run=False)
        h = db.check_statistics_health()
        assert h["orphan_rows"] == {"statistics": 0, "statistics_short_term": 0}
        assert h["orphan_meta"] == []
        assert h["duplicates"]["statistics"]["extra_rows"] == 0
        assert h["future_rows"]["statistics"] == 0
        # la meta orfana è stata rimossa, quelle valide restano
        assert _scalar(path, "SELECT COUNT(*) FROM statistics_meta WHERE id = 5") == 0
        assert _scalar(path, "SELECT COUNT(*) FROM statistics_meta") == 2
    finally:
        os.remove(path)


# ---------------------------------------------------------------------------
# Entità orfane
# ---------------------------------------------------------------------------

def test_list_orphan_entities():
    path = _make_db()
    try:
        now = int(time.time())
        c = sqlite3.connect(path)
        c.execute("INSERT INTO states_meta VALUES (1,'sensor.foo'),(2,'sensor.foo_2'),(3,'sensor.only_old')")
        for i in range(5):  # sensor.foo: fermo da 90 giorni
            c.execute("INSERT INTO states (state,metadata_id,last_updated_ts) VALUES ('1',1,?)", (now - 90 * 86400 - i * 60,))
        for i in range(5):  # sensor.foo_2: attivo
            c.execute("INSERT INTO states (state,metadata_id,last_updated_ts) VALUES ('2',2,?)", (now - i * 60,))
        for i in range(5):  # sensor.only_old: fermo da 90 giorni, nessun sostituto
            c.execute("INSERT INTO states (state,metadata_id,last_updated_ts) VALUES ('9',3,?)", (now - 90 * 86400 - i * 60,))
        c.commit()
        c.close()
        db = HaDatabase(path)

        r = db.list_orphan_entities(inactive_days=30)
        by_id = {o["entity_id"]: o for o in r["orphans"]}
        assert set(by_id) == {"sensor.foo", "sensor.only_old"}
        assert by_id["sensor.foo"]["status"] == "inactive"
        assert by_id["sensor.foo"]["suggested_target"] == "sensor.foo_2"
        assert by_id["sensor.only_old"]["suggested_target"] is None

        # con whitelist: sensor.foo assente -> 'removed'
        r2 = db.list_orphan_entities(inactive_days=3650, valid_entity_ids=["sensor.foo_2"])
        st = {o["entity_id"]: o["status"] for o in r2["orphans"]}
        assert st["sensor.foo"] == "removed"
    finally:
        os.remove(path)


# ---------------------------------------------------------------------------
# Ri-associazione (rename + merge)
# ---------------------------------------------------------------------------

def test_merge_entity_history_rename():
    path = _make_db()
    try:
        c = sqlite3.connect(path)
        c.execute("INSERT INTO states_meta VALUES (1,'sensor.vecchio')")
        for i in range(6):
            c.execute("INSERT INTO states (state,metadata_id,last_updated_ts) VALUES ('1',1,?)", (1000 + i,))
        c.execute("INSERT INTO statistics_meta VALUES (1,'sensor.vecchio','recorder','C',1,0,NULL)")
        c.execute("INSERT INTO statistics (metadata_id,start_ts,mean) VALUES (1,1,1)")
        c.commit(); c.close()

        r = HaDatabase(path).merge_entity_history("sensor.vecchio", "sensor.nuovo")
        assert r["mode"] == "rename" and r["states_moved"] == 6
        assert _scalar(path, "SELECT entity_id FROM states_meta WHERE metadata_id = 1") == "sensor.nuovo"
        assert _scalar(path, "SELECT statistic_id FROM statistics_meta WHERE id = 1") == "sensor.nuovo"
    finally:
        os.remove(path)


def test_merge_entity_history_merge_with_collision():
    path = _make_db()
    try:
        c = sqlite3.connect(path)
        c.execute("INSERT INTO states_meta VALUES (1,'sensor.rotto'),(2,'sensor.nuovo')")
        for i in range(8):
            c.execute("INSERT INTO states (state,metadata_id,last_updated_ts) VALUES ('s',1,?)", (100 + i,))
        for i in range(3):
            c.execute("INSERT INTO states (state,metadata_id,last_updated_ts) VALUES ('n',2,?)", (900 + i,))
        c.execute("INSERT INTO statistics_meta VALUES (1,'sensor.rotto','recorder','C',1,0,NULL)")
        c.execute("INSERT INTO statistics_meta VALUES (2,'sensor.nuovo','recorder','C',1,0,NULL)")
        # sorgente: start_ts 10,20,30 ; target: 20,30,40  -> collisione su 20 e 30
        for ts in (10, 20, 30):
            c.execute("INSERT INTO statistics (metadata_id,start_ts,mean) VALUES (1,?,1)", (ts,))
        for ts in (20, 30, 40):
            c.execute("INSERT INTO statistics (metadata_id,start_ts,mean) VALUES (2,?,9)", (ts,))
        c.commit(); c.close()
        db = HaDatabase(path)

        dry = db.merge_entity_history("sensor.rotto", "sensor.nuovo", dry_run=True)
        assert dry["mode"] == "merge"
        assert dry["states_moved"] == 8
        assert dry["stats_moved"] == 1 and dry["stats_dropped_collision"] == 2
        # dry-run non tocca nulla
        assert _scalar(path, "SELECT COUNT(*) FROM states WHERE metadata_id = 1") == 8

        r = db.merge_entity_history("sensor.rotto", "sensor.nuovo", dry_run=False)
        assert r["source_meta_removed"] is True
        assert _scalar(path, "SELECT COUNT(*) FROM states WHERE metadata_id = 1") == 0
        assert _scalar(path, "SELECT COUNT(*) FROM states WHERE metadata_id = 2") == 11
        assert _scalar(path, "SELECT COUNT(*) FROM states_meta WHERE entity_id = 'sensor.rotto'") == 0
        # statistiche: target conserva le sue 3 righe + 1 non collidente dalla sorgente (start_ts 10)
        assert _scalar(path, "SELECT COUNT(*) FROM statistics WHERE metadata_id = 2") == 4
        assert _scalar(path, "SELECT COUNT(*) FROM statistics_meta WHERE id = 1") == 0
        # la riga collidente start_ts=20 mantiene il valore del target (9), non della sorgente (1)
        assert _scalar(path, "SELECT mean FROM statistics WHERE metadata_id = 2 AND start_ts = 20") == 9
    finally:
        os.remove(path)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("utility tests passed")
