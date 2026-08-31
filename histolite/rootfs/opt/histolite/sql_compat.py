"""
HistoLite - Compatibilità multi-backend (SQLite / PostgreSQL / MariaDB)

Il resto del codice è scritto con la sintassi SQLite (placeholder ``?``,
funzioni ``datetime()/date()/strftime()``, ``CAST(... AS REAL)`` permissivo,
``typeof()``). Questo modulo:

* traduce quelle query verso il dialetto del backend attivo (`translate_sql`);
* fornisce un wrapper di connessione (`CompatConnection`) con la stessa
  interfaccia minima usata nel codice: ``execute()/executemany()/cursor()/
  commit()/rollback()/close()`` e context manager, con righe restituite come
  ``dict`` (accesso sia per chiave sia, dove serve, compatibile con
  ``dict(row)``);
* elenca le classi d'eccezione "operational" per ogni backend
  (`db_error_types`).

Per SQLite non viene toccato nulla: `translate_sql` è l'identità e la
connessione resta quella nativa di ``sqlite3``.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any, Iterable, Sequence


# Regex "è un numero" senza il carattere '?' (usiamo {0,1}) così può essere
# emessa nella query tradotta senza interferire con la sostituzione dei
# placeholder.
_NUMERIC_RE = r"^[+-]{0,1}[0-9]*[.]{0,1}[0-9]+([eE][+-]{0,1}[0-9]+){0,1}$"


def _pg_rewrites() -> list[tuple[re.Pattern[str], str]]:
    return [
        # strftime -> to_char  (consuma i '%' prima dell'escaping)
        (re.compile(r"strftime\(\s*'%Y-%m-%d %H'\s*,\s*([\w.]+)\s*\)"),
         r"to_char((\1)::timestamp, 'YYYY-MM-DD HH24')"),
        (re.compile(r"strftime\(\s*'%Y-%m-%d'\s*,\s*([\w.]+)\s*\)"),
         r"to_char((\1)::timestamp, 'YYYY-MM-DD')"),
        # date(datetime(col,'unixepoch')) -> giorno (UTC) da epoch
        (re.compile(r"date\(\s*datetime\(\s*([\w.]+)\s*,\s*'unixepoch'\s*\)\s*\)"),
         r"to_char(to_timestamp(\1) AT TIME ZONE 'UTC', 'YYYY-MM-DD')"),
        # datetime(col,'unixepoch') / datetime(?,'unixepoch') -> timestamp (UTC) da epoch
        (re.compile(r"datetime\(\s*\?\s*,\s*'unixepoch'\s*\)"),
         r"(to_timestamp(?) AT TIME ZONE 'UTC')"),
        (re.compile(r"datetime\(\s*([\w.]+)\s*,\s*'unixepoch'\s*\)"),
         r"(to_timestamp(\1) AT TIME ZONE 'UTC')"),
        # datetime('now','-N days') -> NOW() - INTERVAL
        (re.compile(r"datetime\(\s*'now'\s*,\s*'-(\d+) days'\s*\)"),
         r"(NOW() - INTERVAL '\1 days')"),
        # datetime('now', ? || ' days')  (param es. "-90")
        (re.compile(r"datetime\(\s*'now'\s*,\s*\?\s*\|\|\s*' days'\s*\)"),
         r"(NOW() + make_interval(days => (?)::int))"),
        # date(col) / datetime(col)
        (re.compile(r"\bdate\(\s*([\w.]+)\s*\)"), r"(\1)::date"),
        (re.compile(r"\bdatetime\(\s*([\w.]+)\s*\)"), r"(\1)::timestamp"),
        # CAST(col / N AS INTEGER)  (troncamento verso il basso, come SQLite)
        (re.compile(r"CAST\(\s*([\w.]+)\s*/\s*(\d+)\s+AS\s+INTEGER\s*\)"),
         r"FLOOR(\1 / \2)"),
        # typeof(CAST(col AS REAL)) = 'real'  -> test "è numerico"
        (re.compile(r"typeof\(\s*CAST\(\s*([\w.]+)\s+AS\s+REAL\s*\)\s*\)\s*=\s*'real'"),
         r"((\1)::text ~ '" + _NUMERIC_RE + r"')"),
        # CAST(col AS REAL) permissivo -> NULL se non numerico
        (re.compile(r"CAST\(\s*([\w.]+)\s+AS\s+REAL\s*\)"),
         r"(CASE WHEN (\1)::text ~ '" + _NUMERIC_RE
         + r"' THEN (\1)::double precision ELSE NULL END)"),
    ]


def _mariadb_rewrites() -> list[tuple[re.Pattern[str], str]]:
    return [
        (re.compile(r"strftime\(\s*'%Y-%m-%d %H'\s*,\s*([\w.]+)\s*\)"),
         r"DATE_FORMAT(\1, '%Y-%m-%d %H')"),
        (re.compile(r"strftime\(\s*'%Y-%m-%d'\s*,\s*([\w.]+)\s*\)"),
         r"DATE_FORMAT(\1, '%Y-%m-%d')"),
        (re.compile(r"date\(\s*datetime\(\s*([\w.]+)\s*,\s*'unixepoch'\s*\)\s*\)"),
         r"DATE(FROM_UNIXTIME(\1))"),
        (re.compile(r"datetime\(\s*\?\s*,\s*'unixepoch'\s*\)"), r"FROM_UNIXTIME(?)"),
        (re.compile(r"datetime\(\s*([\w.]+)\s*,\s*'unixepoch'\s*\)"),
         r"FROM_UNIXTIME(\1)"),
        (re.compile(r"datetime\(\s*'now'\s*,\s*'-(\d+) days'\s*\)"),
         r"(NOW() - INTERVAL \1 DAY)"),
        (re.compile(r"datetime\(\s*'now'\s*,\s*\?\s*\|\|\s*' days'\s*\)"),
         r"(NOW() + INTERVAL ? DAY)"),
        (re.compile(r"\bdate\(\s*([\w.]+)\s*\)"), r"DATE(\1)"),
        (re.compile(r"\bdatetime\(\s*([\w.]+)\s*\)"), r"(\1)"),
        (re.compile(r"CAST\(\s*([\w.]+)\s*/\s*(\d+)\s+AS\s+INTEGER\s*\)"),
         r"FLOOR(\1 / \2)"),
        (re.compile(r"typeof\(\s*CAST\(\s*([\w.]+)\s+AS\s+REAL\s*\)\s*\)\s*=\s*'real'"),
         r"((\1) REGEXP '" + _NUMERIC_RE + r"')"),
        (re.compile(r"CAST\(\s*([\w.]+)\s+AS\s+REAL\s*\)"),
         r"(CASE WHEN (\1) REGEXP '" + _NUMERIC_RE
         + r"' THEN CAST(\1 AS DOUBLE) ELSE NULL END)"),
    ]


_REWRITES = {
    "postgresql": _pg_rewrites(),
    "mariadb": _mariadb_rewrites(),
}


def translate_sql(sql: str, db_type: str) -> str:
    """Adatta una query in sintassi SQLite al dialetto ``db_type``.

    Per ``sqlite`` restituisce la stringa invariata.
    """
    if db_type == "sqlite":
        return sql
    rewrites = _REWRITES.get(db_type)
    if not rewrites:
        return sql
    for pattern, repl in rewrites:
        sql = pattern.sub(repl, sql)
    # Placeholder: da qmark (?) a pyformat (%s). Prima protegge eventuali '%'
    # letterali rimasti (es. DATE_FORMAT di MariaDB).
    sql = sql.replace("%", "%%").replace("?", "%s")
    return sql


def db_error_types(db_type: str) -> tuple[type[BaseException], ...]:
    """Classi d'eccezione da trattare come errori 'operational' recuperabili."""
    errors: list[type[BaseException]] = [sqlite3.OperationalError, sqlite3.DatabaseError]
    if db_type == "postgresql":
        try:
            import psycopg
            errors.append(psycopg.Error)
        except ImportError:
            pass
    elif db_type == "mariadb":
        try:
            import mysql.connector
            errors.append(mysql.connector.Error)
        except ImportError:
            pass
    return tuple(errors)


