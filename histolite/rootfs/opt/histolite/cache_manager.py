"""
HistoLite - Gestione cache risultati query pesanti
"""

import json
import time
import logging
from typing import Optional, Any

logger = logging.getLogger(__name__)


class CacheManager:
    """Gestisce cache di risultati query con TTL (time-to-live)."""

    def __init__(self, cache_dir: str = "/data/histolite"):
        self.cache_dir = cache_dir
        self.cache = {}  # {key: {"value": data, "timestamp": ts, "ttl_seconds": ttl}}

    def get(self, key: str) -> Optional[Any]:
        """Ritorna valore dalla cache se valido, None altrimenti."""
        if key not in self.cache:
            return None

        entry = self.cache[key]
        age = time.time() - entry["timestamp"]
        
        if age > entry["ttl_seconds"]:
            logger.debug(f"Cache {key} scaduto (age={age:.0f}s, TTL={entry['ttl_seconds']}s)")
            del self.cache[key]
            return None

        logger.debug(f"Cache {key} valido (age={age:.0f}s)")
        return entry["value"]

    def set(self, key: str, value: Any, ttl_seconds: int = 300):
        """Salva valore in cache con TTL."""
        self.cache[key] = {
            "value": value,
            "timestamp": time.time(),
            "ttl_seconds": ttl_seconds,
        }
        logger.debug(f"Cache {key} impostato con TTL {ttl_seconds}s")

    def get_age(self, key: str) -> Optional[float]:
        """Ritorna l'età della cache in secondi, o None se non presente."""
        if key not in self.cache:
            return None
        age = time.time() - self.cache[key]["timestamp"]
        return age if age <= self.cache[key]["ttl_seconds"] else None

    def invalidate(self, key: str):
        """Invalida immediatamente una chiave di cache."""
        if key in self.cache:
            del self.cache[key]
            logger.info(f"Cache {key} invalidata")

    def get_with_metadata(self, key: str) -> Optional[dict]:
        """Ritorna {value, timestamp_updated, age_seconds} o None."""
        if key not in self.cache:
            return None

        entry = self.cache[key]
        age = time.time() - entry["timestamp"]
        
        if age > entry["ttl_seconds"]:
            del self.cache[key]
            return None

        return {
            "value": entry["value"],
            "timestamp_updated": entry["timestamp"],
            "age_seconds": age,
            "ttl_seconds": entry["ttl_seconds"],
        }
