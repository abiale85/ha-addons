"""
HistoLite - Gestione configurazione strategie e log operazioni
"""

import json
import os
import uuid
import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


class ConfigManager:
    """Gestisce strategie salvate e log delle operazioni eseguite."""

    def __init__(self, data_path: str):
        self.data_dir = os.path.join(data_path, "histolite")
        os.makedirs(self.data_dir, exist_ok=True)
        self.strategies_file = os.path.join(self.data_dir, "strategies.json")
        self.jobs_file = os.path.join(self.data_dir, "jobs.json")
        self._init_files()

    def _init_files(self):
        for f in (self.strategies_file, self.jobs_file):
            if not os.path.exists(f):
                with open(f, "w", encoding="utf-8") as fh:
                    json.dump([], fh)

    def _load(self, path: str) -> list:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            return []

    def _save(self, path: str, data: list):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    # ------------------------------------------------------------------
    # Strategie salvate
    # ------------------------------------------------------------------

    def list_strategies(self) -> list:
        return self._load(self.strategies_file)

    def get_strategy(self, strategy_id: str) -> Optional[dict]:
        return next(
            (s for s in self.list_strategies() if s["id"] == strategy_id), None
        )

    def save_strategy(self, strategy_config: dict) -> dict:
        """Crea o aggiorna una strategia salvata."""
        strategies = self.list_strategies()
        is_update = "id" in strategy_config and bool(strategy_config["id"])
        if is_update:
            for i, s in enumerate(strategies):
                if s["id"] == strategy_config["id"]:
                    strategies[i] = {**s, **strategy_config, "updated_at": _now()}
                    self._save(self.strategies_file, strategies)
                    return strategies[i]
        new_entry = {
            **strategy_config,
            "id": str(uuid.uuid4()),
            "created_at": _now(),
            "updated_at": _now(),
        }
        strategies.append(new_entry)
        self._save(self.strategies_file, strategies)
        return new_entry

    def delete_strategy(self, strategy_id: str) -> bool:
        strategies = self.list_strategies()
        new_list = [s for s in strategies if s["id"] != strategy_id]
        if len(new_list) == len(strategies):
            return False
        self._save(self.strategies_file, new_list)
        return True

    # ------------------------------------------------------------------
    # Log operazioni (job history)
    # ------------------------------------------------------------------

    def list_jobs(self, limit: int = 50) -> list:
        jobs = self._load(self.jobs_file)
        return sorted(jobs, key=lambda j: j.get("executed_at", ""), reverse=True)[:limit]

    def save_job(self, result: dict, strategy_name: str, entity_ids: list,
                 params: dict, dry_run: bool) -> dict:
        """Salva il risultato di un'operazione eseguita."""
        jobs = self._load(self.jobs_file)
        entry = {
            "id": str(uuid.uuid4()),
            "executed_at": _now(),
            "strategy": strategy_name,
            "entity_ids": entity_ids,
            "params": params,
            "dry_run": dry_run,
            "result": {
                "total_deleted": result.get("total_deleted", 0),
                "entity_count": result.get("entity_count", 0),
                "backup": result.get("backup"),
                "error": result.get("error"),
            },
        }
        jobs.append(entry)
        # Mantieni solo gli ultimi 200 job
        if len(jobs) > 200:
            jobs = sorted(jobs, key=lambda j: j.get("executed_at", ""), reverse=True)[:200]
        self._save(self.jobs_file, jobs)
        return entry

    def clear_jobs(self):
        self._save(self.jobs_file, [])


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")
