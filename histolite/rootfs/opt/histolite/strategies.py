"""
HistoLite - Strategie di ottimizzazione
Definizione ed esecuzione delle 5 strategie disponibili.
"""

import logging
import time
from abc import ABC, abstractmethod
from database import HaDatabase

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helper parametri: intervalli temporali e aggregazioni
# ---------------------------------------------------------------------------

_UNIT_SECONDS = {
    "minute": 60, "minutes": 60, "min": 60, "m": 60,
    "hour": 3600, "hours": 3600, "h": 3600,
    "day": 86400, "days": 86400, "d": 86400,
    "week": 604800, "weeks": 604800, "w": 604800,
}

# Funzioni di aggregazione esposte all'utente per l'appiattimento dei bucket.
AGG_CHOICES = (
    "time_weighted_mean", "mean", "median", "mode",
    "min", "max", "first", "last", "percentile",
)


def parse_interval_to_seconds(spec, default_seconds: int) -> int:
    """Converte una specifica di intervallo in secondi.

    Accetta: ``{"every": N, "unit": "minute|hour|day|week"}``, un intero/float
    di secondi, le stringhe legacy ``"hour"``/``"day"``, oppure ``None``.
    ``every`` <= 0 restituisce 0 (= funzionalità disattivata).
    """
    if spec is None:
        return int(default_seconds)
    if isinstance(spec, bool):  # evita che True/False passino come int
        return int(default_seconds)
    if isinstance(spec, (int, float)):
        return int(spec) if spec > 0 else 0
    if isinstance(spec, str):
        s = spec.strip().lower()
        if s in ("hour", "hourly", "ora", "oraria"):
            return 3600
        if s in ("day", "daily", "giorno", "giornaliera"):
            return 86400
        if s.isdigit():
            return int(s)
        return int(default_seconds)
    if isinstance(spec, dict):
        every = spec.get("every", spec.get("value", 1))
        unit = str(spec.get("unit", "hour")).lower()
        try:
            every = float(every)
        except (TypeError, ValueError):
            return int(default_seconds)
        if every <= 0:
            return 0
        return int(round(every * _UNIT_SECONDS.get(unit, 3600)))
    return int(default_seconds)


def normalize_agg(params: dict, default: str = "time_weighted_mean") -> tuple[str, float]:
    """Estrae ``(agg, agg_pct)`` validati da un dizionario di parametri."""
    agg = str(params.get("agg", default) or default).lower()
    if agg not in AGG_CHOICES:
        agg = default
    try:
        pct = float(params.get("agg_pct", 95) or 95)
    except (TypeError, ValueError):
        pct = 95.0
    return agg, min(100.0, max(0.0, pct))


def normalize_tiers(params: dict) -> list[dict]:
    """Normalizza le fasce del Purge Adattivo.

    Se ``params['tiers']`` è presente lo usa; altrimenti migra i vecchi
    parametri ``threshold_1_days``/``threshold_2_days``/``threshold_3_days``
    (+ ``threshold_4_days``) all'equivalente in fasce, replicando le condizioni
    del motore storico.
    """
    raw = params.get("tiers")
    tiers: list[dict] = []

    if isinstance(raw, list) and raw:
        for t in raw:
            if not isinstance(t, dict):
                continue
            after_raw = t.get("after_days")
            try:
                after = int(after_raw)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
            action = str(t.get("action", "flatten")).lower()
            if action not in ("flatten", "delete"):
                action = "flatten"
            entry = {"after_days": after, "action": action}
            if action == "flatten":
                entry["bucket_seconds"] = parse_interval_to_seconds(t.get("bucket"), 3600) or 3600
                entry["agg"], entry["agg_pct"] = normalize_agg(t)
            tiers.append(entry)
    else:
        def _int(v):
            try:
                return int(v)
            except (TypeError, ValueError):
                return None

        t1, t2 = _int(params.get("threshold_1_days")), _int(params.get("threshold_2_days"))
        t3, t4 = _int(params.get("threshold_3_days")), _int(params.get("threshold_4_days"))
        if t1 is not None and t2 is not None and t2 > t1:
            tiers.append({"after_days": t1, "action": "flatten",
                          "bucket_seconds": 3600, "agg": "time_weighted_mean", "agg_pct": 95.0})
        if t2 is not None and t3 is not None and t3 > t2:
            tiers.append({"after_days": t2, "action": "flatten",
                          "bucket_seconds": 86400, "agg": "time_weighted_mean", "agg_pct": 95.0})
        if t4 is not None and t3 is not None and t3 < t4 < 36500:
            tiers.append({"after_days": t4, "action": "delete"})
        elif t4 is None and t3 is not None:
            tiers.append({"after_days": t3, "action": "delete"})

    tiers.sort(key=lambda x: x["after_days"])
    return tiers


