"""
HistoLite - Analisi statistica del database HA
"""

import logging
import time
from database import HaDatabase

logger = logging.getLogger(__name__)


def get_db_overview(db: HaDatabase) -> dict:
    """Panoramica completa del database."""
    try:
        db_size = db.get_db_size()
        table_counts = db.get_table_counts()
        schema = db.get_schema_info()
        
        total_states = table_counts.get("states", 0)
        logger.info(f"DB overview: total_states={total_states}, schema={schema['timestamp_col']}, schema_type={schema.get('schema_type','?')}")
        
        sensor_ranking = db.get_top_sensors(limit=500)
        top_sensors = sensor_ranking[:10]
        logger.info(f"Ranking sensori recuperato: {len(sensor_ranking)} risultati")
        if top_sensors:
            logger.debug(f"Primi sensori: {[s['entity_id'] for s in top_sensors[:3]]}")

        top_10_states = sum(s["record_count"] for s in top_sensors)
        top_10_pct = (top_10_states / total_states * 100) if total_states > 0 else 0

        schema_type = schema.get("schema_type", "unknown")
        result = {
            "db_size_bytes": db_size,
            "db_size_human": _human_size(db_size),
            "table_counts": table_counts,
            "schema": schema,
            "schema_type": schema_type,
            "total_states": total_states,
            "top_10_states": top_10_states,
            "top_10_pct": round(top_10_pct, 1),
            "top_sensors": top_sensors,
            "sensor_ranking": sensor_ranking,
        }
        if schema_type == "unknown":
            result["schema_warning"] = (
                "Schema del database non riconosciuto. "
                "Tutte le operazioni di scrittura sono bloccate. "
                f"Colonne rilevate in 'states': {sorted(schema.get('columns', []))}."
            )
        return result
    except Exception as e:
        logger.error(f"Errore analisi DB: {e}", exc_info=True)
        return {"error": str(e)}


def analyze_sensor(db: HaDatabase, entity_id: str) -> dict:
    """Analisi approfondita di un singolo sensore."""
    try:
        stats = db.get_sensor_stats(entity_id)
        if not stats:
            return {"error": f"Entità non trovata: {entity_id}"}

        daily_counts = db.get_sensor_daily_counts(entity_id, days=90)
        recent_values = db.get_recent_values(entity_id, limit=10)
        schema = db.get_schema_info()

        # Calcola la frequenza media
        if daily_counts:
            total = sum(d["count"] for d in daily_counts)
            days = len(daily_counts)
            avg_per_day = total / days if days > 0 else 0
        else:
            avg_per_day = 0

        # Stima risparmio potenziale con diverse strategie
        savings = _estimate_savings(stats, avg_per_day)

        return {
            "entity_id": entity_id,
            "stats": stats,
            "daily_counts": daily_counts,
            "recent_values": recent_values,
            "avg_per_day": round(avg_per_day, 1),
            "is_numeric": stats.get("is_numeric", False),
            "savings_estimate": savings,
        }
    except Exception as e:
        logger.error(f"Errore analisi sensore {entity_id}: {e}")
        return {"error": str(e)}


def _estimate_savings(stats: dict, avg_per_day: float) -> dict:
    """Stima il risparmio potenziale delle diverse strategie."""
    total = stats.get("record_count", 0)
    if total == 0:
        return {}

    # Purge 30 giorni: elimina tutto > 30gg
    purge_30_est = max(0, total - int(avg_per_day * 30))
    # Flatten orario 7gg: 1 record/ora vs N record/ora
    flatten_hourly_est = int(total * 0.7) if avg_per_day > 24 else int(total * 0.3)
    # Flatten giornaliero 30gg: 1 record/giorno
    flatten_daily_est = int(total * 0.9) if avg_per_day > 100 else int(total * 0.6)

    return {
        "purge_30d": {
            "estimated_deleted": purge_30_est,
            "pct": round(purge_30_est / total * 100, 1),
        },
        "flatten_hourly_7d": {
            "estimated_deleted": flatten_hourly_est,
            "pct": round(flatten_hourly_est / total * 100, 1),
        },
        "flatten_daily_30d": {
            "estimated_deleted": flatten_daily_est,
            "pct": round(flatten_daily_est / total * 100, 1),
        },
    }


def _human_size(size_bytes: int) -> str:
    """Converti bytes in formato leggibile."""
    for unit in ("B", "KB", "MB", "GB"):
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} TB"
