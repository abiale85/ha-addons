"""
HistoLite - Strategie di ottimizzazione
Definizione ed esecuzione delle 4 strategie disponibili.
"""

import logging
import time
from abc import ABC, abstractmethod
from database import HaDatabase

logger = logging.getLogger(__name__)


def _run_with_retry(strategy_label: str, entity_id: str, operation, retry_attempts: int = 2, retry_delay_sec: float = 1.0):
    """Esegue un'operazione su una singola entità con retry limitati."""
    attempts = max(1, int(retry_attempts))
    delay = max(0.0, float(retry_delay_sec))
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            if attempt > 1:
                logger.info(f"[{strategy_label}] Retry {attempt}/{attempts} su {entity_id}")
            return operation(attempt)
        except Exception as e:
            last_error = e
            if attempt >= attempts:
                raise
            logger.warning(
                f"[{strategy_label}] Fallimento su {entity_id} (tentativo {attempt}/{attempts}): {e}; retry tra {delay}s"
            )
            time.sleep(delay)
    if last_error is not None:
        raise last_error


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
        batch_size: int = 5000,
        cancel_event = None,
    ) -> dict:
        ...


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

    def execute(self, db, entity_ids, params, dry_run=False, batch_size=5000, cancel_event=None):
        older_than_days = int(params.get("older_than_days", 30))
        retry_attempts = int(params.get("retry_attempts", 2))
        retry_delay_sec = float(params.get("retry_delay_sec", 1.0))
        results = []
        for eid in entity_ids:
            if cancel_event and cancel_event.is_set():
                logger.info(f"[SimplePurge] Cancellazione richiesta, interrotto a {eid}")
                break
            try:
                r = _run_with_retry(
                    "SimplePurge",
                    eid,
                    lambda attempt: db.purge_entity(eid, older_than_days, dry_run=dry_run, batch_size=batch_size),
                    retry_attempts=retry_attempts,
                    retry_delay_sec=retry_delay_sec,
                )
                r["entity_id"] = eid
                results.append(r)
                logger.info(f"[SimplePurge] {eid}: {'(DRY) ' if dry_run else ''}"
                            f"~{r.get('estimated', r.get('deleted', 0))} record")
            except Exception as e:
                logger.error(f"[SimplePurge] Errore su {eid} dopo {retry_attempts} tentativi: {e}")
                results.append({"entity_id": eid, "error": str(e)})

        total_deleted = sum(r.get("deleted", r.get("estimated", 0)) for r in results)
        return {
            "strategy": self.name,
            "dry_run": dry_run,
            "params": params,
            "entity_count": len(entity_ids),
            "total_deleted": total_deleted,
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

    def execute(self, db, entity_ids, params, dry_run=False, batch_size=5000, cancel_event=None):
        older_than_days = int(params.get("older_than_days", 14))
        retry_attempts = int(params.get("retry_attempts", 2))
        retry_delay_sec = float(params.get("retry_delay_sec", 1.0))
        results = []
        for eid in entity_ids:
            if cancel_event and cancel_event.is_set():
                logger.info(f"[TemporalDecimation] Cancellazione richiesta, interrotto a {eid}")
                break
            try:
                def _op(_attempt: int):
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
                    return r1, r2

                r1, r2 = _run_with_retry(
                    "TemporalDecimation",
                    eid,
                    _op,
                    retry_attempts=retry_attempts,
                    retry_delay_sec=retry_delay_sec,
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
                logger.error(f"[TemporalDecimation] Errore su {eid} dopo {retry_attempts} tentativi: {e}")
                results.append({"entity_id": eid, "error": str(e)})

        total_deleted = sum(r.get("total_deleted", 0) for r in results if "total_deleted" in r)
        return {
            "strategy": self.name,
            "dry_run": dry_run,
            "params": params,
            "entity_count": len(entity_ids),
            "total_deleted": total_deleted,
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

    def execute(self, db, entity_ids, params, dry_run=False, batch_size=5000, cancel_event=None):
        older_than_days = int(params.get("older_than_days", 7))
        granularity = params.get("granularity", "hour")  # "hour" o "day"
        retry_attempts = int(params.get("retry_attempts", 2))
        retry_delay_sec = float(params.get("retry_delay_sec", 1.0))
        results = []
        for eid in entity_ids:
            if cancel_event and cancel_event.is_set():
                logger.info(f"[RollingAverage] Cancellazione richiesta, interrotto a {eid}")
                break
            try:
                def _op(_attempt: int):
                    stats = db.get_sensor_stats(eid)
                    if stats and not stats.get("is_numeric", False):
                        return {
                            "entity_id": eid,
                            "skipped": True,
                            "reason": "Sensore non numerico - strategia inapplicabile",
                        }

                    r = db.flatten_entity(
                        eid, older_than_days, granularity=granularity,
                        dry_run=dry_run, batch_size=batch_size
                    )
                    r["entity_id"] = eid
                    return r

                r = _run_with_retry(
                    "RollingAverage",
                    eid,
                    _op,
                    retry_attempts=retry_attempts,
                    retry_delay_sec=retry_delay_sec,
                )
                if r.get("skipped"):
                    results.append(r)
                    continue
                r["entity_id"] = eid
                results.append(r)
                logger.info(f"[RollingAverage] {eid}: "
                            f"{'(DRY) ' if dry_run else ''}"
                            f"~{r.get('deleted', r.get('estimated_deleted', 0))} eliminati")
            except Exception as e:
                logger.error(f"[RollingAverage] Errore su {eid} dopo {retry_attempts} tentativi: {e}")
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

    def execute(self, db, entity_ids, params, dry_run=False, batch_size=5000, cancel_event=None):
        threshold_1 = int(params.get("threshold_1_days", 7))    # tutto
        threshold_2 = int(params.get("threshold_2_days", 30))   # orario
        threshold_3 = int(params.get("threshold_3_days", 90))   # giornaliero
        threshold_4 = int(params.get("threshold_4_days", 365))  # eliminazione
        retry_attempts = int(params.get("retry_attempts", 2))
        retry_delay_sec = float(params.get("retry_delay_sec", 1.0))
        results = []
        for eid in entity_ids:
            if cancel_event and cancel_event.is_set():
                logger.info(f"[AdaptivePurge] Cancellazione richiesta, interrotto a {eid}")
                break
            try:
                def _op(_attempt: int):
                    entity_result = {"entity_id": eid, "phases": []}
                    total_deleted = 0

                    if threshold_2 > threshold_1:
                        r = db.flatten_entity(
                            eid, threshold_1, granularity="hour",
                            dry_run=dry_run, batch_size=batch_size
                        )
                        d = r.get("deleted", r.get("estimated_deleted", 0))
                        total_deleted += d
                        entity_result["phases"].append({"label": f"Orario (>{threshold_1}gg)", "deleted": d})

                    if threshold_3 > threshold_2:
                        r = db.flatten_entity(
                            eid, threshold_2, granularity="day",
                            dry_run=dry_run, batch_size=batch_size
                        )
                        d = r.get("deleted", r.get("estimated_deleted", 0))
                        total_deleted += d
                        entity_result["phases"].append({"label": f"Giornaliero (>{threshold_2}gg)", "deleted": d})

                    if threshold_4 > threshold_3:
                        r = db.purge_entity(
                            eid, threshold_4, dry_run=dry_run, batch_size=batch_size
                        )
                        d = r.get("deleted", r.get("estimated", 0))
                        total_deleted += d
                        entity_result["phases"].append({"label": f"Eliminazione (>{threshold_4}gg)", "deleted": d})

                    entity_result["total_deleted"] = total_deleted
                    return entity_result

                entity_result = _run_with_retry(
                    "AdaptivePurge",
                    eid,
                    _op,
                    retry_attempts=retry_attempts,
                    retry_delay_sec=retry_delay_sec,
                )
                results.append(entity_result)
                logger.info(f"[AdaptivePurge] {eid}: {entity_result.get('total_deleted', 0)} eliminati")
            except Exception as e:
                logger.error(f"[AdaptivePurge] Errore su {eid} dopo {retry_attempts} tentativi: {e}")
                results.append({"entity_id": eid, "error": str(e)})

        total = sum(r.get("total_deleted", 0) for r in results if "total_deleted" in r)
        return {
            "strategy": self.name,
            "dry_run": dry_run,
            "params": params,
            "entity_count": len(entity_ids),
            "total_deleted": total,
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

    def execute(self, db, entity_ids, params, dry_run=False, batch_size=5000, cancel_event=None):
        remove_negative = params.get("remove_negative", False)
        min_value = params.get("min_value")
        max_value = params.get("max_value")
        std_mult = params.get("std_dev_multiplier")
        state_blacklist = params.get("state_blacklist", [])
        retry_attempts = int(params.get("retry_attempts", 2))
        retry_delay_sec = float(params.get("retry_delay_sec", 1.0))

        if not any([remove_negative, min_value is not None, max_value is not None,
                    std_mult is not None, state_blacklist]):
            return {
                "strategy": self.name, "dry_run": dry_run, "params": params,
                "entity_count": 0, "total_deleted": 0,
                "error": "Nessun criterio di anomalia specificato",
                "details": [],
            }

        results = []
        for eid in entity_ids:
            if cancel_event and cancel_event.is_set():
                logger.info(f"[OutlierPurge] Cancellazione richiesta, interrotto a {eid}")
                break
            try:
                criteria = {
                    "remove_negative": remove_negative,
                    "min_value": min_value,
                    "max_value": max_value,
                    "std_dev_multiplier": std_mult,
                    "state_blacklist": state_blacklist,
                }
                def _op(_attempt: int):
                    if dry_run:
                        r = db.preview_anomalies(eid, criteria)
                        return {
                            "entity_id": eid,
                            "estimated": r.get("count", 0),
                            "samples": r.get("samples", []),
                            "dry_run": True,
                        }
                    r = db.delete_anomalies(eid, criteria, batch_size=batch_size)
                    return {
                        "entity_id": eid,
                        "deleted": r.get("deleted", 0),
                        "total_found": r.get("total_found", 0),
                    }

                r = _run_with_retry(
                    "OutlierPurge",
                    eid,
                    _op,
                    retry_attempts=retry_attempts,
                    retry_delay_sec=retry_delay_sec,
                )
                results.append(r)
                logger.info(f"[OutlierPurge] {eid}: {'(DRY) ' if dry_run else ''}"
                            f"{r.get('count', r.get('deleted', 0))} record anomali")
            except Exception as e:
                logger.error(f"[OutlierPurge] Errore su {eid} dopo {retry_attempts} tentativi: {e}")
                results.append({"entity_id": eid, "error": str(e)})

        key = "estimated" if dry_run else "deleted"
        total_deleted = sum(r.get(key, 0) for r in results if key in r)
        return {
            "strategy": self.name,
            "dry_run": dry_run,
            "params": params,
            "entity_count": len(entity_ids),
            "total_deleted": total_deleted,
            "details": results,
        }


# ---------------------------------------------------------------------------
# Strategia 6 - Peak Decimation (Massimo per bucket — contatori/crescita)
# ---------------------------------------------------------------------------

class PeakDecimation(Strategy):
    """
    Per sensori a crescita continua (contatori energia, gas, acqua…):
    mantiene il VALORE MASSIMO di ogni bucket temporale anziché la media.

    Vantaggi rispetto alla media mobile:
    - Non distorce le letture cumulative (la media di un contatore non ha senso)
    - Preserva il picco raggiunto nel periodo
    - Gestisce automaticamente i reset periodici del contatore
    """
    name = "peak_decimation"
    label = "Picco per Bucket (Contatori)"
    description = (
        "Per sensori in crescita continua (energia, gas, acqua…): "
        "mantiene il valore MASSIMO per ogni bucket orario/giornaliero. "
        "Rileva e preserva automaticamente i punti di reset."
    )

    def execute(self, db, entity_ids, params, dry_run=False, batch_size=5000, cancel_event=None):
        older_than_days = int(params.get("older_than_days", 7))
        granularity = params.get("granularity", "hour")
        keep_resets = bool(params.get("keep_resets", True))
        reset_threshold_pct = float(params.get("reset_threshold_pct", 50.0))
        retry_attempts = int(params.get("retry_attempts", 2))
        retry_delay_sec = float(params.get("retry_delay_sec", 1.0))
        results = []
        for eid in entity_ids:
            if cancel_event and cancel_event.is_set():
                logger.info(f"[PeakDecimation] Cancellazione richiesta, interrotto a {eid}")
                break
            try:
                def _op(_attempt: int):
                    stats = db.get_sensor_stats(eid)
                    if stats and not stats.get("is_numeric", False):
                        return {
                            "entity_id": eid,
                            "skipped": True,
                            "reason": "Sensore non numerico - strategia inapplicabile",
                        }

                    r = db.peak_decimate_entity(
                        eid, older_than_days,
                        granularity=granularity,
                        keep_resets=keep_resets,
                        reset_threshold_pct=reset_threshold_pct,
                        dry_run=dry_run,
                        batch_size=batch_size,
                    )
                    r["entity_id"] = eid
                    return r

                r = _run_with_retry(
                    "PeakDecimation",
                    eid,
                    _op,
                    retry_attempts=retry_attempts,
                    retry_delay_sec=retry_delay_sec,
                )
                if r.get("skipped"):
                    results.append(r)
                    continue
                r["entity_id"] = eid
                results.append(r)
                reset_info = f", {r.get('reset_points', 0)} reset preservati" if keep_resets else ""
                logger.info(
                    f"[PeakDecimation] {eid}: "
                    f"{'(DRY) ' if dry_run else ''}"
                    f"~{r.get('deleted', r.get('estimated_deleted', 0))} eliminati"
                    f"{reset_info}"
                )
            except Exception as e:
                logger.error(f"[PeakDecimation] Errore su {eid} dopo {retry_attempts} tentativi: {e}")
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
    PeakDecimation.name: PeakDecimation,
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
    {
        "name": PeakDecimation.name,
        "label": PeakDecimation.label,
        "description": PeakDecimation.description,
        "params": [
            {"key": "older_than_days", "label": "Applica a dati più vecchi di (giorni)",
             "type": "number", "default": 7, "min": 1},
            {"key": "granularity", "label": "Granularità bucket",
             "type": "select", "options": ["hour", "day"], "default": "hour"},
            {"key": "keep_resets", "label": "Preserva punti di reset automaticamente",
             "type": "boolean", "default": True},
            {"key": "reset_threshold_pct", "label": "Soglia reset (% calo per rilevare reset)",
             "type": "number", "default": 50.0, "min": 5, "max": 99},
        ],
    },
]


def execute_strategy(
    db: HaDatabase,
    strategy_name: str,
    entity_ids: list[str],
    params: dict,
    dry_run: bool = False,
    batch_size: int = 5000,
    cancel_event = None,
) -> dict:
    """Esegue una strategia per nome.
    cancel_event: threading.Event per richiedere interruzione della strategia."""
    cls = STRATEGY_REGISTRY.get(strategy_name)
    if not cls:
        return {"error": f"Strategia sconosciuta: {strategy_name}"}
    strategy = cls()
    logger.info(f"[StrategyStart] name={strategy_name} entities={len(entity_ids)} dry_run={dry_run}")
    t0 = time.time()
    try:
        res = strategy.execute(
            db, entity_ids, params, dry_run=dry_run,
            batch_size=batch_size, cancel_event=cancel_event,
        )
        elapsed = time.time() - t0
        # try to extract basic summary info
        total_deleted = res.get("total_deleted") if isinstance(res, dict) else None
        logger.info(f"[StrategyEnd] name={strategy_name} elapsed_s={elapsed:.2f} total_deleted={total_deleted}")
        return res
    except Exception as e:
        elapsed = time.time() - t0
        logger.error(f"[StrategyError] name={strategy_name} elapsed_s={elapsed:.2f} error={e}")
        raise