def _bucket_label(seconds: int) -> str:
    """Etichetta breve in italiano per una dimensione di bucket."""
    if seconds % 604800 == 0 and seconds >= 604800:
        n = seconds // 604800
        return "Settimanale" if n == 1 else f"Ogni {n} settimane"
    if seconds % 86400 == 0 and seconds >= 86400:
        n = seconds // 86400
        return "Giornaliero" if n == 1 else f"Ogni {n} giorni"
    if seconds % 3600 == 0 and seconds >= 3600:
        n = seconds // 3600
        return "Orario" if n == 1 else f"Ogni {n} ore"
    n = max(1, seconds // 60)
    return f"Ogni {n} min"


def cleanup_attributes_enabled(params: dict) -> bool:
    """``cleanup_attributes`` è attivo per default; solo un valore falso lo disattiva."""
    return params.get("cleanup_attributes", True) is not False


def _attr_count(r: dict) -> int:
    """Righe state_attributes toccate da un risultato (reali o stimate)."""
    try:
        return int(r.get("attr_deleted", r.get("attr_estimated", 0)) or 0)
    except (TypeError, ValueError):
        return 0


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
        cleanup_attributes = cleanup_attributes_enabled(params)
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
                    lambda attempt: db.purge_entity(
                        eid, older_than_days, dry_run=dry_run, batch_size=batch_size,
                        cleanup_attributes=cleanup_attributes,
                    ),
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
            "total_attr_removed": sum(_attr_count(r) for r in results),
            "details": results,
        }


# ---------------------------------------------------------------------------
# Strategia 2 - Purge Adattivo
# ---------------------------------------------------------------------------

