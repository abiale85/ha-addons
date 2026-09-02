"""
HistoLite - Gestione configurazione strategie e log operazioni
"""

import json
import os
import shutil
import uuid
import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


class ConfigManager:
    """Gestisce strategie salvate e log delle operazioni eseguite."""

    def __init__(self, data_path: str):
        self.data_dir = os.path.join(data_path, "histolite")
        # Percorso storico: la cartella condivisa di Home Assistant. La
        # persistenza è stata spostata nel volume privato /data così che
        # "disinstalla e rimuovi dati" rimuova davvero tutto e "mantieni dati"
        # conservi tutto. I file eventualmente rimasti in /config vengono
        # spostati (non copiati) alla prima esecuzione.
        self.legacy_data_dir = "/config/histolite"
        os.makedirs(self.data_dir, exist_ok=True)
        self.strategies_file = os.path.join(self.data_dir, "strategies.json")
        self.jobs_file = os.path.join(self.data_dir, "jobs.json")
        self._migrate_legacy_files()
        self._init_files()

    def _migrate_legacy_files(self):
        """Sposta strategie e cronologia dal vecchio path /config/histolite al nuovo /data/histolite."""
        if os.path.abspath(self.data_dir) == os.path.abspath(self.legacy_data_dir):
            return
        for name in ("strategies.json", "jobs.json"):
            src = os.path.join(self.legacy_data_dir, name)
            dst = os.path.join(self.data_dir, name)
            if os.path.exists(dst) or not os.path.exists(src):
                continue
            try:
                shutil.copy2(src, dst)
                os.remove(src)
                logger.info(f"Spostato {name} da {src} a {dst}")
            except OSError as e:
                logger.warning(f"Impossibile spostare {src} -> {dst}: {e}")
        try:
            os.rmdir(self.legacy_data_dir)
        except OSError:
            pass  # cartella non vuota o inesistente: normale

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

    def update_strategy_last_run(self, strategy_id: str, timestamp: str):
        """Aggiorna il timestamp dell'ultima esecuzione."""
        strategies = self.list_strategies()
        for i, s in enumerate(strategies):
            if s["id"] == strategy_id:
                strategies[i]["last_run_at"] = timestamp
                self._save(self.strategies_file, strategies)
                return

    # ------------------------------------------------------------------
    # Log operazioni (job history)
    # ------------------------------------------------------------------

    def list_jobs(self, limit: int = 50) -> list:
        jobs = self._load(self.jobs_file)
        return sorted(jobs, key=lambda j: j.get("executed_at", ""), reverse=True)[:limit]

    def latest_job(self) -> Optional[dict]:
        """Il job più recente per ``executed_at`` (o None)."""
        jobs = self._load(self.jobs_file)
        if not jobs:
            return None
        return max(jobs, key=lambda j: j.get("executed_at", ""))

    def _trim_and_save_jobs(self, jobs: list) -> None:
        if len(jobs) > 200:
            jobs = sorted(jobs, key=lambda j: j.get("executed_at", ""), reverse=True)[:200]
        self._save(self.jobs_file, jobs)

    @staticmethod
    def _result_summary(result: dict) -> dict:
        return {
            "total_deleted": result.get("total_deleted", 0),
            "total_attr_removed": result.get("total_attr_removed", 0),
            "entity_count": result.get("entity_count", 0),
            "backup": result.get("backup"),
            "error": result.get("error"),
        }

    def save_job(self, result: dict, strategy_name: str, entity_ids: list,
                 params: dict, dry_run: bool) -> dict:
        """Salva il risultato di un'operazione sincrona già conclusa
        (manutenzione: vacuum, cleanup, statistics_repair, entity_merge…)."""
        jobs = self._load(self.jobs_file)
        entry = {
            "id": str(uuid.uuid4()),
            "executed_at": _now(),
            "strategy": strategy_name,
            "entity_ids": entity_ids,
            "params": params,
            "dry_run": dry_run,
            "status": "error" if result.get("error") else "done",
            "result": self._result_summary(result),
        }
        jobs.append(entry)
        self._trim_and_save_jobs(jobs)
        return entry

    def create_running_job(self, strategy_name: str, entity_ids: list,
                           params: dict) -> str:
        """Registra un job 'in corso' e ne restituisce l'id.

        Prima marca come 'interrupted' ogni job ancora 'running' (residuo di un
        processo terminato senza aggiornare l'esito).
        """
        jobs = self._load(self.jobs_file)
        for j in jobs:
            if j.get("status") == "running":
                j["status"] = "interrupted"
                j.setdefault("result", {}).setdefault(
                    "error", "Interrotta: processo terminato prima del completamento")
                j["finished_at"] = _now()
        job_id = str(uuid.uuid4())
        now_ts = datetime.now().timestamp()
        jobs.append({
            "id": job_id,
            "executed_at": _now(),
            "strategy": strategy_name,
            "entity_ids": entity_ids,
            "params": params,
            "dry_run": False,
            "status": "running",
            "started_ts": now_ts,
            "updated_ts": now_ts,
            "result": {},
        })
        self._trim_and_save_jobs(jobs)
        return job_id

    def finish_job(self, job_id: str, result: dict, status: str = "done") -> None:
        """Aggiorna il job 'running' con l'esito finale."""
        jobs = self._load(self.jobs_file)
        for j in jobs:
            if j.get("id") != job_id:
                continue
            j["status"] = "error" if (status == "error" or result.get("error")) else status
            j["finished_at"] = _now()
            if result.get("duration_sec") is not None:
                j["duration_sec"] = result["duration_sec"]
            j["result"] = self._result_summary(result)
            break
        self._trim_and_save_jobs(jobs)

    def heartbeat_job(self, job_id: str) -> None:
        """Aggiorna updated_ts di un job 'running' (segno di vita)."""
        jobs = self._load(self.jobs_file)
        for j in jobs:
            if j.get("id") == job_id and j.get("status") == "running":
                j["updated_ts"] = datetime.now().timestamp()
                self._save(self.jobs_file, jobs)
                return

    def clear_jobs(self):
        self._save(self.jobs_file, [])


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")
