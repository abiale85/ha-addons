"""
HistoLite - Strategie di ottimizzazione
Definizione ed esecuzione delle 4 strategie disponibili.
"""

import logging
import time
from abc import ABC, abstractmethod
from database import HaDatabase

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------

class Strategy(ABC):
    name: str = ""
    description: str = ""

    @abstractmethod
    def execute(
        self,
        db: HaDatabase,
        entity_ids: list[str],
        params: dict,
        dry_run: bool = False,
        backup_path: str = None,
        backup_before: bool = False,
        batch_size: int = 5000,
    ) -> dict:
        ...

    def _maybe_backup(self, db, backup_path, backup_before):
        if backup_before and backup_path:
            try:
                dest = db.backup_db(backup_path)
                return dest
            except Exception as e:
                logger.warning(f"Backup fallito: {e}")
        return None


# ---------------------------------------------------------------------------
# Strategia 1 - Purge Semplice
# ---------------------------------------------------------------------------

class SimplePurge(Strategy):
    """
    Elimina TUTTI i record più vecchi di N giorni per le entità selezionate.
    Veloce e aggressivo. Consigliato per sensori ad alta frequenza.
    """
    name = "simple_purge"
    label = "Purge Semplice"
    description = "Elimina tutti i record più vecchi di N giorni."

    def execute(self, db, entity_ids, params, dry_run=False,
                backup_path=None, backup_before=False, batch_size=5000):
        older_than_days = int(params.get("older_than_days", 30))
        backup_dest = None
        if not dry_run:
            backup_dest = self._maybe_backup(db, backup_path, backup_before)

        results = []
        for eid in entity_ids:
            try:
                r = db.purge_entity(eid, older_than_days, dry_run=dry_run, batch_size=batch_size)
                r["entity_id"] = eid
                results.append(r)
                logger.info(f"[SimplePurge] {eid}: {'(DRY) ' if dry_run else ''}"
                            f"~{r.get('estimated', r.get('deleted', 0))} record")
            except Exception as e:
                logger.error(f"[SimplePurge] Errore su {eid}: {e}")
                results.append({"entity_id": eid, "error": str(e)})

        total_deleted = sum(r.get("deleted", r.get("estimated", 0)) for r in results)
        return {
            "strategy": self.name,
            "dry_run": dry_run,
            "params": params,
            "entity_count": len(entity_ids),
            "total_deleted": total_deleted,
            "backup": backup_dest,
            "details": results,
        }


# ---------------------------------------------------------------------------
# Strategia 2 - Decimazione Temporale
# ---------------------------------------------------------------------------

class TemporalDecimation(Strategy):
    """
    Mantiene 1 record per ora per dati oltre N giorni,
    e 1 record per giorno per dati oltre 2N giorni.
    Bilanciamento tra riduzione e conservazione della storicità.
    """
    name = "temporal_decimation"
    label = "Decimazione Temporale"
    description = (
        "Mantiene 1 record/ora per dati > N giorni e 1 record/giorno per dati > 2N giorni."
    )

    def execute(self, db, entity_ids, params, dry_run=False,
                backup_path=None, backup_before=False, batch_size=5000):
        older_than_days = int(params.get("older_than_days", 14))
        backup_dest = None
        if not dry_run:
            backup_dest = self._maybe_backup(db, backup_path, backup_before)

        results = []
        for eid in entity_ids:
            try:
                # Fase 1: appiattimento orario per dati > older_than_days
                r1 = db.flatten_entity(
                    eid, older_than_days, granularity="hour",
                    dry_run=dry_run, batch_size=batch_size
                )
                # Fase 2: appiattimento giornaliero per dati > 2 * older_than_days
                r2 = db.flatten_entity(
                    eid, older_than_days * 2, granularity="day",
                    dry_run=dry_run, batch_size=batch_size
                )
                results.append({
                    "entity_id": eid,
                    "phase_hourly": r1,
                    "phase_daily": r2,
                    "total_deleted": (
                        r1.get("deleted", r1.get("estimated_deleted", 0)) +
                        r2.get("deleted", r2.get("estimated_deleted", 0))
                    ),
                })
                logger.info(f"[TemporalDecimation] {eid}: completato")
            except Exception as e:
                logger.error(f"[TemporalDecimation] Errore su {eid}: {e}")
                results.append({"entity_id": eid, "error": str(e)})

        total_deleted = sum(r.get("total_deleted", 0) for r in results if "total_deleted" in r)
        return {
            "strategy": self.name,
            "dry_run": dry_run,
            "params": params,
            "entity_count": len(entity_ids),
            "total_deleted": total_deleted,
            "backup": backup_dest,
            "details": results,
        }


