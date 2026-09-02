import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "rootfs", "opt", "histolite"))

from sql_compat import CompatConnection, translate_sql


# Query rappresentative prese dal codice (sintassi SQLite).
CORPUS = [
    "SELECT COUNT(*) AS c FROM states s WHERE s.metadata_id = ? AND s.last_updated_ts < ?",
    "SELECT sm.entity_id, COUNT(*) AS record_count FROM states s "
    "INNER JOIN states_meta sm ON s.metadata_id = sm.metadata_id "
    "WHERE sm.entity_id LIKE ? GROUP BY sm.entity_id ORDER BY record_count DESC LIMIT ?",
    "SELECT date(datetime(last_updated_ts, 'unixepoch')) AS day, COUNT(*) AS count "
    "FROM states WHERE metadata_id = ? AND last_updated_ts >= ? GROUP BY day ORDER BY day ASC",
    "SELECT state, last_updated_ts AS ts FROM states WHERE metadata_id = ? "
    "ORDER BY last_updated_ts DESC LIMIT ?",
    "SELECT MIN(CAST(s.state AS REAL)) AS obs_min FROM states s WHERE s.metadata_id = ? "
    "AND s.state NOT IN ('unknown','unavailable','') AND typeof(CAST(s.state AS REAL)) = 'real'",
    "CAST(last_updated_ts / 3600 AS INTEGER) * 3600",
    # Bucket a dimensione arbitraria (Purge Adattivo / Picco per Bucket).
    "SELECT state_id, CAST(last_updated_ts / 900 AS INTEGER) * 900 AS bucket, state, "
    "last_updated_ts AS ts FROM states WHERE metadata_id = ? AND last_updated_ts < ? "
    "ORDER BY last_updated_ts ASC, state_id ASC",
    # Bucket-key della deduplica periodica (senza il fattore di ricomposizione).
    "CAST(last_updated_ts / 604800 AS INTEGER)",
    "SELECT state_id FROM states s WHERE s.metadata_id = ? AND datetime(last_updated) < "
    "datetime('now', '-30 days') LIMIT ?",
    "UPDATE states SET state = ? WHERE state_id = ?",
    "DELETE FROM states WHERE state_id IN (?,?,?)",
    "conditions.append(\"datetime(last_updated) >= datetime(?, 'unixepoch')\")",
]


def test_sqlite_is_identity():
    for q in CORPUS:
        assert translate_sql(q, "sqlite") == q


def test_no_qmark_left_and_placeholder_count_matches():
    for backend in ("postgresql", "mariadb"):
        for q in CORPUS:
            out = translate_sql(q, backend)
            assert "?" not in out, (backend, out)
            # ogni '?' originale diventa un '%s'
            assert out.count("%s") == q.count("?"), (backend, q, out)


def test_pg_translations_shape():
    pg = translate_sql(
        "SELECT date(datetime(last_updated_ts, 'unixepoch')) AS day FROM states WHERE x < ?",
        "postgresql",
    )
    assert "to_timestamp(last_updated_ts) AT TIME ZONE 'UTC'" in pg
    assert "date(" not in pg and "datetime(" not in pg

    pg2 = translate_sql("CAST(last_updated_ts / 86400 AS INTEGER) * 86400", "postgresql")
    assert pg2 == "FLOOR(last_updated_ts / 86400) * 86400"

    # Bucket arbitrari: stessa regola, denominatore qualsiasi.
    assert translate_sql("CAST(last_updated_ts / 900 AS INTEGER) * 900", "postgresql") \
        == "FLOOR(last_updated_ts / 900) * 900"
    assert translate_sql("CAST(last_updated_ts / 604800 AS INTEGER)", "mariadb") \
        == "FLOOR(last_updated_ts / 604800)"

    pg3 = translate_sql("WHERE typeof(CAST(state AS REAL)) = 'real'", "postgresql")
    assert "typeof" not in pg3 and "::text ~ '" in pg3


class _FakeCursor:
    def __init__(self, rows):
        self._rows = list(rows)
        self.executed = None
        self.closed = False

    def execute(self, sql, params=None):
        self.executed = (sql, params)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def close(self):
        self.closed = True


class _FakeRaw:
    def __init__(self, rows):
        self._rows = rows
        self.committed = False
        self.rolled_back = False
        self.closed = False
        self.last_cursor = None

    def cursor(self, **kw):
        self.last_cursor = _FakeCursor(self._rows)
        return self.last_cursor

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


def test_compat_connection_read_and_commit():
    raw = _FakeRaw([{"c": 7}])
    with CompatConnection(raw, "postgresql") as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM states WHERE x = ?", (1,)).fetchone()
        assert row["c"] == 7
        # '?' tradotto in '%s' prima di arrivare al driver
        assert raw.last_cursor.executed[0].endswith("x = %s")
        assert raw.last_cursor.executed[1] == (1,)
        assert raw.last_cursor.closed  # il cursore viene chiuso dopo il fetch
    assert raw.committed and raw.closed


def test_compat_connection_rolls_back_on_error():
    raw = _FakeRaw([])
    try:
        with CompatConnection(raw, "postgresql"):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert raw.rolled_back and raw.closed and not raw.committed


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("sql_compat tests passed")