class AdaptivePurge(Strategy):
    """
    Purge intelligente a fasce multiple, in un unico passaggio.

    Ogni fascia parte da un'età in giorni (``after_days``) e sceglie cosa fare
    dei dati più vecchi di quella soglia:
    - ``flatten``: 1 record per bucket temporale (``bucket``: ogni X minuti/ore/
      giorni/settimane), col valore calcolato dall'aggregazione scelta (``agg``:
      media pesata sul tempo, media, mediana, moda, min, max, primo, ultimo,
      percentile);
    - ``delete``: eliminazione completa.
    Numero di fasce libero; ciò che è più recente della prima fascia è intatto.
    """
    name = "adaptive_purge"
    label = "Purge Adattivo"
    description = (
        "Purge a fasce multiple: numero di fasce libero, bucket configurabile "
        "(ogni X minuti/ore/giorni/settimane) e aggregazione scelta per fascia."
    )

    def execute(self, db, entity_ids, params, dry_run=False, batch_size=5000, cancel_event=None):
        tiers = normalize_tiers(params)
        cleanup_attributes = cleanup_attributes_enabled(params)
        retry_attempts = int(params.get("retry_attempts", 2))
        retry_delay_sec = float(params.get("retry_delay_sec", 1.0))
        results = []

        if not tiers:
            return {
                "strategy": self.name, "dry_run": dry_run, "params": params,
                "entity_count": len(entity_ids), "total_deleted": 0,
                "error": "Nessuna fascia valida configurata", "details": [],
            }

        for eid in entity_ids:
            if cancel_event and cancel_event.is_set():
                logger.info(f"[AdaptivePurge] Cancellazione richiesta, interrotto a {eid}")
                break
            try:
                def _op(_attempt: int):
                    entity_result = {"entity_id": eid, "phases": []}
                    total_deleted = 0
                    total_attr = 0
                    for tier in tiers:
                        after = tier["after_days"]
                        if tier["action"] == "delete":
                            r = db.purge_entity(
                                eid, after, dry_run=dry_run, batch_size=batch_size,
                                cleanup_attributes=cleanup_attributes,
                            )
                            d = r.get("estimated", 0) if dry_run else r.get("deleted", 0)
                            label = f"Eliminazione (>{after}gg)"
                        else:
                            r = db.flatten_entity(
                                eid, after,
                                bucket_seconds=tier["bucket_seconds"],
                                agg=tier["agg"], agg_pct=tier["agg_pct"],
                                dry_run=dry_run, batch_size=batch_size,
                                cleanup_attributes=cleanup_attributes,
                            )
                            d = r.get("estimated_deleted", 0) if dry_run else r.get("deleted", 0)
                            label = (
                                f"{_bucket_label(tier['bucket_seconds'])} · "
                                f"{tier['agg']} (>{after}gg)"
                            )
                        a = _attr_count(r)
                        total_deleted += d
                        total_attr += a
                        entity_result["phases"].append(
                            {"label": label, "deleted": d, "attr_removed": a}
                        )

                    entity_result["total_deleted"] = total_deleted
                    entity_result["attr_removed"] = total_attr
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
            "total_attr_removed": sum(r.get("attr_removed", 0) for r in results),
            "details": results,
        }


# ---------------------------------------------------------------------------
# Strategia 3 - Outlier Purge (Rimozione Anomalie)
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
        cleanup_attributes = cleanup_attributes_enabled(params)
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
                            "attr_estimated": r.get("attr_estimated", 0),
                            "samples": r.get("samples", []),
                            "dry_run": True,
                        }
                    r = db.delete_anomalies(
                        eid, criteria, batch_size=batch_size,
                        cleanup_attributes=cleanup_attributes,
                    )
                    return {
                        "entity_id": eid,
                        "deleted": r.get("deleted", 0),
                        "attr_deleted": r.get("attr_deleted", 0),
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
            "total_attr_removed": sum(_attr_count(r) for r in results),
            "details": results,
        }


# ---------------------------------------------------------------------------
# Strategia 4 - Peak Decimation (Massimo per bucket — contatori/crescita)
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
        "mantiene per ogni bucket (ogni X minuti/ore/giorni/settimane) il valore "
        "aggregato scelto — di default il MASSIMO. Rileva e preserva i reset."
    )

    def execute(self, db, entity_ids, params, dry_run=False, batch_size=5000, cancel_event=None):
        older_than_days = int(params.get("older_than_days", 7))
        if params.get("bucket") is not None:
            bucket_seconds = parse_interval_to_seconds(params.get("bucket"), 3600)
        else:
            bucket_seconds = parse_interval_to_seconds(params.get("granularity"), 3600)
        bucket_seconds = bucket_seconds or 3600
        agg, agg_pct = normalize_agg(params, default="max")
        cleanup_attributes = cleanup_attributes_enabled(params)
        keep_resets = bool(params.get("keep_resets", True))
        reset_threshold_pct = float(params.get("reset_threshold_pct", 50.0))
        retry_attempts = int(params.get("retry_attempts", 2))
        retry_delay_sec = float(params.get("retry_delay_sec", 1.0))
        # first/last/mode funzionano anche su sensori testuali; le altre no.
        numeric_agg = agg not in ("first", "last", "mode")
        results = []
        for eid in entity_ids:
            if cancel_event and cancel_event.is_set():
                logger.info(f"[PeakDecimation] Cancellazione richiesta, interrotto a {eid}")
                break
            try:
                def _op(_attempt: int):
                    if numeric_agg:
                        stats = db.get_sensor_stats(eid)
                        if stats and not stats.get("is_numeric", False):
                            return {
                                "entity_id": eid,
                                "skipped": True,
                                "reason": "Sensore non numerico - strategia inapplicabile",
                            }

                    r = db.peak_decimate_entity(
                        eid, older_than_days,
                        bucket_seconds=bucket_seconds,
                        agg=agg, agg_pct=agg_pct,
                        keep_resets=keep_resets,
                        reset_threshold_pct=reset_threshold_pct,
                        dry_run=dry_run,
                        batch_size=batch_size,
                        cleanup_attributes=cleanup_attributes,
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
            "total_attr_removed": sum(_attr_count(r) for r in results if not r.get("skipped")),
            "details": results,
        }