# ---------------------------------------------------------------------------
# Strategia 3 - Media Mobile (Rolling Average)
# ---------------------------------------------------------------------------

class RollingAverage(Strategy):
    """
    Sostituisce i valori originali con medie orarie o giornaliere
    man mano che i dati invecchiano.
    Solo per sensori numerici.
    """
    name = "rolling_average"
    label = "Media Mobile"
    description = (
        "Sostituisce i valori originali con medie (orarie o giornaliere) "
        "per i dati più vecchi di N giorni."
    )

    def execute(self, db, entity_ids, params, dry_run=False,
                backup_path=None, backup_before=False, batch_size=5000):
        older_than_days = int(params.get("older_than_days", 7))
        granularity = params.get("granularity", "hour")  # "hour" o "day"
        backup_dest = None
        if not dry_run:
            backup_dest = self._maybe_backup(db, backup_path, backup_before)

        results = []
        for eid in entity_ids:
            try:
                stats = db.get_sensor_stats(eid)
                if stats and not stats.get("is_numeric", False):
                    results.append({
                        "entity_id": eid,
                        "skipped": True,
                        "reason": "Sensore non numerico - strategia inapplicabile",
                    })
                    continue

                r = db.flatten_entity(
                    eid, older_than_days, granularity=granularity,
                    dry_run=dry_run, batch_size=batch_size
                )
                r["entity_id"] = eid
                results.append(r)
                logger.info(f"[RollingAverage] {eid}: "
                            f"{'(DRY) ' if dry_run else ''}"
                            f"~{r.get('deleted', r.get('estimated_deleted', 0))} eliminati")
            except Exception as e:
                logger.error(f"[RollingAverage] Errore su {eid}: {e}")
                results.append({"entity_id": eid, "error": str(e)})

        total_deleted = sum(
            r.get("deleted", r.get("estimated_deleted", 0))
            for r in results
            if not r.get("skipped")
        )
        return {
            "strategy": self.name,
            "dry_run": dry_run,
            "params": params,
            "entity_count": len(entity_ids),
            "total_deleted": total_deleted,
            "backup": backup_dest,
            "details": results,
        }


# ---------------------------------------------------------------------------
# Strategia 4 - Purge Adattivo
# ---------------------------------------------------------------------------

