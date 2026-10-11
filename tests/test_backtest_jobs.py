"""Tests for durable and owner-scoped asynchronous backtest jobs."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from kuru_backtester.server.backtest_jobs import BacktestJobManager, JobError
from kuru_backtester.server import backtest_jobs
from kuru_backtester.server.backtest_service import canonical_bytes


def stored_status(run_id: str, token: str, status: str = "running") -> dict:
    old = datetime.now(timezone.utc) - timedelta(minutes=5)
    return {
        "schema_version": "1.0",
        "run_id": run_id,
        "owner_token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        "status": status,
        "stage": "simulation",
        "last_progress_at": old.isoformat().replace("+00:00", "Z"),
    }


def write_job(manager: BacktestJobManager, run_id: str, status: dict) -> None:
    directory = manager.root / run_id
    directory.mkdir()
    (directory / "status.json").write_bytes(canonical_bytes(status) + b"\n")


def test_owner_token_is_required_and_stale_running_job_is_marked_stalled(tmp_path) -> None:
    manager = BacktestJobManager(tmp_path)
    run_id = "job-test"
    token = "secret"
    write_job(manager, run_id, stored_status(run_id, token))
    with pytest.raises(JobError, match="owner token"):
        manager.status(run_id, "wrong")
    result = manager.status(run_id, token)
    assert result["status"] == "stalled"
    assert "owner_token_sha256" not in result
    persisted = json.loads((tmp_path / run_id / "status.json").read_text())
    assert persisted["status"] == "stalled"


def test_completed_result_requires_matching_complete_marker_hash(tmp_path) -> None:
    manager = BacktestJobManager(tmp_path)
    run_id = "job-complete"
    token = "secret"
    write_job(manager, run_id, stored_status(run_id, token, "completed"))
    directory = tmp_path / run_id
    payload = canonical_bytes({"run_id": "lp-result", "fills": 3}) + b"\n"
    (directory / "result.json").write_bytes(payload)
    (directory / "complete.json").write_text(json.dumps({
        "result_file_sha256": hashlib.sha256(payload).hexdigest(),
    }))
    assert manager.result(run_id, token)["fills"] == 3
    (directory / "result.json").write_text("{}")
    with pytest.raises(JobError, match="hash does not match"):
        manager.result(run_id, token)


def test_detailed_fills_are_separate_and_hash_verified(tmp_path) -> None:
    manager = BacktestJobManager(tmp_path)
    run_id = "job-fills"
    token = "secret"
    write_job(manager, run_id, stored_status(run_id, token, "completed"))
    directory = tmp_path / run_id
    fills = [{"strategy_order_id": "lp-1", "size": 4}]
    payload = canonical_bytes(fills) + b"\n"
    (directory / "fills.json").write_bytes(payload)
    (directory / "complete.json").write_bytes(canonical_bytes({
        "fills_file_sha256": hashlib.sha256(payload).hexdigest(),
    }) + b"\n")
    assert manager.fills(run_id, token) == fills
    (directory / "fills.json").write_text("[]")
    with pytest.raises(JobError, match="fill hash does not match"):
        manager.fills(run_id, token)


def test_completion_can_remove_heartbeat_during_a_status_read(tmp_path, monkeypatch):
    manager = BacktestJobManager(tmp_path)
    run_id, token = "job-finish", "secret"
    write_job(manager, run_id, stored_status(run_id, token))
    heartbeat = tmp_path / "live.json"
    heartbeat.write_text("{}")
    monkeypatch.setattr(backtest_jobs, "live_status_path", lambda key: heartbeat)
    original_read = Path.read_text

    def finish_before_read(path, *args, **kwargs):
        if path == heartbeat:
            terminal = stored_status(run_id, token, "completed")
            (tmp_path / run_id / "status.json").write_text(json.dumps(terminal))
            heartbeat.unlink()
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", finish_before_read)
    assert manager.status(run_id, token)["status"] == "completed"


def test_worker_exit_cannot_overwrite_completed_status(tmp_path, monkeypatch):
    manager = BacktestJobManager(tmp_path)
    run_id, token = "job-exit", "secret"
    write_job(manager, run_id, stored_status(run_id, token))
    monkeypatch.setattr(backtest_jobs, "live_status_path", lambda key: tmp_path / "absent")

    def finish_before_poll():
        terminal = stored_status(run_id, token, "completed")
        (tmp_path / run_id / "status.json").write_text(json.dumps(terminal))
        return 0

    manager._processes[run_id] = SimpleNamespace(poll=finish_before_poll, returncode=0)
    assert manager.status(run_id, token)["status"] == "completed"
    assert json.loads((tmp_path / run_id / "status.json").read_text())["status"] == "completed"


def test_exit_without_terminal_status_is_still_a_failure(tmp_path, monkeypatch):
    manager = BacktestJobManager(tmp_path)
    run_id, token = "job-crash", "secret"
    write_job(manager, run_id, stored_status(run_id, token))
    monkeypatch.setattr(backtest_jobs, "live_status_path", lambda key: tmp_path / "absent")
    manager._processes[run_id] = SimpleNamespace(poll=lambda: 1, returncode=1)
    result = manager.status(run_id, token)
    assert result["status"] == "failed"
    assert "exited with code 1" in result["failure_reason"]
