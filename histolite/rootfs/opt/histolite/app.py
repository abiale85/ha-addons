"""
HistoLite - Applicazione principale Flask
Add-on Home Assistant per gestione intelligente della history dei sensori
"""

import os
import gc
import logging
import time
from datetime import datetime
from flask import Flask, render_template, request, jsonify
from flask_cors import CORS

from database import HaDatabase, SchemaUnrecognizedError
from analyzer import get_db_overview, analyze_sensor
from strategies import execute_strategy, STRATEGY_LIST
from config_manager import ConfigManager
from cache_manager import CacheManager

# ---------------------------------------------------------------------------
# Configurazione
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=getattr(logging, os.environ.get("LOG_LEVEL", "info").upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("histolite")

DB_PATH = os.environ.get("DB_PATH", "/config/home-assistant_v2.db")
DATA_PATH = os.environ.get("DATA_PATH", "/data")
BACKUP_PATH = os.environ.get("BACKUP_PATH", "/backup")
BACKUP_BEFORE_PURGE = os.environ.get("BACKUP_BEFORE_PURGE", "true").lower() == "true"
MAX_ROWS_PER_BATCH = int(os.environ.get("MAX_ROWS_PER_BATCH", "5000"))
# INGRESS_PATH: HA Supervisor passa il prefisso come env var.
# Ingress fa da reverse proxy e STRIPPA il prefisso prima di inviare la
# richiesta al container → Flask riceve sempre path senza prefisso (es. GET /).
# I template usano {{ base_path }}/route per generare URL completi verso Ingress.
INGRESS_PATH = os.environ.get("INGRESS_PATH", "").rstrip("/")
PORT = int(os.environ.get("PORT", "8099"))

# ---------------------------------------------------------------------------
# App Flask
# ---------------------------------------------------------------------------

app = Flask(__name__)
CORS(app)

db = HaDatabase(DB_PATH)
config_manager = ConfigManager(DATA_PATH)
cache = CacheManager(DATA_PATH)

logger.info(f"HistoLite avviato - DB: {DB_PATH} - Port: {PORT} - Ingress: {INGRESS_PATH or '(nessuno)'}")


# ---------------------------------------------------------------------------
# Gestione errori globali
# ---------------------------------------------------------------------------

@app.errorhandler(SchemaUnrecognizedError)
def handle_schema_error(e):
    """Schema non riconosciuto: blocca qualsiasi scrittura e notifica l'UI."""
    logger.critical(f"SchemaUnrecognizedError: {e}")
    return jsonify({
        "error": str(e),
        "error_type": "schema_unrecognized",
    }), 503


def _get_ingress_path():
    """Restituisce il path Ingress reale leggendo l'header X-Ingress-Path.
    HA Ingress invia questo header con il path token-based corretto
    (es. /api/hassio_ingress/TOKEN). Fallback sull'env var INGRESS_PATH."""
    return request.headers.get('X-Ingress-Path', INGRESS_PATH).rstrip('/')


@app.context_processor
def inject_globals():
    return {
        "base_path": _get_ingress_path(),
        "db_path": DB_PATH,
        "now": datetime.now().strftime("%d/%m/%Y %H:%M"),
    }


# ---------------------------------------------------------------------------
# Pagine HTML
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    # Serve la dashboard direttamente - nessun redirect.
    # HA Ingress strippa il prefisso prima di inviare a Flask.
    # Log degli header per diagnosticare Ingress in produzione.
    logger.info(f"GET / - X-Ingress-Path={request.headers.get('X-Ingress-Path', 'N/A')} "
                f"Host={request.headers.get('Host', 'N/A')} "
                f"Referer={request.headers.get('Referer', 'N/A')}")
    return render_template("dashboard.html", active="dashboard")


@app.route("/dashboard")
def dashboard():
    return render_template("dashboard.html", active="dashboard")


@app.route("/sensors")
def sensors():
    search = request.args.get("search", "")
    sort = request.args.get("sort", "count")
    return render_template("sensors.html", active="sensors", search=search, sort=sort)


@app.route("/sensors/<path:entity_id>")
def sensor_detail(entity_id):
    return render_template("sensor_detail.html", active="sensors", entity_id=entity_id)


@app.route("/sensors/<path:entity_id>/edit")
def sensor_edit(entity_id):
    return render_template("sensor_edit.html", active="sensors", entity_id=entity_id)


@app.route("/strategies")
def strategies_page():
    saved = config_manager.list_strategies()
    return render_template(
        "strategies.html",
        active="strategies",
        saved_strategies=saved,
        strategy_types=STRATEGY_LIST,
    )


@app.route("/jobs")
def jobs_page():
    jobs = config_manager.list_jobs(limit=100)
    return render_template("jobs.html", active="jobs", jobs=jobs)


# ---------------------------------------------------------------------------
# API - Panoramica
# ---------------------------------------------------------------------------

@app.route("/api/overview")
def api_overview():
    """Ritorna panoramica DB con cache (TTL 5min) e timestamp aggiornamento."""
    try:
        # Controlla cache
        cached = cache.get_with_metadata("overview")
        if cached:
            data = cached["value"]
            data["cached"] = True
            data["updated_timestamp"] = int(cached["timestamp_updated"])
            data["age_seconds"] = int(cached["age_seconds"])
            logger.info(f"Ritornando overview da cache (eta {cached['age_seconds']:.0f}s)")
            return jsonify(data)

        # Cache scaduto o assente -> ricalcola
        logger.info("Cache overview scaduto, ricalcolando...")
        data = get_db_overview(db)
        if "error" not in data:
            # TTL 5 minuti (300 secondi)
            cache.set("overview", data, ttl_seconds=300)
            data["cached"] = False
            data["updated_timestamp"] = int(time.time())
            data["age_seconds"] = 0
        return jsonify(data)
    except Exception as e:
        logger.error(f"Errore api/overview: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/overview/refresh", methods=["POST"])
def api_overview_refresh():
    """Forza il ricalcolo dell'overview, invalidando la cache."""
    try:
        cache.invalidate("overview")
        logger.info("Cache overview invalidato, ricalcolando...")
        data = get_db_overview(db)
        if "error" not in data:
            cache.set("overview", data, ttl_seconds=300)
            data["cached"] = False
            data["updated_timestamp"] = int(time.time())
            data["age_seconds"] = 0
        return jsonify(data)
    except Exception as e:
        logger.error(f"Errore refresh overview: {e}")
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# API - Sensori
# ---------------------------------------------------------------------------

@app.route("/api/sensors")
def api_sensors():
    try:
        limit = int(request.args.get("limit", 200))
        sort = request.args.get("sort", "count")
        search = request.args.get("search", "")
        sensors_list = db.get_top_sensors(limit=limit, sort_by=sort, search=search)
        return jsonify(sensors_list)
    except Exception as e:
        logger.error(f"Errore api/sensors: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/sensors/list")
def api_sensors_list():
    """Endpoint veloce - senza COUNT/GROUP BY. Carica pagina sensori velocemente."""
    try:
        search = request.args.get("search", "")
        entities = db.get_entity_list(search=search)
        return jsonify(entities)
    except Exception as e:
        logger.error(f"Errore api/sensors/list: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/sensors/<path:entity_id>/values")
def api_sensor_values(entity_id):
    """Valori paginati con filtri per timeframe e range."""
    try:
        start_ts = request.args.get("start_ts", type=float)
        end_ts = request.args.get("end_ts", type=float)
        min_val = request.args.get("min_val", type=float)
        max_val = request.args.get("max_val", type=float)
        page = int(request.args.get("page", 1))
        per_page = min(int(request.args.get("per_page", 100)), 500)
        data = db.get_sensor_values(entity_id, start_ts, end_ts, min_val, max_val, page, per_page)
        return jsonify(data)
    except Exception as e:
        logger.error(f"Errore api/sensors/{entity_id}/values: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/sensors/<path:entity_id>/values", methods=["DELETE"])
def api_delete_sensor_values(entity_id):
    """Elimina record specifici per state_id."""
    try:
        body = request.get_json()
        state_ids = body.get("state_ids", []) if body else []
        if not state_ids:
            return jsonify({"error": "state_ids richiesto"}), 400
        if len(state_ids) > 1000:
            return jsonify({"error": "Max 1000 record per richiesta"}), 400
        deleted = db.delete_states_by_ids([int(i) for i in state_ids])
        return jsonify({"deleted": deleted})
    except Exception as e:
        logger.error(f"Errore delete values {entity_id}: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/sensors/<path:entity_id>/anomalies/preview", methods=["POST"])
def api_anomalies_preview(entity_id):
    """Anteprima anomalie senza modifiche."""
    try:
        body = request.get_json() or {}
        criteria = body.get("criteria", {})
        result = db.preview_anomalies(entity_id, criteria)
        return jsonify(result)
    except Exception as e:
        logger.error(f"Errore anomalies preview {entity_id}: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/sensors/<path:entity_id>/anomalies/execute", methods=["POST"])
def api_anomalies_execute(entity_id):
    """Elimina anomalie secondo criteri."""
    try:
        body = request.get_json() or {}
        criteria = body.get("criteria", {})
        result = db.delete_anomalies(entity_id, criteria)
        return jsonify(result)
    except Exception as e:
        logger.error(f"Errore anomalies execute {entity_id}: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/sensors/<path:entity_id>")
def api_sensor_detail(entity_id):
    try:
        data = analyze_sensor(db, entity_id)
        return jsonify(data)
    except Exception as e:
        logger.error(f"Errore api/sensors/{entity_id}: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/sensors/<path:entity_id>/chart")
def api_sensor_chart(entity_id):
    try:
        days = int(request.args.get("days", 90))
        data = db.get_sensor_daily_counts(entity_id, days=days)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# API - Strategie
# ---------------------------------------------------------------------------

@app.route("/api/strategy-types")
def api_strategy_types():
    return jsonify(STRATEGY_LIST)


@app.route("/api/strategies", methods=["GET"])
def api_list_strategies():
    return jsonify(config_manager.list_strategies())


@app.route("/api/strategies", methods=["POST"])
def api_save_strategy():
    try:
        data = request.get_json(force=True)
        if not data.get("name") or not data.get("strategy_type"):
            return jsonify({"error": "Campi 'name' e 'strategy_type' obbligatori"}), 400
        saved = config_manager.save_strategy(data)
        return jsonify(saved), 201
    except Exception as e:
        logger.error(f"Errore salvataggio strategia: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/strategies/<strategy_id>", methods=["DELETE"])
def api_delete_strategy(strategy_id):
    if config_manager.delete_strategy(strategy_id):
        return jsonify({"ok": True})
    return jsonify({"error": "Strategia non trovata"}), 404


# ---------------------------------------------------------------------------
# API - Esecuzione
# ---------------------------------------------------------------------------

@app.route("/api/preview", methods=["POST"])
def api_preview():
    """Anteprima (dry-run) di una strategia senza modificare il DB."""
    try:
        data = request.get_json(force=True)
        strategy_type = data.get("strategy_type")
        entity_ids = data.get("entity_ids", [])
        params = data.get("params", {})

        if not strategy_type or not entity_ids:
            return jsonify({"error": "Parametri mancanti: strategy_type, entity_ids"}), 400

        result = execute_strategy(
            db=db,
            strategy_name=strategy_type,
            entity_ids=entity_ids,
            params=params,
            dry_run=True,
        )
        return jsonify(result)
    except Exception as e:
        logger.error(f"Errore api/preview: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/execute", methods=["POST"])
def api_execute():
    """Esegue una strategia per le entità selezionate."""
    try:
        data = request.get_json(force=True)
        strategy_type = data.get("strategy_type")
        entity_ids = data.get("entity_ids", [])
        params = data.get("params", {})

        if not strategy_type or not entity_ids:
            return jsonify({"error": "Parametri mancanti: strategy_type, entity_ids"}), 400

        if len(entity_ids) > 50:
            return jsonify({"error": "Massimo 50 entità per operazione"}), 400

        start = time.time()
        result = execute_strategy(
            db=db,
            strategy_name=strategy_type,
            entity_ids=entity_ids,
            params=params,
            dry_run=False,
            backup_path=BACKUP_PATH,
            backup_before=BACKUP_BEFORE_PURGE,
            batch_size=MAX_ROWS_PER_BATCH,
        )
        result["duration_sec"] = round(time.time() - start, 2)

        # Salva nel log
        config_manager.save_job(result, strategy_type, entity_ids, params, dry_run=False)

        return jsonify(result)
    except Exception as e:
        logger.error(f"Errore api/execute: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/execute-saved/<strategy_id>", methods=["POST"])
def api_execute_saved(strategy_id):
    """Esegue una strategia salvata (per nome ID)."""
    saved = config_manager.get_strategy(strategy_id)
    if not saved:
        return jsonify({"error": "Strategia non trovata"}), 404
    try:
        start = time.time()
        result = execute_strategy(
            db=db,
            strategy_name=saved["strategy_type"],
            entity_ids=saved.get("entity_ids", []),
            params=saved.get("params", {}),
            dry_run=False,
            backup_path=BACKUP_PATH,
            backup_before=BACKUP_BEFORE_PURGE,
            batch_size=MAX_ROWS_PER_BATCH,
        )
        result["duration_sec"] = round(time.time() - start, 2)
        config_manager.save_job(
            result, saved["strategy_type"],
            saved.get("entity_ids", []), saved.get("params", {}), dry_run=False
        )
        return jsonify(result)
    except Exception as e:
        logger.error(f"Errore execute-saved {strategy_id}: {e}")
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# API - Manutenzione
# ---------------------------------------------------------------------------

@app.route("/api/maintenance/cleanup-attributes", methods=["POST"])
def api_cleanup_attributes():
    try:
        dry_run = request.get_json(force=True).get("dry_run", False)
        result = db.cleanup_orphaned_attributes(dry_run=dry_run)
        if not dry_run:
            config_manager.save_job(result, "cleanup_attributes", [], {}, dry_run=False)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/maintenance/cleanup-null-entities", methods=["POST"])
def api_cleanup_null_entities():
    try:
        body = request.get_json(force=True) or {}
        dry_run = body.get("dry_run", False)
        result = db.cleanup_null_entities(dry_run=dry_run)
        if not dry_run:
            config_manager.save_job(result, "cleanup_null_entities", [], {}, dry_run=False)
        return jsonify(result)
    except Exception as e:
        logger.error(f"Errore cleanup-null-entities: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/maintenance/vacuum", methods=["POST"])
def api_vacuum():
    try:
        ok = db.run_vacuum()
        result = {"ok": ok, "message": "VACUUM completato" if ok else "VACUUM fallito"}
        config_manager.save_job(result, "vacuum", [], {}, dry_run=False)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/maintenance/purge-statistics", methods=["POST"])
def api_purge_statistics():
    try:
        data = request.get_json(force=True)
        older_than_days = int(data.get("older_than_days", 90))
        dry_run = data.get("dry_run", False)
        result = db.purge_statistics_short_term(older_than_days, dry_run=dry_run)
        if not dry_run:
            config_manager.save_job(result, "purge_statistics_short_term", [], {}, dry_run=False)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/statistics-short-term")
def api_statistics_short_term():
    try:
        data = db.get_statistics_short_term_stats()
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# API - Job history
# ---------------------------------------------------------------------------

@app.route("/api/jobs")
def api_jobs():
    limit = int(request.args.get("limit", 50))
    return jsonify(config_manager.list_jobs(limit=limit))


@app.route("/api/jobs/clear", methods=["POST"])
def api_jobs_clear():
    config_manager.clear_jobs()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Avvio
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import multiprocessing
    logger.info(f"HistoLite avviato - DB: {DB_PATH} - Port: {PORT}")
    # Usa Gunicorn in produzione per gestire memoria in modo controllato.
    # 1 worker + 4 thread: bassa RAM, concorrenza sufficiente per add-on locale.
    try:
        from gunicorn.app.base import BaseApplication

        class StandaloneApp(BaseApplication):
            def __init__(self, application, options=None):
                self.options = options or {}
                self.application = application
                super().__init__()

            def load_config(self):
                for key, value in self.options.items():
                    if key in self.cfg.settings and value is not None:
                        self.cfg.set(key.lower(), value)

            def load(self):
                return self.application

        options = {
            "bind": f"0.0.0.0:{PORT}",
            "workers": 1,
            "threads": 4,
            "worker_class": "gthread",
            # Riavvia worker dopo N richieste per liberare memoria (Python non restituisce RAM all'OS)
            "max_requests": 200,
            "max_requests_jitter": 30,
            "timeout": 120,
            "keepalive": 2,
            "accesslog": "-",
            "errorlog": "-",
            "loglevel": os.environ.get("LOG_LEVEL", "info").lower(),
        }
        logger.info("Avvio con Gunicorn (1 worker, 4 thread)")
        StandaloneApp(app, options).run()
    except ImportError:
        logger.warning("Gunicorn non disponibile, fallback su Flask dev server")
        app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
