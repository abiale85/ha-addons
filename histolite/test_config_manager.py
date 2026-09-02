"""Test per il ciclo di vita dei job (running → done/error/interrupted)."""

import os
import sys
import json
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "rootfs", "opt", "histolite"))

from config_manager import ConfigManager


def _cm():
    d = tempfile.mkdtemp()
    return ConfigManager(d), d


def test_create_and_finish_done():
    cm, _ = _cm()
    jid = cm.create_running_job("simple_purge", ["sensor.a"], {"older_than_days": 30})
    latest = cm.latest_job()
    assert latest["id"] == jid
    assert latest["status"] == "running"
    assert latest["started_ts"] > 0

    cm.finish_job(jid, {"total_deleted": 123, "entity_count": 1, "duration_sec": 4.2}, "done")
    latest = cm.latest_job()
    assert latest["status"] == "done"
    assert latest["result"]["total_deleted"] == 123
    assert latest["duration_sec"] == 4.2
    assert "finished_at" in latest


def test_finish_error():
    cm, _ = _cm()
    jid = cm.create_running_job("adaptive_purge", ["sensor.b"], {})
    cm.finish_job(jid, {"error": "boom", "entity_count": 1}, "error")
    latest = cm.latest_job()
    assert latest["status"] == "error"
    assert latest["result"]["error"] == "boom"


def test_new_run_marks_previous_running_as_interrupted():
    cm, _ = _cm()
    jid1 = cm.create_running_job("simple_purge", ["sensor.a"], {})
    jid2 = cm.create_running_job("simple_purge", ["sensor.a"], {})
    jobs = {j["id"]: j for j in cm.list_jobs(50)}
    assert jobs[jid1]["status"] == "interrupted"
    assert jobs[jid1]["result"].get("error")
    assert jobs[jid2]["status"] == "running"


def test_legacy_job_without_status():
    cm, d = _cm()
    # scrive direttamente un job "vecchio stile" senza campo status
    legacy = [{
        "id": "old-1", "executed_at": "2020-01-01T00:00:00", "strategy": "simple_purge",
        "entity_ids": [], "params": {}, "dry_run": False,
        "result": {"total_deleted": 5, "entity_count": 0, "error": None},
    }]
    with open(cm.jobs_file, "w", encoding="utf-8") as fh:
        json.dump(legacy, fh)
    latest = cm.latest_job()
    assert latest["id"] == "old-1"
    assert latest.get("status") is None  # i lettori lo trattano come 'done'
    # create_running_job non deve toccarlo (non è 'running')
    cm.create_running_job("simple_purge", [], {})
    jobs = {j["id"]: j for j in cm.list_jobs(50)}
    assert jobs["old-1"].get("status") is None


def test_save_job_sets_status():
    cm, _ = _cm()
    cm.save_job({"total_deleted": 0}, "vacuum", [], {}, dry_run=False)
    cm.save_job({"error": "nope"}, "statistics_repair", [], {}, dry_run=False)
    jobs = cm.list_jobs(50)
    by_strat = {j["strategy"]: j for j in jobs}
    assert by_strat["vacuum"]["status"] == "done"
    assert by_strat["statistics_repair"]["status"] == "error"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("config_manager tests passed")
