"""Regression checks for API limits and historical data validation."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from kuru_backtester.server import backtest_jobs
from kuru_backtester.server.backtest_jobs import BacktestJobManager, JobError
from kuru_backtester.server.backtest_server import BacktestHandler
from kuru_backtester.history.historical_system import HistoricalRangeError, ValidationLayer
from kuru_backtester.history import promote_historical_partition as promotion
from kuru_backtester.engine import strategy_vertical_slice as strategy


def test_worker_limit_rejects_before_launch(tmp_path):
    manager = BacktestJobManager(tmp_path, maximum_workers=1)
    manager._processes["job-active"] = SimpleNamespace(poll=lambda: None)
    manager._create = Mock()
    with pytest.raises(JobError, match="maximum active"):
        manager.create({})
    manager._create.assert_not_called()


def test_shutdown_stops_owned_workers(tmp_path, monkeypatch):
    manager = BacktestJobManager(tmp_path)
    job = tmp_path / "job-active"
    job.mkdir()
    (job / "status.json").write_text(json.dumps({"status": "queued"}))
    process = Mock()
    process.poll.return_value = None
    manager._processes["job-active"] = process
    monkeypatch.setattr(backtest_jobs, "live_status_path", lambda key: tmp_path / "heartbeat")
    manager.close()
    process.terminate.assert_called_once()
    process.wait.assert_called_once_with(timeout=5)
    assert json.loads((job / "status.json").read_text())["status"] == "canceled"
    with pytest.raises(JobError, match="shutting down"):
        manager.create({})


def test_network_access_requires_matching_cookie():
    handler = object.__new__(BacktestHandler)
    handler.access_token = "expected"
    handler.headers = {"Cookie": "kuru_access=wrong"}
    assert not handler.authorized()
    handler.headers = {"Cookie": "kuru_access=expected"}
    assert handler.authorized()


def test_local_api_rejects_foreign_browser_origin():
    handler = object.__new__(BacktestHandler)
    handler.access_token = None
    handler.headers = {"Host": "127.0.0.1:8000", "Origin": "https://foreign.example"}
    assert not handler.authorized()
    handler.headers["Origin"] = "http://127.0.0.1:8000"
    assert handler.authorized()


@pytest.mark.parametrize("field", ["midpoint", "cancellation"])
def test_validation_rejects_failed_extra_gate(tmp_path, field):
    root = Path(__file__).resolve().parents[1]
    payload = json.loads((root / "validation_output/relatively_active/summary.json").read_text())
    if field == "midpoint":
        payload["validation"]["maximum_midpoint_error"] = 1
    else:
        payload["cancellation_summary_audit"]["status"] = "fail"
    path = tmp_path / "summary.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(RuntimeError):
        strategy.verify_validation_summary(path)


def test_modified_file_is_rechecked(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_bytes(b"first")
    validation = ValidationLayer()
    expected = hashlib.sha256(path.read_bytes()).hexdigest()
    validation.verify_file(path, expected)
    path.write_bytes(b"changed")
    with pytest.raises(HistoricalRangeError, match="hash does not match"):
        validation.verify_file(path, expected)


def test_promotion_rejects_top_level_foreign_market(tmp_path, monkeypatch):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"market": "0xabc"}))
    monkeypatch.setattr(promotion.strategy, "verify_validation_summary", lambda path: {
        "market": "0xforeign", "interval": {"from_block": 10, "to_block": 20},
    })
    with pytest.raises(ValueError, match="validation market"):
        promotion.promote(manifest, tmp_path / "events", tmp_path / "summary", tmp_path / "checkpoint")