class AdaptivePurge(Strategy):
    """
    Purge intelligente multi-fascia:
    - < soglia_1 giorni: mantieni tutto
    - soglia_1 ~ soglia_2 giorni: 1 record/ora
    - > soglia_2 giorni: 1 record/giorno
    - > soglia_3 giorni: elimina completamente
    Offre il massimo controllo sulla storicità.
    """
    name = "adaptive_purge"
    label = "Purge Adattivo"
    description = (
        "Purge multi-fascia: mantieni tutto < soglia_1 giorni, "
        "appiattisci progressivamente, elimina completamente > soglia_3 giorni."
    )

    def execute(self, db, entity_ids, params, dry_run=False,
                backup_path=None, backup_before=False, batch_size=5000):
        threshold_1 = int(params.get("threshold_1_days", 7))    # tutto
        threshold_2 = int(params.get("threshold_2_days", 30))   # orario
        threshold_3 = int(params.get("threshold_3_days", 90))   # giornaliero
        threshold_4 = int(params.get("threshold_4_days", 365))  # eliminazione
        backup_dest = None
        if not dry_run:
            backup_dest = self._maybe_backup(db, backup_path, backup_before)

        results = []
        for eid in entity_ids:
            entity_result = {"entity_id": eid, "phases": []}
            total_deleted = 0
            try:
                # Fase A: appiattimento orario (threshold_1 ~ threshold_2)
                if threshold_2 > threshold_1:
                    r = db.flatten_entity(
                        eid, threshold_1, granularity="hour",
                        dry_run=dry_run, batch_size=batch_size
                    )
                    d = r.get("deleted", r.get("estimated_deleted", 0))
                    total_deleted += d
                    entity_result["phases"].append({"label": f"Orario (>{threshold_1}gg)", "deleted": d})

                # Fase B: appiattimento giornaliero (threshold_2 ~ threshold_3)
                if threshold_3 > threshold_2:
                    r = db.flatten_entity(
                        eid, threshold_2, granularity="day",
                        dry_run=dry_run, batch_size=batch_size
                    )
                    d = r.get("deleted", r.get("estimated_deleted", 0))
                    total_deleted += d
                    entity_result["phases"].append({"label": f"Giornaliero (>{threshold_2}gg)", "deleted": d})

                # Fase C: eliminazione completa (> threshold_4)
                if threshold_4 > threshold_3:
                    r = db.purge_entity(
                        eid, threshold_4, dry_run=dry_run, batch_size=batch_size
                    )
                    d = r.get("deleted", r.get("estimated", 0))
                    total_deleted += d
                    entity_result["phases"].append({"label": f"Eliminazione (>{threshold_4}gg)", "deleted": d})

                entity_result["total_deleted"] = total_deleted
                results.append(entity_result)
                logger.info(f"[AdaptivePurge] {eid}: {total_deleted} eliminati")
            except Exception as e:
                logger.error(f"[AdaptivePurge] Errore su {eid}: {e}")
                results.append({"entity_id": eid, "error": str(e)})

        total = sum(r.get("total_deleted", 0) for r in results if "total_deleted" in r)
        return {
            "strategy": self.name,
            "dry_run": dry_run,
            "params": params,
            "entity_count": len(entity_ids),
            "total_deleted": total,
            "backup": backup_dest,
            "details": results,
        }


# ---------------------------------------------------------------------------
# Strategia 5 - Outlier Purge (Rimozione Anomalie)
# ---------------------------------------------------------------------------

class OutlierPurge(Strategy):
    """
    Rimuove valori anomali/impossibili da sensori numerici.
    Criteri configurabili:
      - Valori negativi (quando fisicamente impossibili)
      - Valori fuori range assoluto [min_value, max_value]
      - Valori statistici fuori N deviazioni standard dalla media
      - Stati specifici da eliminare (es. 'unavailable' prolungati)
    """
    name = "outlier_purge"
    label = "Rimozione Anomalie"
    description = (
        "Elimina valori impossibili o anomali: negativi, fuori range accettabile, "
        "o fuori N deviazioni standard dalla media storica."
    )

    def execute(self, db, entity_ids, params, dry_run=False,
                backup_path=None, backup_before=False, batch_size=5000):
        remove_negative = params.get("remove_negative", False)
        min_value = params.get("min_value")
        max_value = params.get("max_value")
        std_mult = params.get("std_dev_multiplier")
        state_blacklist = params.get("state_blacklist", [])

        if not any([remove_negative, min_value is not None, max_value is not None,
                    std_mult is not None, state_blacklist]):
            return {
                "strategy": self.name, "dry_run": dry_run, "params": params,
                "entity_count": 0, "total_deleted": 0,
                "error": "Nessun criterio di anomalia specificato",
                "details": [],
            }

        backup_dest = None
        if not dry_run:
            backup_dest = self._maybe_backup(db, backup_path, backup_before)

        results = []
        for eid in entity_ids:
            try:
                criteria = {
                    "remove_negative": remove_negative,
                    "min_value": min_value,
                    "max_value": max_value,
                    "std_dev_multiplier": std_mult,
                    "state_blacklist": state_blacklist,
                }
                if dry_run:
                    r = db.preview_anomalies(eid, criteria)
                    results.append({
                        "entity_id": eid,
                        "estimated": r.get("count", 0),
                        "samples": r.get("samples", []),
                        "dry_run": True,
                    })
                else:
                    r = db.delete_anomalies(eid, criteria, batch_size=batch_size)
                    results.append({
                        "entity_id": eid,
                        "deleted": r.get("deleted", 0),
                        "total_found": r.get("total_found", 0),
                    })
                logger.info(f"[OutlierPurge] {eid}: {'(DRY) ' if dry_run else ''}"
                            f"{r.get('count', r.get('deleted', 0))} record anomali")
            except Exception as e:
                logger.error(f"[OutlierPurge] Errore su {eid}: {e}")
                results.append({"entity_id": eid, "error": str(e)})

        key = "estimated" if dry_run else "deleted"
        total_deleted = sum(r.get(key, 0) for r in results if key in r)
        return {
            "strategy": self.name,
            "dry_run": dry_run,
            "params": params,
            "entity_count": len(entity_ids),
            "total_deleted": total_deleted,
            "backup": backup_dest,
            "details": results,
        }


