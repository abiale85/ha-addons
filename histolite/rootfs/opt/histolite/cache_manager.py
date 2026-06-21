"""
HistoLite - Gestione cache risultati query pesanti
"""

import gc
import json
import logging
import os
import shutil
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Limite massimo di entry in cache (evita crescita illimitata)
_MAX_CACHE_ENTRIES = 10


class CacheManager:
    """Gestisce cache di risultati query con TTL e persistenza su disco."""

    def __init__(self, cache_dir: str = "/data"):
        self.data_dir = os.path.join(cache_dir, "histolite")
        self.legacy_data_dir = "/data/histolite"
        os.makedirs(self.data_dir, exist_ok=True)
        self.cache_file = os.path.join(self.data_dir, "cache.json")
        self.cache: dict[str, dict[str, Any]] = {}
        self._migrate_legacy_cache_file()
        self._load_from_disk()

    def _migrate_legacy_cache_file(self):
        """Migra il file cache dal vecchio path /data/histolite al nuovo /config/histolite."""
        if os.path.abspath(self.data_dir) == os.path.abspath(self.legacy_data_dir):
            return
        legacy_cache_file = os.path.join(self.legacy_data_dir, "cache.json")
        if os.path.exists(self.cache_file) or not os.path.exists(legacy_cache_file):
            return
        try:
            shutil.copy2(legacy_cache_file, self.cache_file)
            logger.info(f"Migrato cache overview da {legacy_cache_file} a {self.cache_file}")
        except OSError as e:
            logger.warning(f"Impossibile migrare cache da {legacy_cache_file}: {e}")

    def _load_from_disk(self):
        """Carica la cache persistita da disco e scarta le entry scadute/corrotte."""
        if not os.path.exists(self.cache_file):
            return

        try:
            with open(self.cache_file, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            if not isinstance(raw, dict):
                logger.warning("Cache file non valido, reset")
                return

            now = time.time()
            loaded = {}
            for key, entry in raw.items():
                if not isinstance(entry, dict):
                    continue
                timestamp = entry.get("timestamp")
                ttl_seconds = entry.get("ttl_seconds")
                value = entry.get("value")
                if not isinstance(timestamp, (int, float)):
                    continue
                if ttl_seconds is not None and not isinstance(ttl_seconds, (int, float)):
                    continue
                if ttl_seconds is not None and now - timestamp > ttl_seconds:
                    continue
                loaded[key] = {
                    "value": value,
                    "timestamp": float(timestamp),
                    "ttl_seconds": None if ttl_seconds is None else int(ttl_seconds),
                }

            self.cache = loaded
            if loaded:
                logger.info(f"Cache ricaricata da disco: {len(loaded)} chiavi valide")
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"Impossibile caricare cache persistita: {e}")
            self.cache = {}

    def _save_to_disk(self):
        """Persisti su disco la cache corrente in formato JSON."""
        tmp_file = self.cache_file + ".tmp"
        try:
            with open(tmp_file, "w", encoding="utf-8") as fh:
                json.dump(self.cache, fh, ensure_ascii=False)
            os.replace(tmp_file, self.cache_file)
        except (OSError, TypeError) as e:
            logger.warning(f"Impossibile salvare cache su disco: {e}")
            try:
                if os.path.exists(tmp_file):
                    os.remove(tmp_file)
            except OSError:
                pass

    def _evict_expired(self):
        """Rimuove tutte le voci scadute."""
        now = time.time()
        expired = [
            key for key, value in self.cache.items()
            if value["ttl_seconds"] is not None and now - value["timestamp"] > value["ttl_seconds"]
        ]
        for key in expired:
            del self.cache[key]

    def get(self, key: str) -> Optional[Any]:
        """Ritorna valore dalla cache se valido, None altrimenti."""
        if key not in self.cache:
            return None

        entry = self.cache[key]
        age = time.time() - entry["timestamp"]
        if entry["ttl_seconds"] is not None and age > entry["ttl_seconds"]:
            logger.debug(f"Cache {key} scaduto (age={age:.0f}s, TTL={entry['ttl_seconds']}s)")
            del self.cache[key]
            self._save_to_disk()
            return None

        logger.debug(f"Cache {key} valido (age={age:.0f}s)")
        return entry["value"]

    def set(self, key: str, value: Any, ttl_seconds: Optional[int] = 300):
        """Salva valore in cache con TTL e persistenza su disco."""
        self._evict_expired()
        if len(self.cache) >= _MAX_CACHE_ENTRIES and key not in self.cache:
            oldest = min(self.cache, key=lambda cache_key: self.cache[cache_key]["timestamp"])
            del self.cache[oldest]
            logger.debug(f"Cache evicted: {oldest}")

        self.cache[key] = {
            "value": value,
            "timestamp": time.time(),
            "ttl_seconds": ttl_seconds,
        }
        logger.debug(
            f"Cache {key} impostato con TTL {'infinito' if ttl_seconds is None else str(ttl_seconds) + 's'}"
        )
        self._save_to_disk()
        gc.collect()

    def get_age(self, key: str) -> Optional[float]:
        """Ritorna l'età della cache in secondi, o None se non presente."""
        if key not in self.cache:
            return None
        age = time.time() - self.cache[key]["timestamp"]
        ttl_seconds = self.cache[key]["ttl_seconds"]
        return age if ttl_seconds is None or age <= ttl_seconds else None

    def invalidate(self, key: str):
        """Invalida immediatamente una chiave di cache."""
        if key in self.cache:
            del self.cache[key]
            self._save_to_disk()
            gc.collect()
            logger.info(f"Cache {key} invalidata")

    def get_with_metadata(self, key: str) -> Optional[dict]:
        """Ritorna {value, timestamp_updated, age_seconds} o None."""
        value = self.get(key)
        if value is None:
            return None

        entry = self.cache[key]
        age = time.time() - entry["timestamp"]
        return {
            "value": value,
            "timestamp_updated": entry["timestamp"],
            "age_seconds": age,
            "ttl_seconds": entry["ttl_seconds"],
        }
