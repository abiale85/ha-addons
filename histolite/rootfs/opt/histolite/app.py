"""
HistoLite - Applicazione principale Flask
Add-on Home Assistant per gestione intelligente della history dei sensori
"""

import os
import logging
import time
from datetime import datetime
from flask import Flask, render_template, request, jsonify, redirect, url_for
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix

from database import HaDatabase
from analyzer import get_db_overview, analyze_sensor
from strategies import execute_strategy, STRATEGY_LIST
from config_manager import ConfigManager


# ---------------------------------------------------------------------------
# Middleware per supportare Ingress path prefix
# ---------------------------------------------------------------------------

class IngressPathMiddleware:
    """Middleware che configura SCRIPT_NAME per Ingress reverse proxy."""
    def __init__(self, app, ingress_path):
        self.app = app
        self.ingress_path = ingress_path
    
    def __call__(self, environ, start_response):
        # Ingress è un reverse proxy che non include il prefisso in PATH_INFO
        # Impostiamo SCRIPT_NAME per url_for()
        if self.ingress_path:
            environ['SCRIPT_NAME'] = self.ingress_path
            logger.debug(f"Ingress middleware: SCRIPT_NAME={self.ingress_path}, PATH_INFO={environ['PATH_INFO']}")
        return self.app(environ, start_response)

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
INGRESS_PATH = os.environ.get("INGRESS_PATH", "")
PORT = int(os.environ.get("PORT", "8099"))

# ---------------------------------------------------------------------------
# App Flask
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
if INGRESS_PATH:
    app.wsgi_app = IngressPathMiddleware(app.wsgi_app, INGRESS_PATH)
CORS(app)

db = HaDatabase(DB_PATH)
config_manager = ConfigManager(DATA_PATH)


@app.context_processor
def inject_globals():
    return {
        "base_path": INGRESS_PATH,
        "db_path": DB_PATH,
        "now": datetime.now().strftime("%d/%m/%Y %H:%M"),
    }


# Logging all'avvio
logger.info(f"HistoLite avviato - DB: {DB_PATH} - Port: {PORT}")
logger.info(f"INGRESS_PATH={INGRESS_PATH}")


# ---------------------------------------------------------------------------
# Endpoint di debug (solo con log level debug)
# ---------------------------------------------------------------------------

@app.route("/_debug")
def debug_info():
    """Endpoint per debuggare la configurazione Ingress."""
    return jsonify({
        "INGRESS_PATH": INGRESS_PATH,
        "SCRIPT_NAME": request.environ.get("SCRIPT_NAME", ""),
        "PATH_INFO": request.environ.get("PATH_INFO", ""),
        "REQUEST_URL": request.url,
        "REQUEST_BASE_URL": request.base_url,
        "REQUEST_HOST": request.host,
        "url_for_dashboard": url_for("dashboard"),
    })


# ---------------------------------------------------------------------------
# Pagine HTML
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    # url_for() con SCRIPT_NAME impostato genera automaticamente l'URL corretto
    return redirect(url_for("dashboard"))


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
    try:
        data = get_db_overview(db)
        return jsonify(data)
    except Exception as e:
        logger.error(f"Errore api/overview: {e}")
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
    logger.info(f"HistoLite avviato - DB: {DB_PATH} - Port: {PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
