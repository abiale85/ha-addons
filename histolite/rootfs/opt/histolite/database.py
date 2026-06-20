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
        if not read_only:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    # ------------------------------------------------------------------
    # Rilevamento schema
    # ------------------------------------------------------------------

    def get_schema_info(self) -> dict:
        """Rileva la versione dello schema del database HA."""
        if self._schema:
            return self._schema
        with self._connect(read_only=True) as conn:
            cur = conn.execute("PRAGMA table_info(states)")
            cols = {row["name"] for row in cur.fetchall()}
            uses_ts = "last_updated_ts" in cols
            has_attributes_id = "attributes_id" in cols
            self._schema = {
                "uses_ts": uses_ts,
                "has_attributes_id": has_attributes_id,
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
        """Restituisce i sensori con più stati registrati."""
        schema = self.get_schema_info()
        if schema["uses_ts"]:
            min_ts_expr = "MIN(last_updated_ts)"
            max_ts_expr = "MAX(last_updated_ts)"
        else:
            min_ts_expr = "MIN(last_updated)"
            max_ts_expr = "MAX(last_updated)"

        order_map = {
            "count": "record_count DESC",
            "entity": "entity_id ASC",
        }
        order = order_map.get(sort_by, "record_count DESC")

        query = f"""
            SELECT
                entity_id,
                COUNT(*) AS record_count,
                {min_ts_expr} AS first_seen,
                {max_ts_expr} AS last_seen
            FROM states
            WHERE entity_id LIKE ?
            GROUP BY entity_id
            ORDER BY {order}
            LIMIT ?
        """
        search_pattern = f"%{search}%"
        with self._connect(read_only=True) as conn:
            rows = conn.execute(query, (search_pattern, limit)).fetchall()
            return [dict(r) for r in rows]

    def get_sensor_stats(self, entity_id: str) -> Optional[dict]:
        """Statistiche dettagliate per un singolo sensore."""
        schema = self.get_schema_info()
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
            row = conn.execute(query, (entity_id,)).fetchone()
            if not row:
                return None
            result = dict(row)
            # Campiona ultime 10 righe per determinare se è numerico
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

    def get_sensor_daily_counts(self, entity_id: str, days: int = 90) -> list[dict]:
        """Conta i record per giorno per un sensore (per il grafico)."""
        schema = self.get_schema_info()
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
            params = (entity_id, cutoff)
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
            params = (entity_id, f"-{days}")

        with self._connect(read_only=True) as conn:
            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    def get_recent_values(self, entity_id: str, limit: int = 20) -> list[dict]:
        """Ultime N righe per un sensore."""
        schema = self.get_schema_info()
        order_col = "last_updated_ts" if schema["uses_ts"] else "last_updated"
        with self._connect(read_only=True) as conn:
            rows = conn.execute(
                f"SELECT state, {order_col} AS ts FROM states "
                f"WHERE entity_id = ? ORDER BY {order_col} DESC LIMIT ?",
                (entity_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]

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
        cond, param = self._ts_filter("s", older_than_days)
        count_query = f"SELECT COUNT(*) AS c FROM states s WHERE entity_id = ? AND {cond}"
        params_count = [entity_id]
        if param is not None:
            params_count.append(param)

        with self._connect(read_only=dry_run) as conn:
            count_row = conn.execute(count_query, params_count).fetchone()
            total_to_delete = count_row["c"] if count_row else 0

            if dry_run or total_to_delete == 0:
                return {"deleted": 0, "estimated": total_to_delete, "dry_run": dry_run}

            deleted = 0
            schema = self.get_schema_info()
            ts_col = schema["timestamp_col"]

            while True:
                if schema["uses_ts"]:
                    ids = conn.execute(
                        "SELECT state_id FROM states "
                        "WHERE entity_id = ? AND last_updated_ts < ? "
                        "LIMIT ?",
                        (entity_id, param, batch_size),
                    ).fetchall()
                else:
                    ids = conn.execute(
                        f"SELECT state_id FROM states "
                        f"WHERE entity_id = ? AND datetime(last_updated) < "
                        f"datetime('now', '-{older_than_days} days') "
                        f"LIMIT ?",
                        (entity_id, batch_size),
                    ).fetchall()

                if not ids:
                    break
                id_list = [r["state_id"] for r in ids]
                placeholders = ",".join("?" * len(id_list))
                # Annulla riferimenti old_state_id prima di eliminare
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

        # Formato bucket in SQL
        if schema["uses_ts"]:
            import time
            cutoff = time.time() - older_than_days * 86400
            if granularity == "hour":
                bucket_expr = (
                    "CAST(last_updated_ts / 3600 AS INTEGER) * 3600"
                )
            else:  # day
                bucket_expr = (
                    "CAST(last_updated_ts / 86400 AS INTEGER) * 86400"
                )
            where_clause = f"entity_id = ? AND last_updated_ts < ?"
            base_params = (entity_id, cutoff)
        else:
            if granularity == "hour":
                bucket_expr = "strftime('%Y-%m-%d %H', last_updated)"
            else:
                bucket_expr = "strftime('%Y-%m-%d', last_updated)"
            cutoff = None
            where_clause = (
                f"entity_id = ? AND datetime(last_updated) < "
                f"datetime('now', '-{older_than_days} days')"
            )
            base_params = (entity_id,)

        # Conta quante righe verranno elaborate
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

        with self._connect(read_only=dry_run) as conn:
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

                # Aggiorna il valore del record da mantenere (se numerico)
                if avg_val is not None:
                    try:
                        avg_str = f"{avg_val:.4f}".rstrip("0").rstrip(".")
                        conn.execute(
                            "UPDATE states SET state = ? WHERE state_id = ?",
                            (avg_str, keep_id),
                        )
                    except Exception as e:
                        logger.warning(f"Errore aggiornamento stato: {e}")

                # Trova i record da eliminare in questo bucket
                if schema["uses_ts"]:
                    to_delete = conn.execute(
                        f"SELECT state_id FROM states "
                        f"WHERE entity_id = ? AND last_updated_ts < ? "
                        f"AND {bucket_expr} = ? AND state_id != ?",
                        (entity_id, cutoff, bucket["bucket"], keep_id),
                    ).fetchall()
                else:
                    to_delete = conn.execute(
                        f"SELECT state_id FROM states "
                        f"WHERE entity_id = ? "
                        f"AND datetime(last_updated) < datetime('now', '-{older_than_days} days') "
                        f"AND {bucket_expr} = ? AND state_id != ?",
                        (entity_id, bucket["bucket"], keep_id),
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