# ---------------------------------------------------------------------------
# Strategia 5 - Deduplica Sequenziale
# ---------------------------------------------------------------------------

class DeduplicateValues(Strategy):
    """
    Elimina sequenze di valori uguali mantenendo il record più vecchio.
    Se il valore resta identico su giorni diversi, conserva al massimo 1 record al giorno.
    Funziona sia per stati testuali sia per numeri salvati come stringhe.
    """
    name = "deduplicate_values"
    label = "Deduplica Valori"
    description = (
        "Rimuove duplicati consecutivi dello stesso valore, mantenendo il record più vecchio. "
        "Preserva comunque almeno un record per ogni intervallo di tempo configurato "
        "(default: uno al giorno), anche se il valore non cambia."
    )

    def execute(self, db, entity_ids, params, dry_run=False, batch_size=5000, cancel_event=None):
        older_than_days = int(params.get("older_than_days", 7))
        keep_interval_seconds = parse_interval_to_seconds(params.get("keep_interval"), 86400)
        cleanup_attributes = cleanup_attributes_enabled(params)
        retry_attempts = int(params.get("retry_attempts", 2))
        retry_delay_sec = float(params.get("retry_delay_sec", 1.0))
        results = []

        for eid in entity_ids:
            if cancel_event and cancel_event.is_set():
                logger.info(f"[DeduplicateValues] Cancellazione richiesta, interrotto a {eid}")
                break
            try:
                raw_result = _run_with_retry(
                    "DeduplicateValues",
                    eid,
                    lambda attempt: db.deduplicate_entity(
                        eid,
                        older_than_days,
                        dry_run=dry_run,
                        batch_size=batch_size,
                        keep_interval_seconds=keep_interval_seconds,
                        cleanup_attributes=cleanup_attributes,
                    ),
                    retry_attempts=retry_attempts,
                    retry_delay_sec=retry_delay_sec,
                )
                r = raw_result if isinstance(raw_result, dict) else {}
                r["entity_id"] = eid
                results.append(r)
                logger.info(
                    f"[DeduplicateValues] {eid}: "
                    f"{'(DRY) ' if dry_run else ''}"
                    f"~{r.get('deleted', r.get('estimated_deleted', 0))} record duplicati"
                )
            except Exception as e:
                logger.error(f"[DeduplicateValues] Errore su {eid} dopo {retry_attempts} tentativi: {e}")
                results.append({"entity_id": eid, "error": str(e)})

        total_deleted = sum(r.get("deleted", r.get("estimated_deleted", 0)) for r in results)
        return {
            "strategy": self.name,
            "dry_run": dry_run,
            "params": params,
            "entity_count": len(entity_ids),
            "total_deleted": total_deleted,
            "total_attr_removed": sum(_attr_count(r) for r in results),
            "details": results,
        }


