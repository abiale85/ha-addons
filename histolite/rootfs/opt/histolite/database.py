"""
HistoLite - Gestione database Home Assistant
Connessione, analisi e operazioni su home-assistant_v2.db (SQLite)
"""

import sqlite3
import os
import shutil
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


class HaDatabase:
    """Gestisce la connessione e le operazioni sul database HA."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._schema = None

    # ------------------------------------------------------------------
    # Connessioni
    # ------------------------------------------------------------------

    def _connect(self, read_only: bool = False) -> sqlite3.Connection:
        if not os.path.exists(self.db_path):
            raise FileNotFoundError(f"Database non trovato: {self.db_path}")
        if read_only:
            uri = f"file:{self.db_path}?mode=ro"
        else:
            uri = f"file:{self.db_path}"
        conn = sqlite3.connect(uri, uri=True, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        # Limita cache SQLite: 512 pagine × 4KB = 2MB massimo per connessione
        conn.execute("PRAGMA cache_size = -512")
        # Nessuna memory-mapped I/O (riduce RSS in modo significativo)
        conn.execute("PRAGMA mmap_size = 0")
        # Temp store su file invece che in RAM
        conn.execute("PRAGMA temp_store = FILE")
        if not read_only:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            # WAL checkpoint automatico ogni 500 pagine (evita crescita illimitata)
            conn.execute("PRAGMA wal_autocheckpoint = 500")
            try:
                conn.execute("CREATE INDEX IF NOT EXISTS idx_entity_id ON states(entity_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_last_updated_ts ON states(last_updated_ts) WHERE last_updated_ts IS NOT NULL")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_last_updated ON states(last_updated) WHERE last_updated IS NOT NULL")
            except Exception as e:
                logger.debug(f"Index creation: {e}")
                try:
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_states_meta_entity_id ON states_meta(entity_id)")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_states_metadata_id ON states(metadata_id) WHERE metadata_id IS NOT NULL")
                except Exception as e:
                    logger.debug(f"states_meta index creation: {e}")
    def get_schema_info(self) -> dict:
        """Rileva la versione dello schema del database HA."""
        if self._schema:
            return self._schema
        with self._connect(read_only=True) as conn:
            cur = conn.execute("PRAGMA table_info(states)")
            cols = {row["name"] for row in cur.fetchall()}
            uses_ts = "last_updated_ts" in cols
            has_attributes_id = "attributes_id" in cols
            # Nuovo schema HA (recorder >= 23): entity_id spostato in states_meta
            has_metadata = "metadata_id" in cols
            has_entity_id_in_states = "entity_id" in cols
            self._schema = {
                "uses_ts": uses_ts,
                "has_attributes_id": has_attributes_id,
                "has_metadata": has_metadata,
                "has_entity_id_in_states": has_entity_id_in_states,
                "timestamp_col": "last_updated_ts" if uses_ts else "last_updated",
                "columns": list(cols),
            }
        return self._schema

    def _ts_filter(self, alias: str, older_than_days: int) -> tuple[str, float | str]:
        """Restituisce (condizione SQL, valore parametro) per filtrare per età."""
        schema = self.get_schema_info()
        if schema["uses_ts"]:
            import time
            cutoff = time.time() - older_than_days * 86400
            return (f"{alias}.last_updated_ts < ?", cutoff)
        else:
            return (
                f"datetime({alias}.last_updated) < datetime('now', '-{older_than_days} days')",
                None,
            )

    def _get_metadata_id(self, conn: sqlite3.Connection, entity_id: str) -> Optional[int]:
        """Nuovo schema HA: risolve entity_id → metadata_id tramite states_meta."""
        try:
            row = conn.execute(
                "SELECT metadata_id FROM states_meta WHERE entity_id = ? LIMIT 1",
                (entity_id,),
            ).fetchone()
            return row["metadata_id"] if row else None
        except sqlite3.OperationalError:
            return None

    def _use_meta_schema(self) -> bool:
        """True se il DB usa il nuovo schema con states_meta (entity_id separato)."""
        schema = self.get_schema_info()
        return schema.get("has_metadata", False) and not schema.get("has_entity_id_in_states", True)

    # ------------------------------------------------------------------
    # Statistiche generali
    # ------------------------------------------------------------------

    def get_db_size(self) -> int:
        """Dimensione del file DB in bytes."""
        return os.path.getsize(self.db_path)

    def get_table_counts(self) -> dict:
        """Conteggio righe nelle principali tabelle."""
        tables = ["states", "state_attributes", "events", "statistics", "statistics_short_term"]
        result = {}
        with self._connect(read_only=True) as conn:
            for table in tables:
                try:
                    row = conn.execute(f"SELECT COUNT(*) as c FROM {table}").fetchone()
                    result[table] = row["c"] if row else 0
                except sqlite3.OperationalError:
                    result[table] = 0
        return result

    def get_recorder_runs(self) -> list[dict]:
        """Ultime run del recorder HA."""
        with self._connect(read_only=True) as conn:
            try:
                rows = conn.execute(
                    "SELECT * FROM recorder_runs ORDER BY start DESC LIMIT 5"
                ).fetchall()
                return [dict(r) for r in rows]
            except sqlite3.OperationalError:
                return []

    # ------------------------------------------------------------------
    # Analisi sensori
    # ------------------------------------------------------------------

    def get_top_sensors(
        self, limit: int = 100, sort_by: str = "count", search: str = ""
    ) -> list[dict]:
        """Restituisce i sensori con più stati registrati. Timeout: 5 sec."""
        schema = self.get_schema_info()
        if schema["uses_ts"]:
            min_ts_expr = "MIN(s.last_updated_ts)"
            max_ts_expr = "MAX(s.last_updated_ts)"
        else:
            min_ts_expr = "MIN(s.last_updated)"
            max_ts_expr = "MAX(s.last_updated)"

        order = "record_count DESC" if sort_by != "entity" else "entity_id ASC"
        search_pattern = f"%{search}%" if search else ""

        logger.debug(f"get_top_sensors: limit={limit}, sort={sort_by}, search={search}")

        if self._use_meta_schema():
            # Nuovo schema HA: entity_id in states_meta
            where_clause = "WHERE sm.entity_id LIKE ?" if search else ""
            query = f"""
                SELECT
                    sm.entity_id,
                    COUNT(*) AS record_count,
                    {min_ts_expr} AS first_seen,
                    {max_ts_expr} AS last_seen
                FROM states s
                INNER JOIN states_meta sm ON s.metadata_id = sm.metadata_id
                {where_clause}
                GROUP BY sm.entity_id
                ORDER BY {order}
                LIMIT ?
            """
        else:
            # Schema legacy: entity_id direttamente in states
            where_clause = "WHERE entity_id LIKE ?" if search else ""
            query = f"""
                SELECT
                    entity_id,
                    COUNT(*) AS record_count,
                    {min_ts_expr.replace('s.', '')} AS first_seen,
                    {max_ts_expr.replace('s.', '')} AS last_seen
                FROM states s
                {where_clause}
                GROUP BY entity_id
                ORDER BY {order}
                LIMIT ?
            """

        with self._connect(read_only=True) as conn:
            conn.execute("PRAGMA busy_timeout=5000")
            try:
                if search:
                    rows = conn.execute(query, (search_pattern, limit)).fetchall()
                else:
                    rows = conn.execute(query, (limit,)).fetchall()
                results = [dict(r) for r in rows]
                logger.info(f"get_top_sensors: {len(results)} sensori trovati")
                return results
            except sqlite3.OperationalError as e:
                if "timeout" in str(e).lower():
                    logger.warning(f"get_top_sensors: timeout (query troppo pesante)")
                else:
                    logger.error(f"get_top_sensors query error: {e}", exc_info=True)
                return []
            except Exception as e:
                logger.error(f"get_top_sensors query error: {e}", exc_info=True)
                return []

    def get_entity_list(self, search: str = "") -> list[dict]:
        """Lista veloce SENZA calcoli - solo entity_id.
        Usato per caricamento rapido pagina sensori.
        Timeout: 5 sec."""
        search_pattern = f"%{search}%" if search else ""

        if self._use_meta_schema():
            # Nuovo schema HA: entity_id in states_meta
            where_clause = "WHERE sm.entity_id LIKE ?" if search else ""
            query = f"""
                SELECT DISTINCT sm.entity_id
                FROM states_meta sm
                INNER JOIN states s ON s.metadata_id = sm.metadata_id
                {where_clause}
                ORDER BY sm.entity_id
                LIMIT 1000
            """
        else:
            where_clause = "WHERE entity_id LIKE ?" if search else ""
            query = f"""
                SELECT DISTINCT entity_id
                FROM states
                {where_clause}
                ORDER BY entity_id
                LIMIT 1000
            """

        logger.debug(f"get_entity_list: search={search}")
        with self._connect(read_only=True) as conn:
            conn.execute("PRAGMA busy_timeout=5000")
            try:
                if search:
                    rows = conn.execute(query, (search_pattern,)).fetchall()
                else:
                    rows = conn.execute(query).fetchall()
                results = [{"entity_id": r["entity_id"]} for r in rows]
                logger.info(f"get_entity_list: {len(results)} entità trovate")
                return results
            except sqlite3.OperationalError as e:
                if "timeout" in str(e).lower():
                    logger.warning(f"get_entity_list: timeout")
                else:
                    logger.error(f"get_entity_list error: {e}", exc_info=True)
                return []
            except Exception as e:
                logger.error(f"get_entity_list error: {e}", exc_info=True)
                return []

    def get_sensor_stats(self, entity_id: str) -> Optional[dict]:
        """Statistiche dettagliate per un singolo sensore. Timeout: 5 sec."""
        schema = self.get_schema_info()
        use_meta = self._use_meta_schema()

        if use_meta:
            if schema["uses_ts"]:
                query = """
                    SELECT
                        sm.entity_id,
                        COUNT(*) AS record_count,
                        MIN(s.last_updated_ts) AS first_ts,
                        MAX(s.last_updated_ts) AS last_ts,
                        AVG(CASE WHEN CAST(s.state AS REAL) != 0 OR s.state = '0'
                                  THEN 1 ELSE NULL END) AS numeric_ratio
                    FROM states s
                    INNER JOIN states_meta sm ON s.metadata_id = sm.metadata_id
                    WHERE sm.entity_id = ?
                    GROUP BY sm.entity_id
                """
            else:
                query = """
                    SELECT
                        sm.entity_id,
                        COUNT(*) AS record_count,
                        MIN(s.last_updated) AS first_ts,
                        MAX(s.last_updated) AS last_ts,
                        NULL AS numeric_ratio
                    FROM states s
                    INNER JOIN states_meta sm ON s.metadata_id = sm.metadata_id
                    WHERE sm.entity_id = ?
                    GROUP BY sm.entity_id
                """
        else:
            if schema["uses_ts"]:
                query = """
                    SELECT
                        entity_id,
                        COUNT(*) AS record_count,
                        MIN(last_updated_ts) AS first_ts,
                        MAX(last_updated_ts) AS last_ts,
                        AVG(CASE WHEN CAST(state AS REAL) != 0 OR state = '0'
                                  THEN 1 ELSE NULL END) AS numeric_ratio
                    FROM states
                    WHERE entity_id = ?
                    GROUP BY entity_id
                """
            else:
                query = """
                    SELECT
                        entity_id,
                        COUNT(*) AS record_count,
                        MIN(last_updated) AS first_ts,
                        MAX(last_updated) AS last_ts,
                        NULL AS numeric_ratio
                    FROM states
                    WHERE entity_id = ?
                    GROUP BY entity_id
                """

        with self._connect(read_only=True) as conn:
            conn.execute("PRAGMA busy_timeout=5000")
            try:
                row = conn.execute(query, (entity_id,)).fetchone()
                if not row:
                    return None
                result = dict(row)
                # Campiona ultime 50 righe per determinare se è numerico
                if use_meta:
                    meta_id = self._get_metadata_id(conn, entity_id)
                    if meta_id is None:
                        return None
                    sample = conn.execute(
                        "SELECT state FROM states WHERE metadata_id = ? "
                        "ORDER BY rowid DESC LIMIT 50",
                        (meta_id,),
                    ).fetchall()
                else:
                    sample = conn.execute(
                        "SELECT state FROM states WHERE entity_id = ? "
                        "ORDER BY rowid DESC LIMIT 50",
                        (entity_id,),
                    ).fetchall()
                numeric_count = 0
                for s in sample:
                    try:
                        float(s["state"])
                        numeric_count += 1
                    except (ValueError, TypeError):
                        pass
                result["is_numeric"] = numeric_count > len(sample) * 0.6
                return result
            except sqlite3.OperationalError as e:
                if "timeout" in str(e).lower():
                    logger.warning(f"get_sensor_stats {entity_id}: timeout")
                    return None
                raise

    def get_sensor_daily_counts(self, entity_id: str, days: int = 90) -> list[dict]:
        """Conta i record per giorno per un sensore (per il grafico). Timeout: 5 sec."""
        schema = self.get_schema_info()
        use_meta = self._use_meta_schema()

        with self._connect(read_only=True) as conn:
            conn.execute("PRAGMA busy_timeout=5000")
            try:
                if use_meta:
                    meta_id = self._get_metadata_id(conn, entity_id)
                    if meta_id is None:
                        return []
                    if schema["uses_ts"]:
                        import time
                        cutoff = time.time() - days * 86400
                        query = """
                            SELECT
                                date(datetime(last_updated_ts, 'unixepoch')) AS day,
                                COUNT(*) AS count
                            FROM states
                            WHERE metadata_id = ? AND last_updated_ts >= ?
                            GROUP BY day
                            ORDER BY day ASC
                        """
                        rows = conn.execute(query, (meta_id, cutoff)).fetchall()
                    else:
                        query = """
                            SELECT
                                date(last_updated) AS day,
                                COUNT(*) AS count
                            FROM states
                            WHERE metadata_id = ?
                              AND datetime(last_updated) >= datetime('now', ? || ' days')
                            GROUP BY day
                            ORDER BY day ASC
                        """
                        rows = conn.execute(query, (meta_id, f"-{days}")).fetchall()
                else:
                    if schema["uses_ts"]:
                        import time
                        cutoff = time.time() - days * 86400
                        query = """
                            SELECT
                                date(datetime(last_updated_ts, 'unixepoch')) AS day,
                                COUNT(*) AS count
                            FROM states
                            WHERE entity_id = ? AND last_updated_ts >= ?
                            GROUP BY day
                            ORDER BY day ASC
                        """
                        rows = conn.execute(query, (entity_id, cutoff)).fetchall()
                    else:
                        query = """
                            SELECT
                                date(last_updated) AS day,
                                COUNT(*) AS count
                            FROM states
                            WHERE entity_id = ?
                              AND datetime(last_updated) >= datetime('now', ? || ' days')
                            GROUP BY day
                            ORDER BY day ASC
                        """
                        rows = conn.execute(query, (entity_id, f"-{days}")).fetchall()
                return [dict(r) for r in rows]
            except sqlite3.OperationalError as e:
                if "timeout" in str(e).lower():
                    logger.warning(f"get_sensor_daily_counts {entity_id}: timeout")
                    return []
                raise

    def get_recent_values(self, entity_id: str, limit: int = 20) -> list[dict]:
        """Ultime N righe per un sensore. Timeout: 5 sec."""
        schema = self.get_schema_info()
        order_col = "last_updated_ts" if schema["uses_ts"] else "last_updated"
        use_meta = self._use_meta_schema()
        with self._connect(read_only=True) as conn:
            conn.execute("PRAGMA busy_timeout=5000")
            try:
                if use_meta:
                    meta_id = self._get_metadata_id(conn, entity_id)
                    if meta_id is None:
                        return []
                    rows = conn.execute(
                        f"SELECT state, {order_col} AS ts FROM states "
                        f"WHERE metadata_id = ? ORDER BY {order_col} DESC LIMIT ?",
                        (meta_id, limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        f"SELECT state, {order_col} AS ts FROM states "
                        f"WHERE entity_id = ? ORDER BY {order_col} DESC LIMIT ?",
                        (entity_id, limit),
                    ).fetchall()
                return [dict(r) for r in rows]
            except sqlite3.OperationalError as e:
                if "timeout" in str(e).lower():
                    logger.warning(f"get_recent_values {entity_id}: timeout")
                    return []
                raise

    # ------------------------------------------------------------------
    # Operazioni di purge
    # ------------------------------------------------------------------

    def backup_db(self, backup_path: str) -> str:
        """Crea un backup del database."""
        os.makedirs(backup_path, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = os.path.join(backup_path, f"home-assistant_v2_backup_{ts}.db")
        shutil.copy2(self.db_path, dest)
        logger.info(f"Backup creato: {dest}")
        return dest

    def purge_entity(
        self,
        entity_id: str,
        older_than_days: int,
        dry_run: bool = False,
        batch_size: int = 5000,
    ) -> dict:
        """
        Elimina tutti i record di un'entità più vecchi di N giorni.
        Restituisce statistiche sull'operazione.
        """
        schema = self.get_schema_info()
        use_meta = self._use_meta_schema()
        cond, param = self._ts_filter("s", older_than_days)

        with self._connect(read_only=dry_run) as conn:
            if use_meta:
                meta_id = self._get_metadata_id(conn, entity_id)
                if meta_id is None:
                    return {"deleted": 0, "estimated": 0, "dry_run": dry_run}
                id_filter = "s.metadata_id = ?"
                id_param = meta_id
            else:
                id_filter = "s.entity_id = ?"
                id_param = entity_id

            count_query = f"SELECT COUNT(*) AS c FROM states s WHERE {id_filter} AND {cond}"
            params_count = [id_param]
            if param is not None:
                params_count.append(param)

            count_row = conn.execute(count_query, params_count).fetchone()
            total_to_delete = count_row["c"] if count_row else 0

            if dry_run or total_to_delete == 0:
                return {"deleted": 0, "estimated": total_to_delete, "dry_run": dry_run}

            deleted = 0
            while True:
                if schema["uses_ts"]:
                    ids = conn.execute(
                        f"SELECT state_id FROM states s "
                        f"WHERE {id_filter} AND last_updated_ts < ? "
                        f"LIMIT ?",
                        (id_param, param, batch_size),
                    ).fetchall()
                else:
                    ids = conn.execute(
                        f"SELECT state_id FROM states s "
                        f"WHERE {id_filter} AND datetime(last_updated) < "
                        f"datetime('now', '-{older_than_days} days') "
                        f"LIMIT ?",
                        (id_param, batch_size),
                    ).fetchall()

                if not ids:
                    break
                id_list = [r["state_id"] for r in ids]
                placeholders = ",".join("?" * len(id_list))
                conn.execute(
                    f"UPDATE states SET old_state_id = NULL "
                    f"WHERE old_state_id IN ({placeholders})",
                    id_list,
                )
                conn.execute(
                    f"DELETE FROM states WHERE state_id IN ({placeholders})", id_list
                )
                conn.commit()
                deleted += len(id_list)

            return {"deleted": deleted, "estimated": total_to_delete, "dry_run": False}

    # ------------------------------------------------------------------
    # Operazioni di flatten (appiattimento)
    # ------------------------------------------------------------------

    def flatten_entity(
        self,
        entity_id: str,
        older_than_days: int,
        granularity: str = "hour",  # "hour" o "day"
        dry_run: bool = False,
        batch_size: int = 5000,
    ) -> dict:
        """
        Appiattisce la storia di un'entità: per ogni bucket temporale
        mantiene un solo record con la media dei valori.
        """
        schema = self.get_schema_info()
        use_meta = self._use_meta_schema()

        with self._connect(read_only=dry_run) as conn:
            if use_meta:
                meta_id = self._get_metadata_id(conn, entity_id)
                if meta_id is None:
                    return {"total_records": 0, "buckets": 0, "estimated_deleted": 0, "deleted": 0, "dry_run": dry_run}
                id_filter = "metadata_id = ?"
                id_param = meta_id
            else:
                id_filter = "entity_id = ?"
                id_param = entity_id

            # Formato bucket in SQL
            if schema["uses_ts"]:
                import time
                cutoff = time.time() - older_than_days * 86400
                if granularity == "hour":
                    bucket_expr = "CAST(last_updated_ts / 3600 AS INTEGER) * 3600"
                else:
                    bucket_expr = "CAST(last_updated_ts / 86400 AS INTEGER) * 86400"
                where_clause = f"{id_filter} AND last_updated_ts < ?"
                base_params = (id_param, cutoff)
            else:
                if granularity == "hour":
                    bucket_expr = "strftime('%Y-%m-%d %H', last_updated)"
                else:
                    bucket_expr = "strftime('%Y-%m-%d', last_updated)"
                cutoff = None
                where_clause = (
                    f"{id_filter} AND datetime(last_updated) < "
                    f"datetime('now', '-{older_than_days} days')"
                )
                base_params = (id_param,)

            count_query = f"SELECT COUNT(*) AS c FROM states WHERE {where_clause}"
            bucket_query = f"""
                SELECT
                    {bucket_expr} AS bucket,
                    MIN(state_id) AS keep_id,
                    COUNT(*) AS bucket_count,
                    AVG(CASE
                        WHEN state NOT IN ('unknown','unavailable','')
                             AND CAST(state AS REAL) IS NOT NULL
                        THEN CAST(state AS REAL)
                        ELSE NULL END) AS avg_value
                FROM states
                WHERE {where_clause}
                GROUP BY bucket
                HAVING COUNT(*) > 1
            """

            count_row = conn.execute(count_query, base_params).fetchone()
            total_records = count_row["c"] if count_row else 0
            buckets = conn.execute(bucket_query, base_params).fetchall()
            estimated_deleted = sum(b["bucket_count"] - 1 for b in buckets)

            if dry_run:
                return {
                    "total_records": total_records,
                    "buckets": len(buckets),
                    "estimated_deleted": estimated_deleted,
                    "dry_run": True,
                }

            deleted = 0
            for bucket in buckets:
                keep_id = bucket["keep_id"]
                avg_val = bucket["avg_value"]

                if avg_val is not None:
                    try:
                        avg_str = f"{avg_val:.4f}".rstrip("0").rstrip(".")
                        conn.execute(
                            "UPDATE states SET state = ? WHERE state_id = ?",
                            (avg_str, keep_id),
                        )
                    except Exception as e:
                        logger.warning(f"Errore aggiornamento stato: {e}")

                if schema["uses_ts"]:
                    to_delete = conn.execute(
                        f"SELECT state_id FROM states "
                        f"WHERE {id_filter} AND last_updated_ts < ? "
                        f"AND {bucket_expr} = ? AND state_id != ?",
                        (id_param, cutoff, bucket["bucket"], keep_id),
                    ).fetchall()
                else:
                    to_delete = conn.execute(
                        f"SELECT state_id FROM states "
                        f"WHERE {id_filter} "
                        f"AND datetime(last_updated) < datetime('now', '-{older_than_days} days') "
                        f"AND {bucket_expr} = ? AND state_id != ?",
                        (id_param, bucket["bucket"], keep_id),
                    ).fetchall()

                if not to_delete:
                    continue

                id_list = [r["state_id"] for r in to_delete]
                placeholders = ",".join("?" * len(id_list))
                conn.execute(
                    f"UPDATE states SET old_state_id = NULL "
                    f"WHERE old_state_id IN ({placeholders})",
                    id_list,
                )
                conn.execute(
                    f"DELETE FROM states WHERE state_id IN ({placeholders})", id_list
                )
                deleted += len(id_list)

            conn.commit()
            return {
                "total_records": total_records,
                "buckets": len(buckets),
                "estimated_deleted": estimated_deleted,
                "deleted": deleted,
                "dry_run": False,
            }

    # ------------------------------------------------------------------
    # Purge statistics_short_term
    # ------------------------------------------------------------------

    def get_statistics_short_term_stats(self) -> list[dict]:
        """Conta i record in statistics_short_term per entità."""
        with self._connect(read_only=True) as conn:
            try:
                rows = conn.execute("""
                    SELECT
                        sm.statistic_id AS entity_id,
                        COUNT(sst.id) AS record_count,
                        MIN(sst.start_ts) AS first_ts,
                        MAX(sst.start_ts) AS last_ts
                    FROM statistics_short_term sst
                    JOIN statistics_metadata sm ON sst.metadata_id = sm.id
                    GROUP BY sm.statistic_id
                    ORDER BY record_count DESC
                """).fetchall()
                return [dict(r) for r in rows]
            except sqlite3.OperationalError:
                return []

    def purge_statistics_short_term(
        self, older_than_days: int, entity_ids: Optional[list] = None, dry_run: bool = False
    ) -> dict:
        """Elimina record da statistics_short_term più vecchi di N giorni."""
        import time
        cutoff = time.time() - older_than_days * 86400

        with self._connect(read_only=dry_run) as conn:
            try:
                if entity_ids:
                    placeholders = ",".join("?" * len(entity_ids))
                    meta_ids = conn.execute(
                        f"SELECT id FROM statistics_metadata "
                        f"WHERE statistic_id IN ({placeholders})",
                        entity_ids,
                    ).fetchall()
                    meta_id_list = [r["id"] for r in meta_ids]
                    if not meta_id_list:
                        return {"deleted": 0, "dry_run": dry_run}
                    ph2 = ",".join("?" * len(meta_id_list))
                    count_row = conn.execute(
                        f"SELECT COUNT(*) AS c FROM statistics_short_term "
                        f"WHERE metadata_id IN ({ph2}) AND start_ts < ?",
                        meta_id_list + [cutoff],
                    ).fetchone()
                    estimated = count_row["c"] if count_row else 0
                    if not dry_run and estimated > 0:
                        conn.execute(
                            f"DELETE FROM statistics_short_term "
                            f"WHERE metadata_id IN ({ph2}) AND start_ts < ?",
                            meta_id_list + [cutoff],
                        )
                        conn.commit()
                else:
                    count_row = conn.execute(
                        "SELECT COUNT(*) AS c FROM statistics_short_term WHERE start_ts < ?",
                        (cutoff,),
                    ).fetchone()
                    estimated = count_row["c"] if count_row else 0
                    if not dry_run and estimated > 0:
                        conn.execute(
                            "DELETE FROM statistics_short_term WHERE start_ts < ?", (cutoff,)
                        )
                        conn.commit()
                return {"deleted": estimated if not dry_run else 0, "estimated": estimated, "dry_run": dry_run}
            except sqlite3.OperationalError as e:
                logger.error(f"Errore purge statistics_short_term: {e}")
                return {"deleted": 0, "error": str(e), "dry_run": dry_run}

    # ------------------------------------------------------------------
    # Manutenzione
    # ------------------------------------------------------------------

    def cleanup_orphaned_attributes(self, dry_run: bool = False) -> dict:
        """Elimina righe orfane da state_attributes."""
        with self._connect(read_only=dry_run) as conn:
            try:
                count_row = conn.execute("""
                    SELECT COUNT(*) AS c FROM state_attributes
                    WHERE attributes_id NOT IN (
                        SELECT attributes_id FROM states WHERE attributes_id IS NOT NULL
                    )
                """).fetchone()
                estimated = count_row["c"] if count_row else 0
                if not dry_run and estimated > 0:
                    conn.execute("""
                        DELETE FROM state_attributes
                        WHERE attributes_id NOT IN (
                            SELECT attributes_id FROM states WHERE attributes_id IS NOT NULL
                        )
                    """)
                    conn.commit()
                return {"deleted": estimated if not dry_run else 0, "estimated": estimated, "dry_run": dry_run}
            except sqlite3.OperationalError:
                return {"deleted": 0, "estimated": 0, "dry_run": dry_run}

    def run_vacuum(self) -> bool:
        """Esegue VACUUM per recuperare spazio su disco."""
        try:
            with self._connect(read_only=False) as conn:
                conn.execute("VACUUM")
            return True
        except Exception as e:
            logger.error(f"Errore VACUUM: {e}")
            return False

    def cleanup_null_entities(self, dry_run: bool = False) -> dict:
        """
        Conta (dry_run) o elimina i record dalla tabella states corrotti:
        - Schema legacy: entity_id IS NULL o vuoto
        - Nuovo schema (states_meta): metadata_id IS NULL o orfano (non in states_meta)
        """
        use_meta = self._use_meta_schema()
        with self._connect(read_only=dry_run) as conn:
            try:
                if use_meta:
                    # Nel nuovo schema: record con metadata_id NULL o non in states_meta
                    count_row = conn.execute(
                        "SELECT COUNT(*) AS c FROM states "
                        "WHERE metadata_id IS NULL "
                        "OR metadata_id NOT IN (SELECT metadata_id FROM states_meta)"
                    ).fetchone()
                else:
                    count_row = conn.execute(
                        "SELECT COUNT(*) AS c FROM states "
                        "WHERE entity_id IS NULL OR entity_id = ''"
                    ).fetchone()

                count = count_row["c"] if count_row else 0

                if dry_run or count == 0:
                    return {"deleted": 0, "estimated": count, "dry_run": dry_run}

                if use_meta:
                    conn.execute(
                        "UPDATE states SET old_state_id = NULL "
                        "WHERE old_state_id IN ("
                        "  SELECT state_id FROM states "
                        "  WHERE metadata_id IS NULL "
                        "  OR metadata_id NOT IN (SELECT metadata_id FROM states_meta)"
                        ")"
                    )
                    conn.execute(
                        "DELETE FROM states "
                        "WHERE metadata_id IS NULL "
                        "OR metadata_id NOT IN (SELECT metadata_id FROM states_meta)"
                    )
                else:
                    conn.execute(
                        "UPDATE states SET old_state_id = NULL "
                        "WHERE old_state_id IN ("
                        "  SELECT state_id FROM states "
                        "  WHERE entity_id IS NULL OR entity_id = ''"
                        ")"
                    )
                    conn.execute(
                        "DELETE FROM states WHERE entity_id IS NULL OR entity_id = ''"
                    )
                conn.commit()
                logger.info(f"cleanup_null_entities: {count} record eliminati")
                return {"deleted": count, "estimated": count, "dry_run": False}
            except Exception as e:
                logger.error(f"cleanup_null_entities error: {e}", exc_info=True)
                return {"deleted": 0, "error": str(e), "dry_run": dry_run}

    # ------------------------------------------------------------------
    # Editing storia sensore
    # ------------------------------------------------------------------

    def get_sensor_values(
        self,
        entity_id: str,
        start_ts: Optional[float] = None,
        end_ts: Optional[float] = None,
        min_val: Optional[float] = None,
        max_val: Optional[float] = None,
        page: int = 1,
        per_page: int = 100,
    ) -> dict:
        """
        Restituisce i record di un sensore con filtri opzionali su
        timeframe e range di valori. Paginato. Timeout: 5 sec.
        """
        schema = self.get_schema_info()
        ts_col = "last_updated_ts" if schema["uses_ts"] else "last_updated"
        use_meta = self._use_meta_schema()
        offset = (page - 1) * per_page

        with self._connect(read_only=True) as conn:
            conn.execute("PRAGMA busy_timeout=5000")
            try:
                if use_meta:
                    meta_id = self._get_metadata_id(conn, entity_id)
                    if meta_id is None:
                        return {"total": 0, "page": page, "per_page": per_page, "pages": 1, "records": []}
                    conditions = ["metadata_id = ?"]
                    params: list = [meta_id]
                else:
                    conditions = ["entity_id = ?"]
                    params = [entity_id]

                # Filtro temporale
                if schema["uses_ts"]:
                    if start_ts is not None:
                        conditions.append("last_updated_ts >= ?")
                        params.append(start_ts)
                    if end_ts is not None:
                        conditions.append("last_updated_ts <= ?")
                        params.append(end_ts)
                else:
                    if start_ts is not None:
                        conditions.append("datetime(last_updated) >= datetime(?, 'unixepoch')")
                        params.append(start_ts)
                    if end_ts is not None:
                        conditions.append("datetime(last_updated) <= datetime(?, 'unixepoch')")
                        params.append(end_ts)

                # Filtro valore
                if min_val is not None:
                    conditions.append("CAST(state AS REAL) >= ? AND state NOT IN ('unknown','unavailable','')")
                    params.append(min_val)
                if max_val is not None:
                    conditions.append("CAST(state AS REAL) <= ? AND state NOT IN ('unknown','unavailable','')")
                    params.append(max_val)

                where = " AND ".join(conditions)
                count_query = f"SELECT COUNT(*) AS c FROM states WHERE {where}"
                data_query = f"""
                    SELECT state_id, state, {ts_col} AS ts
                    FROM states
                    WHERE {where}
                    ORDER BY ts DESC
                    LIMIT ? OFFSET ?
                """

                count_row = conn.execute(count_query, params).fetchone()
                total = count_row["c"] if count_row else 0
                rows = conn.execute(data_query, params + [per_page, offset]).fetchall()
                records = [dict(r) for r in rows]

                if not schema["uses_ts"]:
                    from datetime import datetime as _dt
                    for r in records:
                        if r["ts"] and isinstance(r["ts"], str):
                            try:
                                dt = _dt.fromisoformat(r["ts"].replace("Z", "+00:00"))
                                r["ts"] = dt.timestamp()
                            except Exception:
                                pass

                return {
                    "total": total,
                    "page": page,
                    "per_page": per_page,
                    "pages": max(1, (total + per_page - 1) // per_page),
                    "records": records,
                }
            except sqlite3.OperationalError as e:
                if "timeout" in str(e).lower():
                    logger.warning(f"get_sensor_values {entity_id}: timeout")
                    return {"total": 0, "page": 1, "per_page": per_page, "pages": 1, "records": [], "error": "timeout"}
                raise

    def delete_states_by_ids(self, state_ids: list[int]) -> int:
        """Elimina record specifici per state_id. Restituisce il numero eliminati."""
        if not state_ids:
            return 0
        with self._connect(read_only=False) as conn:
            placeholders = ",".join("?" * len(state_ids))
            # Annulla riferimenti old_state_id prima di eliminare
            conn.execute(
                f"UPDATE states SET old_state_id = NULL "
                f"WHERE old_state_id IN ({placeholders})",
                state_ids,
            )
            conn.execute(
                f"DELETE FROM states WHERE state_id IN ({placeholders})", state_ids
            )
            conn.commit()
            return len(state_ids)

    def preview_anomalies(self, entity_id: str, criteria: dict) -> dict:
        """
        Anteprima dei record anomali da eliminare. Non modifica nulla.
        Criteri supportati:
          - remove_negative: bool (elimina valori < 0)
          - min_value: float (elimina valori < min)
          - max_value: float (elimina valori > max)
          - std_dev_multiplier: float (elimina valori oltre N deviazioni std)
          - state_whitelist: list[str] (stati non numerici da ELIMINARE)
          - state_blacklist: list[str] (stati specifici da eliminare, es. 'unavailable')
        Timeout: 10 sec.
        """
        conditions, params = self._build_anomaly_conditions(entity_id, criteria)
        if not conditions:
            return {"count": 0, "samples": [], "error": "Nessun criterio valido specificato"}

        with self._connect(read_only=True) as conn:
            conn.execute("PRAGMA busy_timeout=10000")
            # Nuovo schema: antepone metadata_id filter
            if self._use_meta_schema():
                meta_id = self._get_metadata_id(conn, entity_id)
                if meta_id is None:
                    return {"count": 0, "samples": []}
                conditions = ["metadata_id = ?"] + conditions
                params = [meta_id] + params

            where = " AND ".join(conditions)
            query = f"""
                SELECT state_id, state
                FROM states
                WHERE {where}
                ORDER BY state_id DESC
                LIMIT 1000
            """
            count_q = f"SELECT COUNT(*) AS c FROM states WHERE {where}"
            try:
                count_row = conn.execute(count_q, params).fetchone()
                count = count_row["c"] if count_row else 0
                samples = [dict(r) for r in conn.execute(query, params).fetchall()[:20]]
                return {"count": count, "samples": samples}
            except sqlite3.OperationalError as e:
                if "timeout" in str(e).lower():
                    return {"count": -1, "samples": [], "error": "Timeout - usa filtri più restrittivi"}
                raise

    def delete_anomalies(self, entity_id: str, criteria: dict, batch_size: int = 5000) -> dict:
        """Elimina i record anomali secondo i criteri dati. Ritorna conteggio eliminati."""
        conditions, params = self._build_anomaly_conditions(entity_id, criteria)
        if not conditions:
            return {"deleted": 0, "error": "Nessun criterio valido"}

        with self._connect(read_only=False) as conn:
            # Nuovo schema: antepone metadata_id filter
            if self._use_meta_schema():
                meta_id = self._get_metadata_id(conn, entity_id)
                if meta_id is None:
                    return {"deleted": 0}
                conditions = ["metadata_id = ?"] + conditions
                params = [meta_id] + params

            where = " AND ".join(conditions)
            count_row = conn.execute(f"SELECT COUNT(*) AS c FROM states WHERE {where}", params).fetchone()
            total = count_row["c"] if count_row else 0
            if total == 0:
                return {"deleted": 0}

            deleted = 0
            while True:
                ids = conn.execute(
                    f"SELECT state_id FROM states WHERE {where} LIMIT ?",
                    params + [batch_size],
                ).fetchall()
                if not ids:
                    break
                id_list = [r["state_id"] for r in ids]
                ph = ",".join("?" * len(id_list))
                conn.execute(f"UPDATE states SET old_state_id = NULL WHERE old_state_id IN ({ph})", id_list)
                conn.execute(f"DELETE FROM states WHERE state_id IN ({ph})", id_list)
                conn.commit()
                deleted += len(id_list)
                if len(id_list) < batch_size:
                    break

            logger.info(f"delete_anomalies {entity_id}: {deleted}/{total} eliminati")
            return {"deleted": deleted, "total_found": total}

    def _build_anomaly_conditions(self, entity_id: str, criteria: dict):
        """Helper: costruisce WHERE conditions per anomalie."""
        anomaly_conds = []
        params_list: list = []

        remove_negative = criteria.get("remove_negative", False)
        min_value = criteria.get("min_value")
        max_value = criteria.get("max_value")
        std_mult = criteria.get("std_dev_multiplier")
        state_blacklist = criteria.get("state_blacklist", [])

        if remove_negative:
            anomaly_conds.append(
                "state NOT IN ('unknown','unavailable','') AND CAST(state AS REAL) < 0"
            )
        if min_value is not None:
            anomaly_conds.append(
                "state NOT IN ('unknown','unavailable','') AND CAST(state AS REAL) < ?"
            )
            params_list.append(float(min_value))
        if max_value is not None:
            anomaly_conds.append(
                "state NOT IN ('unknown','unavailable','') AND CAST(state AS REAL) > ?"
            )
            params_list.append(float(max_value))
        if state_blacklist:
            ph = ",".join("?" * len(state_blacklist))
            anomaly_conds.append(f"state IN ({ph})")
            params_list.extend(state_blacklist)
        if std_mult is not None:
            use_meta = self._use_meta_schema()
            with self._connect(read_only=True) as conn:
                if use_meta:
                    meta_id = self._get_metadata_id(conn, entity_id)
                    if meta_id is not None:
                        row = conn.execute(
                            "SELECT AVG(CAST(state AS REAL)) AS avg_v, "
                            "SUM((CAST(state AS REAL) - (SELECT AVG(CAST(state AS REAL)) "
                            "FROM states WHERE metadata_id = ? AND "
                            "state NOT IN ('unknown','unavailable',''))) * "
                            "(CAST(state AS REAL) - (SELECT AVG(CAST(state AS REAL)) "
                            "FROM states WHERE metadata_id = ? AND "
                            "state NOT IN ('unknown','unavailable','')))) / COUNT(*) AS variance "
                            "FROM states WHERE metadata_id = ? AND state NOT IN ('unknown','unavailable','')",
                            (meta_id, meta_id, meta_id),
                        ).fetchone()
                    else:
                        row = None
                else:
                    row = conn.execute(
                        "SELECT AVG(CAST(state AS REAL)) AS avg_v, "
                        "SUM((CAST(state AS REAL) - (SELECT AVG(CAST(state AS REAL)) "
                        "FROM states WHERE entity_id = ? AND "
                        "state NOT IN ('unknown','unavailable',''))) * "
                        "(CAST(state AS REAL) - (SELECT AVG(CAST(state AS REAL)) "
                        "FROM states WHERE entity_id = ? AND "
                        "state NOT IN ('unknown','unavailable','')))) / COUNT(*) AS variance "
                        "FROM states WHERE entity_id = ? AND state NOT IN ('unknown','unavailable','')",
                        (entity_id, entity_id, entity_id),
                    ).fetchone()
                if row and row["avg_v"] is not None and row["variance"] is not None:
                    import math
                    avg = row["avg_v"]
                    std = math.sqrt(max(0, row["variance"]))
                    lower = avg - float(std_mult) * std
                    upper = avg + float(std_mult) * std
                    anomaly_conds.append(
                        "state NOT IN ('unknown','unavailable','') AND "
                        "(CAST(state AS REAL) < ? OR CAST(state AS REAL) > ?)"
                    )
                    params_list.extend([lower, upper])

        if not anomaly_conds:
            return [], []

        # Combina con OR (record anomalo se soddisfa QUALSIASI criterio)
        combined_anomaly = "(" + " OR ".join(f"({c})" for c in anomaly_conds) + ")"
        use_meta = self._use_meta_schema()
        if use_meta:
            # La metadata_id viene iniettata a runtime da preview/delete_anomalies
            # Restituisce il filtro senza entity_id, aggiunto da chi chiama
            final_conditions = [combined_anomaly]
            final_params = params_list
        else:
            final_conditions = ["entity_id = ?"] + [combined_anomaly]
            final_params = [entity_id] + params_list

        return final_conditions, final_params