# ---------------------------------------------------------------------------
# Registry e factory
# ---------------------------------------------------------------------------

STRATEGY_REGISTRY = {
    SimplePurge.name: SimplePurge,
    TemporalDecimation.name: TemporalDecimation,
    RollingAverage.name: RollingAverage,
    AdaptivePurge.name: AdaptivePurge,
    OutlierPurge.name: OutlierPurge,
}

STRATEGY_LIST = [
    {
        "name": SimplePurge.name,
        "label": SimplePurge.label,
        "description": SimplePurge.description,
        "params": [
            {"key": "older_than_days", "label": "Elimina record più vecchi di (giorni)",
             "type": "number", "default": 30, "min": 1},
        ],
    },
    {
        "name": TemporalDecimation.name,
        "label": TemporalDecimation.label,
        "description": TemporalDecimation.description,
        "params": [
            {"key": "older_than_days", "label": "Soglia appiattimento orario (giorni)",
             "type": "number", "default": 14, "min": 1},
        ],
    },
    {
        "name": RollingAverage.name,
        "label": RollingAverage.label,
        "description": RollingAverage.description,
        "params": [
            {"key": "older_than_days", "label": "Applica media a dati più vecchi di (giorni)",
             "type": "number", "default": 7, "min": 1},
            {"key": "granularity", "label": "Granularità media",
             "type": "select", "options": ["hour", "day"], "default": "hour"},
        ],
    },
    {
        "name": AdaptivePurge.name,
        "label": AdaptivePurge.label,
        "description": AdaptivePurge.description,
        "params": [
            {"key": "threshold_1_days", "label": "Appiattimento orario dopo (giorni)",
             "type": "number", "default": 7, "min": 1},
            {"key": "threshold_2_days", "label": "Appiattimento giornaliero dopo (giorni)",
             "type": "number", "default": 30, "min": 1},
            {"key": "threshold_3_days", "label": "Eliminazione completa dopo (giorni)",
             "type": "number", "default": 365, "min": 1},
        ],
    },
    {
        "name": OutlierPurge.name,
        "label": OutlierPurge.label,
        "description": OutlierPurge.description,
        "params": [
            {"key": "remove_negative", "label": "Elimina valori negativi",
             "type": "boolean", "default": False},
            {"key": "min_value", "label": "Valore minimo accettabile (opzionale)",
             "type": "number", "default": None, "optional": True},
            {"key": "max_value", "label": "Valore massimo accettabile (opzionale)",
             "type": "number", "default": None, "optional": True},
            {"key": "std_dev_multiplier", "label": "Soglia deviazione standard (N sigma, opzionale)",
             "type": "number", "default": None, "min": 0.5, "optional": True},
            {"key": "state_blacklist", "label": "Stati da eliminare (es. unavailable, unknown)",
             "type": "list", "default": [], "optional": True},
        ],
    },
]


def execute_strategy(
    db: HaDatabase,
    strategy_name: str,
    entity_ids: list[str],
    params: dict,
    dry_run: bool = False,
    backup_path: str = None,
    backup_before: bool = False,
    batch_size: int = 5000,
) -> dict:
    """Esegue una strategia per nome."""
    cls = STRATEGY_REGISTRY.get(strategy_name)
    if not cls:
        return {"error": f"Strategia sconosciuta: {strategy_name}"}
    return cls().execute(
        db, entity_ids, params, dry_run=dry_run,
        backup_path=backup_path, backup_before=backup_before,
        batch_size=batch_size,
    )