# ---------------------------------------------------------------------------
# Registry e factory
# ---------------------------------------------------------------------------

STRATEGY_REGISTRY = {
    SimplePurge.name: SimplePurge,
    AdaptivePurge.name: AdaptivePurge,
    OutlierPurge.name: OutlierPurge,
    PeakDecimation.name: PeakDecimation,
    DeduplicateValues.name: DeduplicateValues,
}

# Toggle condiviso da tutte le strategie: pulizia mirata delle righe
# state_attributes rimaste orfane dopo le cancellazioni.
_CLEANUP_ATTRS_PARAM = {
    "key": "cleanup_attributes",
    "label": "Elimina anche le righe state_attributes rimaste orfane",
    "type": "boolean",
    "default": True,
}

STRATEGY_LIST = [
    {
        "name": SimplePurge.name,
        "label": SimplePurge.label,
        "description": SimplePurge.description,
        "example": "Se un sensore registra ogni minuto, elimina tutto cio' che e' piu' vecchio della soglia senza guardare il valore.",
        "overlap": "Coincide con la sola fase finale del 'Purge Adattivo' (eliminazione oltre soglia). Usa questa se non ti serve conservare nulla del passato remoto.",
        "params": [
            {"key": "older_than_days", "label": "Elimina record più vecchi di (giorni)",
             "type": "number", "default": 30, "min": 1},
            _CLEANUP_ATTRS_PARAM,
        ],
    },
    {
        "name": AdaptivePurge.name,
        "label": AdaptivePurge.label,
        "description": AdaptivePurge.description,
        "example": "4 fasce: 0–7gg tutto; 7–30gg 1 punto ogni 15 min (media pesata); 30–90gg 1 al giorno (mediana); >365gg eliminati.",
        "overlap": "Strategia più completa: in un unico passaggio combina N fasce di appiattimento con bucket e aggregazione a scelta più l'eliminazione finale. 'Purge Semplice' ne è il sottoinsieme con la sola eliminazione.",
        "params": [
            {"key": "tiers", "label": "Fasce (dopo N giorni → appiattisci con bucket+aggregazione, oppure elimina)",
             "type": "tiers", "default": [
                 {"after_days": 7, "action": "flatten", "bucket": {"every": 1, "unit": "hour"}, "agg": "time_weighted_mean"},
                 {"after_days": 30, "action": "flatten", "bucket": {"every": 1, "unit": "day"}, "agg": "time_weighted_mean"},
                 {"after_days": 365, "action": "delete"},
             ]},
            _CLEANUP_ATTRS_PARAM,
        ],
    },
    {
        "name": OutlierPurge.name,
        "label": OutlierPurge.label,
        "description": OutlierPurge.description,
        "example": "Se un sensore manda -999, unavailable o valori fuori range, li elimina senza toccare i record validi.",
        "overlap": "Nessuna sovrapposizione con le altre: agisce sulla qualità dei valori, non sulla risoluzione temporale. Complementare, si può usare prima di qualsiasi altra strategia.",
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
            _CLEANUP_ATTRS_PARAM,
        ],
    },
    {
        "name": PeakDecimation.name,
        "label": PeakDecimation.label,
        "description": PeakDecimation.description,
        "example": "Per un contatore energia conserva il valore massimo (o mediana/percentile) di ogni ora, giorno o intervallo scelto, e protegge i reset.",
        "overlap": "Come una fascia del 'Purge Adattivo' (1 record per bucket) ma con reset detection integrata: per i contatori cumulativi la media non ha senso, di default tiene il MASSIMO.",
        "params": [
            {"key": "older_than_days", "label": "Applica a dati più vecchi di (giorni)",
             "type": "number", "default": 7, "min": 1},
            {"key": "bucket", "label": "Dimensione bucket (ogni X min/ore/giorni/settimane)",
             "type": "interval", "default": {"every": 1, "unit": "hour"}},
            {"key": "agg", "label": "Aggregazione per bucket",
             "type": "select", "options": list(AGG_CHOICES), "default": "max"},
            {"key": "agg_pct", "label": "Percentile (se aggregazione = percentile)",
             "type": "number", "default": 95, "min": 0, "max": 100, "optional": True},
            {"key": "keep_resets", "label": "Preserva punti di reset automaticamente",
             "type": "boolean", "default": True},
            {"key": "reset_threshold_pct", "label": "Soglia reset (% calo per rilevare reset)",
             "type": "number", "default": 50.0, "min": 5, "max": 99},
            _CLEANUP_ATTRS_PARAM,
        ],
    },
    {
        "name": DeduplicateValues.name,
        "label": DeduplicateValues.label,
        "description": DeduplicateValues.description,
        "example": "Se un sensore continua a pubblicare 21.3 con timestamp diversi, lascia il primo record della sequenza e, se continua a lungo, almeno uno ogni intervallo scelto (es. 1 all'ora).",
        "overlap": "Nessuna sovrapposizione: non riduce la risoluzione né altera i valori, rimuove solo ripetizioni identiche consecutive. È quasi senza perdita e si combina bene con il 'Purge Adattivo'.",
        "params": [
            {"key": "older_than_days", "label": "Applica a dati più vecchi di (giorni)",
             "type": "number", "default": 7, "min": 1},
            {"key": "keep_interval", "label": "Preserva almeno 1 valore ogni (0 = deduplica pura)",
             "type": "interval", "default": {"every": 1, "unit": "day"}},
            _CLEANUP_ATTRS_PARAM,
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
    resolved_entity_ids = entity_ids
    unmatched_patterns: list[str] = []

    resolver = getattr(db, "resolve_entity_ids", None)
    if callable(resolver):
        try:
            resolved_result = resolver(entity_ids)
            if isinstance(resolved_result, tuple) and len(resolved_result) == 2:
                resolved_entity_ids, unmatched_patterns = resolved_result
            else:
                resolved_entity_ids = entity_ids
                unmatched_patterns = []
        except Exception as e:
            logger.warning(f"[StrategyStart] fallback senza espansione wildcard: {e}")
            resolved_entity_ids = entity_ids
            unmatched_patterns = []

    logger.info(
        f"[StrategyStart] name={strategy_name} requested={len(entity_ids)} "
        f"resolved={len(resolved_entity_ids)} dry_run={dry_run}"
    )
    if unmatched_patterns:
        logger.warning(
            f"[StrategyStart] pattern wildcard senza risultati per {strategy_name}: {unmatched_patterns}"
        )

    if not resolved_entity_ids:
        return {
            "error": "Nessuna entita' corrisponde ai filtri specificati",
            "input_entity_ids": entity_ids,
            "resolved_entity_ids": [],
            "unmatched_entity_patterns": unmatched_patterns,
        }

    t0 = time.time()
    try:
        res = strategy.execute(
            db, resolved_entity_ids, params, dry_run=dry_run,
            batch_size=batch_size, cancel_event=cancel_event,
        )
        elapsed = time.time() - t0
        # try to extract basic summary info
        total_deleted = res.get("total_deleted") if isinstance(res, dict) else None
        logger.info(f"[StrategyEnd] name={strategy_name} elapsed_s={elapsed:.2f} total_deleted={total_deleted}")
        if isinstance(res, dict):
            res.setdefault("input_entity_ids", entity_ids)
            res["resolved_entity_ids"] = resolved_entity_ids
            if unmatched_patterns:
                res["unmatched_entity_patterns"] = unmatched_patterns
            res["entity_count"] = len(resolved_entity_ids)
        return res
    except Exception as e:
        elapsed = time.time() - t0
        logger.error(f"[StrategyError] name={strategy_name} elapsed_s={elapsed:.2f} error={e}")
        raise