class _CompatResult:
    """Wrapper del cursore: normalizza fetchone/fetchall e chiude il cursore."""

    def __init__(self, cursor: Any):
        self._cursor = cursor

    def fetchone(self):
        if self._cursor is None:
            return None
        try:
            return self._cursor.fetchone()
        finally:
            self._safe_close()

    def fetchall(self) -> list:
        if self._cursor is None:
            return []
        try:
            return list(self._cursor.fetchall())
        finally:
            self._safe_close()

    def _safe_close(self) -> None:
        try:
            self._cursor.close()
        except Exception:
            pass
        self._cursor = None


class CompatConnection:
    """Interfaccia minima uniforme su una connessione PostgreSQL/MariaDB.

    Espone lo stesso sottoinsieme di API usato nel codice con una
    ``sqlite3.Connection`` (``execute``, ``executemany``, ``cursor``,
    ``commit``, ``rollback``, ``close`` e context manager). Le query passano
    per :func:`translate_sql`; le righe sono ``dict``.
    """

    _READ_PREFIXES = ("select", "with", "show", "pragma", "explain", "values")

    def __init__(self, raw, db_type: str, cursor_kwargs: dict | None = None):
        self._raw = raw
        self._db_type = db_type
        self._cursor_kwargs = cursor_kwargs or {}

    # -- API usata dal resto del codice ---------------------------------
    def execute(self, sql: str, params: Sequence | None = None) -> _CompatResult:
        tsql = translate_sql(sql, self._db_type)
        cur = self._raw.cursor(**self._cursor_kwargs)
        try:
            if params:
                cur.execute(tsql, tuple(params))
            else:
                cur.execute(tsql)
        except Exception:
            try:
                cur.close()
            except Exception:
                pass
            raise
        if sql.lstrip()[:7].lower().startswith(self._READ_PREFIXES):
            return _CompatResult(cur)
        try:
            cur.close()
        except Exception:
            pass
        return _CompatResult(None)

    def executemany(self, sql: str, seq_of_params: Iterable[Sequence]) -> None:
        tsql = translate_sql(sql, self._db_type)
        cur = self._raw.cursor(**self._cursor_kwargs)
        try:
            cur.executemany(tsql, [tuple(p) for p in seq_of_params])
        finally:
            try:
                cur.close()
            except Exception:
                pass

    def cursor(self):
        """Cursore grezzo del driver (righe come dict). Le query eseguite
        qui NON passano per translate_sql: usarlo solo per SQL già scritto
        nel dialetto del backend."""
        return self._raw.cursor(**self._cursor_kwargs)

    def commit(self) -> None:
        self._raw.commit()

    def rollback(self) -> None:
        try:
            self._raw.rollback()
        except Exception:
            pass

    def close(self) -> None:
        try:
            self._raw.close()
        except Exception:
            pass

    # -- context manager ----------------------------------------------
    def __enter__(self) -> "CompatConnection":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None:
                try:
                    self._raw.commit()
                except Exception:
                    pass
            else:
                self.rollback()
        finally:
            self.close()
