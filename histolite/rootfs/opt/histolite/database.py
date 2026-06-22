"""
HistoLite - Gestione database Home Assistant
Connessione, analisi e operazioni su home-assistant_v2.db (SQLite)
"""

import sqlite3
import os
import logging
import json
import time
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


class SchemaUnrecognizedError(RuntimeError):
    """Sollevata quando lo schema del DB non è riconoscibile.
    Blocca qualsiasi operazione di lettura avanzata o scrittura."""
    pass


class HaDatabase:
    """Gestisce la connessione e le operazioni sul database HA."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._schema = None
        # helper for query instrumentation
        self._query_counter = 0

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
        return conn

    def get_schema_info(self) -> dict:
        """Rileva la versione dello schema del database HA.

        schema_type:
          'legacy'       - entity_id direttamente in states (HA < recorder 23)
          'modern'       - entity_id in states_meta via metadata_id (HA >= recorder 23)
          'transitional' - entrambe le colonne presenti (migrazione in corso), trattato come legacy
          'unknown'      - schema non riconosciuto: nessuna operazione di scrittura consentita
        """
        if self._schema:
            return self._schema
        with self._connect(read_only=True) as conn:
            cur = conn.execute("PRAGMA table_info(states)")
            cols = {row["name"] for row in cur.fetchall()}
            uses_ts = "last_updated_ts" in cols
            has_attributes_id = "attributes_id" in cols
            has_metadata = "metadata_id" in cols
            has_entity_id_in_states = "entity_id" in cols

            # Determina il tipo di schema
            if has_entity_id_in_states and not has_metadata:
                schema_type = "legacy"
            elif has_metadata and not has_entity_id_in_states:
                # Verifica che states_meta esista davvero
                try:
                    sm = conn.execute(
                        "SELECT name FROM sqlite_master "
                        "WHERE type='table' AND name='states_meta'"
                    ).fetchone()
                    schema_type = "modern" if sm else "unknown"
                except Exception:
                    schema_type = "unknown"
            elif has_entity_id_in_states and has_metadata:
                # Migrazione in corso: entrambe le colonne presenti
                schema_type = "transitional"   # trattato come legacy in _use_meta_schema
            else:
                # Nessuna delle due → schema non riconosciuto
                schema_type = "unknown"

            if schema_type == "unknown":
                logger.error(
                    f"Schema del database non riconosciuto! "
                    f"Colonne states: {sorted(cols)}. "
                    f"Operazioni di scrittura bloccate."
                )

            self._schema = {
                "uses_ts": uses_ts,
                "has_attributes_id": has_attributes_id,
                "has_metadata": has_metadata,
                "has_entity_id_in_states": has_entity_id_in_states,
                "schema_type": schema_type,
                "timestamp_col": "last_updated_ts" if uses_ts else "last_updated",
                "columns": list(cols),
            }
        return self._schema

    def _ts_filter(self, alias: str, older_than_days: int) -> tuple[str, float | str | None]:
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

    def _log_query_start(self, sql: str, params) -> int:
        """Logga l'inizio di una query in esecuzione quando il livello è DEBUG e restituisce il numero di query."""
        if not logger.isEnabledFor(logging.DEBUG):
            return 0
        short_sql = " ".join(sql.strip().split())
        if len(short_sql) > 300:
            short_sql = short_sql[:300] + "..."
        self._query_counter += 1
        counter = self._query_counter
        logger.debug(f"[DBQuerySTART#{counter}] in esecuzione: {short_sql} params={params!r}")
        return counter

    def _log_query(self, sql: str, duration: float, counter: int = 0) -> None:
        """Logga la fine e durata di una query."""
        short_sql = " ".join(sql.strip().split())
        if len(short_sql) > 300:
            short_sql = short_sql[:300] + "..."
        ms = int(duration * 1000)
        c_str = str(counter) if counter > 0 else "?"
        logger.info(f"[DBQueryEND#{c_str}] duration_ms={ms} sql={short_sql}")

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
        """True se il DB usa il nuovo schema con states_meta (entity_id separato).
        
        Solo per 'modern': in HA recorder v23+ con migrazione completata.
        'transitional' viene trattato come legacy SE ha dati reali in entity_id;
        altrimenti (migrazione completata, entity_id tutto NULL) → usa meta schema.
        """
        schema = self.get_schema_info()
        st = schema.get("schema_type")
        if st == "modern":
            return True
        if st == "transitional":
            # Controlla se esistono record con entity_id valido (rilevazione una-tantum, cachata)
            if "_has_legacy_data" not in schema:
                try:
                    with self._connect(read_only=True) as conn:
                        conn.execute("PRAGMA busy_timeout=2000")
                        row = conn.execute(
                            "SELECT 1 FROM states WHERE entity_id IS NOT NULL AND entity_id != '' LIMIT 1"
                        ).fetchone()
                        schema["_has_legacy_data"] = row is not None
                        logger.info(f"Schema transitional: has_legacy_data={schema['_has_legacy_data']}")
                except Exception as e:
                    logger.warning(f"_use_meta_schema transitional check: {e}")
                    schema["_has_legacy_data"] = True  # fallback conservativo
            return not schema["_has_legacy_data"]
        return False

    def _validate_schema_for_write(self) -> None:
        """Verifica che lo schema sia riconosciuto prima di qualsiasi scrittura.

        Solleva SchemaUnrecognizedError se lo schema è 'unknown'.
        Questa eccezione viene propagata fino agli endpoint Flask che la
        restituiscono come errore JSON visibile nelle notifiche dell'UI.
        Non viene mai silenziata internamente.
        """
        schema = self.get_schema_info()
        if schema.get("schema_type") == "unknown":
            cols = schema.get("columns", [])
            raise SchemaUnrecognizedError(
                f"Schema del database non riconosciuto "
                f"(colonne rilevate in 'states': {sorted(cols)}). "
                f"Nessuna operazione di scrittura o modifica viene eseguita. "
                f"Aggiorna HistoLite o apri una issue allegando questo messaggio."
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
            # Schema legacy/transitional: entity_id direttamente in states
            # Filtra entity_id IS NOT NULL (record transitional con solo metadata_id)
            if search:
                where_clause = "WHERE entity_id IS NOT NULL AND entity_id != '' AND entity_id LIKE ?"
            else:
                where_clause = "WHERE entity_id IS NOT NULL AND entity_id != ''"
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
            conn.execute("PRAGMA busy_timeout=30000")  # attendi fino a 30s per lock DB
            try:
                if search:
                    rows = conn.execute(query, (search_pattern, limit)).fetchall()
                else:
                    rows = conn.execute(query, (limit,)).fetchall()
                results = [dict(r) for r in rows]
                logger.info(f"get_top_sensors: {len(results)} sensori trovati")
                return results
            except sqlite3.OperationalError as e:
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
            # Nuovo schema HA: entity_id in states_meta — query diretta, senza JOIN
            if search:
                query = "SELECT entity_id FROM states_meta WHERE entity_id LIKE ? ORDER BY entity_id LIMIT 1000"
            else:
                query = "SELECT entity_id FROM states_meta ORDER BY entity_id LIMIT 1000"
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

    def get_sensor_value_range(self, entity_id: str) -> dict:
        """
        Estrae il range di valori accettabili per un sensore:
        1. min_value/max_value configurato in HA (da state_attributes)
        2. min/max osservato nei dati storici
        3. media e stddev dei valori numerici recenti
        """
        result = {
            "entity_id": entity_id,
            "configured_min": None,
            "configured_max": None,
            "observed_min": None,
            "observed_max": None,
            "recent_avg": None,
            "recent_stddev": None,
            "unit": None,
            "device_class": None,
        }
        
        use_meta = self._use_meta_schema()
        schema = self.get_schema_info()
        order_col = "last_updated_ts" if schema["uses_ts"] else "last_updated"
        
        with self._connect(read_only=True) as conn:
            conn.execute("PRAGMA busy_timeout=5000")
            try:
                # Estrai metadata_id
                if use_meta:
                    meta_id = self._get_metadata_id(conn, entity_id)
                    if meta_id is None:
                        return result
                    id_filter = "s.metadata_id = ?"
                    id_param = meta_id
                else:
                    id_filter = "s.entity_id = ?"
                    id_param = entity_id
                
                # Estrai attributi dal primo record (contengono le configurazioni)
                if use_meta:
                    attr_query = f"""
                        SELECT sa.attributes FROM state_attributes sa
                        WHERE sa.attributes_id IN (
                            SELECT DISTINCT s.attributes_id FROM states s
                            WHERE {id_filter} LIMIT 1
                        )
                    """
                else:
                    attr_query = f"""
                        SELECT attributes FROM states
                        WHERE {id_filter} LIMIT 1
                    """
                
                attr_row = conn.execute(attr_query, (id_param,)).fetchone()
                
                if attr_row and attr_row[0]:
                    try:
                        attrs = json.loads(attr_row[0])
                        result["configured_min"] = attrs.get("min_value")
                        result["configured_max"] = attrs.get("max_value")
                        result["unit"] = attrs.get("unit_of_measurement")
                        result["device_class"] = attrs.get("device_class")
                    except (json.JSONDecodeError, TypeError):
                        pass
                
                # Calcola min/max osservati sui valori numerici (CAST fa filtro automatico)
                # Usa TRY() per gestire i valori non-numerici
                stats_query = f"""
                    SELECT
                        MIN(CAST(s.state AS REAL)) AS obs_min,
                        MAX(CAST(s.state AS REAL)) AS obs_max,
                        AVG(CAST(s.state AS REAL)) AS avg_val
                    FROM states s
                    WHERE {id_filter}
                    AND s.state NOT IN ('unknown','unavailable','')
                    AND typeof(CAST(s.state AS REAL)) = 'real'
                """
                
                stats_row = conn.execute(stats_query, (id_param,)).fetchone()
                if stats_row:
                    result["observed_min"] = stats_row["obs_min"]
                    result["observed_max"] = stats_row["obs_max"]
                    result["recent_avg"] = stats_row["avg_val"]
                    
                    # Calcola stddev separatamente per evitare errori
                    if stats_row["avg_val"] is not None:
                        stddev_query = f"""
                            SELECT SQRT(
                                SUM((CAST(s.state AS REAL) - ?) * (CAST(s.state AS REAL) - ?))
                                / COUNT(*)
                            ) AS stddev_val
                            FROM states s
                            WHERE {id_filter}
                            AND s.state NOT IN ('unknown','unavailable','')
                            AND typeof(CAST(s.state AS REAL)) = 'real'
                        """
                        stddev_row = conn.execute(stddev_query, 
                            (id_param, stats_row["avg_val"], stats_row["avg_val"])).fetchone()
                        if stddev_row:
                            result["recent_stddev"] = stddev_row["stddev_val"]
                
            except sqlite3.OperationalError as e:
                logger.warning(f"get_sensor_value_range {entity_id}: {e}")
        
        return result


    # ------------------------------------------------------------------
    # Operazioni di purge
    # ------------------------------------------------------------------

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
        self._validate_schema_for_write()
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
            params_count: list[object] = [id_param]
            if param is not None:
                params_count.append(param)
            start_t = time.time()
            c_qid = self._log_query_start(count_query, params_count)
            count_row = conn.execute(count_query, params_count).fetchone()
            self._log_query(count_query, time.time() - start_t, c_qid)
            total_to_delete = count_row["c"] if count_row else 0

            if dry_run or total_to_delete == 0:
                return {"deleted": 0, "estimated": total_to_delete, "dry_run": dry_run}

            deleted = 0
            start_op = time.time()
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
                # time the cleanup/update and delete statements
                t0 = time.time()
                bqid = self._log_query_start(f"UPDATE/DELETE batch ({len(id_list)})", id_list)
                conn.execute(
                    f"UPDATE states SET old_state_id = NULL "
                    f"WHERE old_state_id IN ({placeholders})",
                    id_list,
                )
                conn.execute(
                    f"DELETE FROM states WHERE state_id IN ({placeholders})", id_list
                )
                conn.commit()
                self._log_query(f"UPDATE/DELETE batch ({len(id_list)})", time.time() - t0, bqid)
                deleted += len(id_list)
                elapsed = time.time() - start_op
                logger.info(f"[PurgeProgress] entity={entity_id} deleted={deleted}/{total_to_delete} elapsed_s={elapsed:.2f}")
                # Riposo per non bloccare il database principale di HA
                import time as _sys_time
                _sys_time.sleep(0.5)

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
        self._validate_schema_for_write()
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
            
            # Media pesata nel tempo mantenendo i 2 punti agli estremi (primo e ultimo)
            # - Il PRIMO record (più vecchio) del bucket viene aggiornato con la media pesata
            # - L'ULTIMO record (più nuovo) rimane invariato e serve da "ponte" per il bucket successivo
            # - Tutti gli altri record nel mezzo vengono cancellati
            # Questo garantisce continuità temporale tra bucket e ponderazione corretta della durata
            bucket_query = f"""
                WITH windowed AS (
                    SELECT
                        {bucket_expr} AS bucket,
                        state_id,
                        last_updated_ts,
                        state,
                        CAST(state AS REAL) AS numeric_value,
                        LEAD(last_updated_ts) OVER (
                            PARTITION BY {bucket_expr}
                            ORDER BY last_updated_ts ASC, state_id ASC
                        ) AS next_ts,
                        ROW_NUMBER() OVER (
                            PARTITION BY {bucket_expr}
                            ORDER BY last_updated_ts ASC, state_id ASC
                        ) AS rn,
                        COUNT(*) OVER (PARTITION BY {bucket_expr}) AS cnt
                    FROM states
                    WHERE {where_clause}
                ),
                durations AS (
                    SELECT
                        bucket,
                        state_id,
                        state,
                        numeric_value,
                        rn,
                        cnt,
                        COALESCE(next_ts - last_updated_ts, 1) AS duration_sec,
                        CASE
                            WHEN state NOT IN ('unknown','unavailable','')
                                 AND numeric_value IS NOT NULL
                            THEN numeric_value * COALESCE(next_ts - last_updated_ts, 1)
                            ELSE 0
                        END AS value_weighted
                    FROM windowed
                )
                SELECT
                    bucket,
                    MIN(CASE WHEN rn = 1 THEN state_id ELSE NULL END) AS keep_id_first,
                    MAX(CASE WHEN rn = cnt THEN state_id ELSE NULL END) AS keep_id_last,
                    COUNT(*) AS bucket_count,
                    CASE 
                        WHEN SUM(CASE 
                                    WHEN state NOT IN ('unknown','unavailable','')
                                         AND numeric_value IS NOT NULL
                                    THEN duration_sec
                                    ELSE 0
                                END) > 0
                        THEN ROUND(SUM(value_weighted) / CAST(SUM(CASE 
                                                                    WHEN state NOT IN ('unknown','unavailable','')
                                                                         AND numeric_value IS NOT NULL
                                                                    THEN duration_sec
                                                                    ELSE 0
                                                                END) AS REAL), 4)
                        ELSE NULL 
                    END AS avg_value
                FROM durations
                GROUP BY bucket
            """

            t0 = time.time()
            c_qid = self._log_query_start(count_query, base_params)
            count_row = conn.execute(count_query, base_params).fetchone()
            self._log_query(count_query, time.time() - t0, c_qid)
            total_records = count_row["c"] if count_row else 0
            t1 = time.time()
            b_qid = self._log_query_start(bucket_query, base_params)
            buckets = conn.execute(bucket_query, base_params).fetchall()
            self._log_query(bucket_query, time.time() - t1, b_qid)
            # Manteniamo 2 record per bucket (primo e ultimo), quindi eliminiamo count-2
            estimated_deleted = sum(max(0, b["bucket_count"] - 2) for b in buckets)

            if dry_run:
                return {
                    "total_records": total_records,
                    "buckets": len(buckets),
                    "estimated_deleted": estimated_deleted,
                    "dry_run": True,
                }

            t_ids = time.time()
            id_qid = self._log_query_start("SELECT all state_ids", base_params)
            all_rows = conn.execute(f"SELECT state_id FROM states WHERE {where_clause}", base_params).fetchall()
            self._log_query("SELECT all state_ids", time.time() - t_ids, id_qid)

            # Mantieni il primo e l'ultimo record di ogni bucket (per continuità temporale)
            # Aggiorna il primo record con la media pesata
            all_keep_ids = set()
            updates = []
            for b in buckets:
                if b["keep_id_first"]:
                    all_keep_ids.add(b["keep_id_first"])
                if b["keep_id_last"]:
                    all_keep_ids.add(b["keep_id_last"])
                
                # Aggiorna SOLO il primo record del bucket con la media pesata
                if b["bucket_count"] > 1 and b["avg_value"] is not None and b["keep_id_first"]:
                    avg_str = f"{b['avg_value']:.4f}".rstrip("0").rstrip(".")
                    updates.append((avg_str, b["keep_id_first"]))

            if updates:
                try:
                    conn.executemany("UPDATE states SET state = ? WHERE state_id = ?", updates)
                    conn.commit()
                    logger.info(f"[FlattenProgress] entity={entity_id} updated={len(updates)} first-record values con media")
                except Exception as e:
                    logger.warning(f"Errore executemany aggiornamento stato: {e}")

            to_delete_ids = [r["state_id"] for r in all_rows if r["state_id"] not in all_keep_ids]

            deleted = 0
            start_op = time.time()
            for i in range(0, len(to_delete_ids), batch_size):
                chunk = to_delete_ids[i:i+batch_size]
                ph = ",".join("?" * len(chunk))
                
                t_batch = time.time()
                fb_qid = self._log_query_start(f"flatten batch ({len(chunk)})", chunk)
                conn.execute(
                    f"UPDATE states SET old_state_id = NULL WHERE old_state_id IN ({ph})",
                    chunk,
                )
                conn.execute(
                    f"DELETE FROM states WHERE state_id IN ({ph})", chunk
                )
                conn.commit()
                self._log_query(f"flatten batch ({len(chunk)})", time.time() - t_batch, fb_qid)
                
                deleted += len(chunk)
                elapsed = time.time() - start_op
                logger.info(f"[FlattenProgress] entity={entity_id} deleted_total={deleted}/{len(to_delete_ids)} elapsed_s={elapsed:.2f}")
                
                import time as _sys_time
                _sys_time.sleep(0.5)

            conn.commit()
            return {
                "total_records": total_records,
                "buckets": len(buckets),
                "estimated_deleted": estimated_deleted,
                "deleted": deleted,
                "dry_run": False,
            }

    def peak_decimate_entity(
        self,
        entity_id: str,
        older_than_days: int,
        granularity: str = "hour",
        keep_resets: bool = True,
        reset_threshold_pct: float = 50.0,
        dry_run: bool = False,
        batch_size: int = 5000,
    ) -> dict:
        """
        Decima la storia di un sensore a crescita continua (contatori energia, acqua…)
        mantenendo il VALORE MASSIMO per ogni bucket temporale invece della media.

        - Preserva il picco di ogni periodo → non distorce le letture cumulative
        - Rileva i reset automaticamente (calo > reset_threshold_pct%) e conserva
          il punto immediatamente PRIMA e DOPO il reset
        - I bucket con un solo record vengono lasciati invariati
        """
        self._validate_schema_for_write()
        schema = self.get_schema_info()
        use_meta = self._use_meta_schema()

        import time as _time

        with self._connect(read_only=dry_run) as conn:
            if use_meta:
                meta_id = self._get_metadata_id(conn, entity_id)
                if meta_id is None:
                    return {"total_records": 0, "buckets": 0,
                            "estimated_deleted": 0, "deleted": 0, "dry_run": dry_run}
                id_filter = "metadata_id = ?"
                id_param = meta_id
            else:
                id_filter = "entity_id = ?"
                id_param = entity_id

            if schema["uses_ts"]:
                cutoff = _time.time() - older_than_days * 86400
                ts_col = "last_updated_ts"
                bucket_expr = (
                    "CAST(last_updated_ts / 3600 AS INTEGER) * 3600"
                    if granularity == "hour"
                    else "CAST(last_updated_ts / 86400 AS INTEGER) * 86400"
                )
                where_clause = f"{id_filter} AND last_updated_ts < ?"
                base_params: tuple = (id_param, cutoff)
            else:
                ts_col = "last_updated"
                cutoff = None
                bucket_expr = (
                    "strftime('%Y-%m-%d %H', last_updated)"
                    if granularity == "hour"
                    else "strftime('%Y-%m-%d', last_updated)"
                )
                where_clause = (
                    f"{id_filter} AND datetime(last_updated) < "
                    f"datetime('now', '-{older_than_days} days')"
                )
                base_params = (id_param,)

            # Conteggio totale
            t0 = time.time()
            c_qid = self._log_query_start("COUNT total_records", base_params)
            total_records = conn.execute(
                f"SELECT COUNT(*) AS c FROM states WHERE {where_clause}", base_params
            ).fetchone()["c"]
            self._log_query(f"COUNT total_records", time.time() - t0, c_qid)

            if total_records == 0:
                return {"total_records": 0, "buckets": 0,
                        "estimated_deleted": 0, "deleted": 0, "dry_run": dry_run}

            # ── Fase 1: trova MAX valore per bucket ──────────────────────
            # Usiamo window function ROW_NUMBER per selezionare il record col valore più alto
            # Fallback: se il sensore non è numerico, teniamo MIN(state_id) per bucket
            bucket_query = f"""
                SELECT state_id, bucket, bucket_count FROM (
                    SELECT
                        state_id,
                        {bucket_expr} AS bucket,
                        COUNT(*) OVER (PARTITION BY {bucket_expr}) AS bucket_count,
                        ROW_NUMBER() OVER (
                            PARTITION BY {bucket_expr}
                            ORDER BY
                                CASE WHEN state NOT IN ('unknown','unavailable','')
                                     THEN CAST(state AS REAL) ELSE -1e18 END DESC,
                                state_id DESC
                        ) AS rn
                    FROM states
                    WHERE {where_clause}
                ) t WHERE rn = 1
            """
            t1 = time.time()
            bq_id = self._log_query_start(bucket_query, base_params)
            keep_rows = conn.execute(bucket_query, base_params).fetchall()
            self._log_query(bucket_query, time.time() - t1, bq_id)
            keep_ids: set = {r["state_id"] for r in keep_rows}
            num_buckets = len(keep_rows)

            # ── Fase 2: reset detection ──────────────────────────────────
            reset_keep_ids: set = set()
            if keep_resets and reset_threshold_pct > 0:
                threshold_ratio = reset_threshold_pct / 100.0
                order_col = ts_col
                # Carica tutti i record numerici (solo state_id + valore) — solo il range
                vals_query = f"""
                    SELECT state_id, CAST(state AS REAL) AS val
                    FROM states
                    WHERE {where_clause}
                    AND state NOT IN ('unknown','unavailable','')
                    ORDER BY {order_col} ASC
                """
                t2 = time.time()
                vq_id = self._log_query_start(vals_query, base_params)
                vals = conn.execute(vals_query, base_params).fetchall()
                self._log_query(vals_query, time.time() - t2, vq_id)
                prev_id, prev_val = None, None
                for row in vals:
                    curr_id = row["state_id"]
                    curr_val = row["val"]
                    if prev_val is not None and prev_val > 0 and curr_val is not None:
                        drop_ratio = (prev_val - curr_val) / prev_val
                        if drop_ratio >= threshold_ratio:
                            # Reset rilevato: mantieni il punto appena prima e appena dopo
                            reset_keep_ids.add(prev_id)
                            reset_keep_ids.add(curr_id)
                    prev_id, prev_val = curr_id, curr_val

            all_keep_ids = keep_ids | reset_keep_ids
            estimated_deleted = total_records - len(all_keep_ids)

            if dry_run:
                return {
                    "total_records": total_records,
                    "buckets": num_buckets,
                    "estimated_deleted": max(0, estimated_deleted),
                    "reset_points": len(reset_keep_ids),
                    "dry_run": True,
                }

            # ── Fase 3: elimina in batch tutto tranne all_keep_ids ───────
            # Query all state_ids to find which ones to delete
            t_ids = time.time()
            id_qid = self._log_query_start("SELECT all state_ids", base_params)
            all_ids_rows = conn.execute(f"SELECT state_id FROM states WHERE {where_clause}", base_params).fetchall()
            self._log_query("SELECT all state_ids", time.time() - t_ids, id_qid)
            
            to_delete_ids = [r["state_id"] for r in all_ids_rows if r["state_id"] not in all_keep_ids]

            deleted = 0
            start_op = time.time()
            for i in range(0, len(to_delete_ids), batch_size):
                chunk = to_delete_ids[i:i+batch_size]
                ph = ",".join("?" * len(chunk))
                
                t_b = time.time()
                pb_qid = self._log_query_start(f"peak_decimate batch ({len(chunk)})", chunk)
                conn.execute(
                    f"UPDATE states SET old_state_id = NULL WHERE old_state_id IN ({ph})",
                    chunk,
                )
                conn.execute(
                    f"DELETE FROM states WHERE state_id IN ({ph})", chunk
                )
                conn.commit()
                self._log_query(f"peak_decimate batch ({len(chunk)})", time.time() - t_b, pb_qid)
                
                deleted += len(chunk)
                elapsed = time.time() - start_op
                logger.info(f"[PeakDecimateProgress] entity={entity_id} deleted_total={deleted}/{len(to_delete_ids)} elapsed_s={elapsed:.2f}")
                
                import time as _sys_time
                _sys_time.sleep(0.5)

            return {
                "total_records": total_records,
                "buckets": num_buckets,
                "estimated_deleted": estimated_deleted,
                "deleted": deleted,
                "reset_points": len(reset_keep_ids),
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
        self._validate_schema_for_write()
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
        self._validate_schema_for_write()
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
        - Schema modern/transitional: metadata_id IS NULL o orfano (non in states_meta)
        
        NOTA: schema 'transitional' (HA recorder v23, entrambe le colonne presenti)
        viene trattato come modern perché entity_id è NULL per i record nuovi.
        """
        self._validate_schema_for_write()
        use_meta = self._use_meta_schema()
        schema_type = self.get_schema_info().get("schema_type", "legacy")
        
        with self._connect(read_only=dry_run) as conn:
            try:
                if use_meta:
                    # Schema modern: record senza metadata_id valido
                    count_row = conn.execute(
                        "SELECT COUNT(*) AS c FROM states "
                        "WHERE metadata_id IS NULL "
                        "OR metadata_id NOT IN (SELECT metadata_id FROM states_meta)"
                    ).fetchone()
                elif schema_type == "transitional":
                    # Schema transitional: entrambe le colonne presenti.
                    # Solo record senza NESSUN identificatore valido sono corrotti.
                    # NON eliminare record con entity_id=NULL se hanno metadata_id valido.
                    count_row = conn.execute(
                        "SELECT COUNT(*) AS c FROM states "
                        "WHERE (entity_id IS NULL OR entity_id = '') "
                        "AND (metadata_id IS NULL)"
                    ).fetchone()
                else:
                    # Schema legacy puro: record senza entity_id
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
                elif schema_type == "transitional":
                    condition = "(entity_id IS NULL OR entity_id = '') AND metadata_id IS NULL"
                    conn.execute(
                        f"UPDATE states SET old_state_id = NULL "
                        f"WHERE old_state_id IN (SELECT state_id FROM states WHERE {condition})"
                    )
                    conn.execute(f"DELETE FROM states WHERE {condition}")
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
                logger.info(f"cleanup_null_entities: {count} record eliminati (schema: {schema_type})")
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
        self._validate_schema_for_write()
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
        self._validate_schema_for_write()
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
        self._validate_schema_for_write()
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
            t0 = time.time()
            c_qid = self._log_query_start("COUNT anomalies", params)
            # already counted above; log the count query timing
            self._log_query(f"COUNT anomalies", time.time() - t0, c_qid)

            deleted = 0
            while True:
                tq = time.time()
                sq_id = self._log_query_start(f"select anomaly ids limit {batch_size}", params + [batch_size])
                ids = conn.execute(
                    f"SELECT state_id FROM states WHERE {where} LIMIT ?",
                    params + [batch_size],
                ).fetchall()
                self._log_query(f"select anomaly ids limit {batch_size}", time.time() - tq, sq_id)
                if not ids:
                    break
                id_list = [r["state_id"] for r in ids]
                ph = ",".join("?" * len(id_list))
                t_b = time.time()
                db_qid = self._log_query_start(f"delete anomalies batch ({len(id_list)})", id_list)
                conn.execute(f"UPDATE states SET old_state_id = NULL WHERE old_state_id IN ({ph})", id_list)
                conn.execute(f"DELETE FROM states WHERE state_id IN ({ph})", id_list)
                conn.commit()
                self._log_query(f"delete anomalies batch ({len(id_list)})", time.time() - t_b, db_qid)
                deleted += len(id_list)
                logger.info(f"[AnomalyDeleteProgress] entity={entity_id} deleted_total={deleted} of={total}")
                # Riposo per non bloccare il database principale di HA
                import time as _sys_time
                _sys_time.sleep(0.5)

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
