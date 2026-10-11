"""Regression tests for maintenance changes outside trading semantics."""

import hashlib
import json
from types import SimpleNamespace
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest
from eth_abi import encode

from kuru_backtester.server import backtest_jobs
from kuru_backtester.server.backtest_jobs import BacktestJobManager, JobError
from kuru_backtester.server.backtest_service import atomic_write_bytes, decimal_text, raw_price_text
from kuru_backtester.tools.generate_trade_history_chart import build_chart, source_sha256
from kuru_backtester.history.historical_system import CheckpointStore, HistoricalRangeError
from kuru_backtester.history.index_history import EVENT_TOPICS, StagingPartition, run_index
from kuru_backtester.history.discover_envio_intervals import DiscoveryError
from kuru_backtester.engine.rebuild_from_chain import DEPLOYMENT_BLOCK, market_parameters


@pytest.mark.parametrize("raw,precision,expected", [(10**9,10**8,"10"), (10**10,10**8,"100"), (2711150,10**8,"0.0271115"), (0,10**8,"0")])
def test_whole_price_zeros_are_not_removed(raw, precision, expected):
    assert raw_price_text(raw, precision) == expected


def test_zero_decimal_integer_keeps_zeros():
    assert decimal_text("100", 0, "amount") == "100"


def manager_with_broker(tmp_path, monkeypatch):
    manager = BacktestJobManager(tmp_path)
    monkeypatch.setattr(manager, "_ensure_sdk_broker", lambda: SimpleNamespace(url="local", token="test"))
    monkeypatch.setattr(backtest_jobs, "live_status_path", lambda key: tmp_path / f"{key}.live")
    return manager


def test_fast_worker_terminal_status_is_not_overwritten(tmp_path, monkeypatch):
    manager = manager_with_broker(tmp_path, monkeypatch)
    handles = []
    def launch(command, **kwargs):
        handles.extend((kwargs["stdout"], kwargs["stderr"]))
        directory = Path(command[-1])
        status_path = directory / "status.json"
        status = json.loads(status_path.read_text())
        status.update(status="completed", stage="completed", worker_pid=42)
        status_path.write_text(json.dumps(status))
        return SimpleNamespace(pid=42, poll=lambda: 0)
    monkeypatch.setattr(backtest_jobs.subprocess, "Popen", launch)
    created = manager.create({"engine": "kuru_lp"})
    assert manager.status(created["run_id"], created["owner_token"])["status"] == "completed"
    assert all(handle.closed for handle in handles)


def test_failed_launch_closes_handles_and_records_failure(tmp_path, monkeypatch):
    manager = manager_with_broker(tmp_path, monkeypatch)
    handles = []
    def launch(*args, **kwargs):
        handles.extend((kwargs["stdout"], kwargs["stderr"]))
        raise OSError("test launch failure")
    monkeypatch.setattr(backtest_jobs.subprocess, "Popen", launch)
    with pytest.raises(JobError, match="could not start"):
        manager.create({})
    status_path = next(tmp_path.glob("job-*/status.json"))
    assert json.loads(status_path.read_text())["status"] == "failed"
    assert all(handle.closed for handle in handles)


@pytest.mark.parametrize("response", [
    {"data": []},
    {"next_block": 10, "data": []},
    {"next_block": 12, "data": [{"blocks": [{"number":12,"timestamp":100}], "logs": [{
        "address":"0xabc", "block_number":12, "transaction_hash":"0x1", "transaction_index":0,
        "log_index":0, "topic0":EVENT_TOPICS[0], "data":"0x",
    }]}]},
])
def test_invalid_page_does_not_advance_or_append(tmp_path, response):
    partition = StagingPartition(tmp_path, {"from_block":10,"to_block":20})
    client = SimpleNamespace(query=lambda *args, **kwargs: response)
    with pytest.raises(DiscoveryError):
        run_index(client, partition, "0xabc", 21)
    assert partition.state["next_block"] == 10
    assert not partition.events_path.exists()
    assert json.loads(partition.state_path.read_text())["next_block"] == 10


def test_checkpoint_path_cannot_leave_project_root(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"market":"0xabc", "checkpoints":[{
        "block_number":10, "block_timestamp":100, "path":"../outside.json", "sha256":"test",
    }]}))
    with pytest.raises(HistoricalRangeError, match="leaves the project root"):
        CheckpointStore(manifest)


def test_parameters_use_requested_block_and_keep_legacy_default():
    calls = []
    types = ["uint32","uint96","address","uint256","address","uint256","uint32","uint96","uint96","uint256","uint256"]
    data = "0x" + encode(types, [100,100,"0x"+"01"*20,18,"0x"+"02"*20,6,1,100,200,0,0]).hex()
    rpc = SimpleNamespace(contract_call=lambda *args: calls.append(args) or data)
    assert market_parameters(rpc,"0xabc",123)["base_decimals"] == 18
    market_parameters(rpc,"0xabc")
    assert [item[2] for item in calls] == [123,DEPLOYMENT_BLOCK]


def test_streaming_hash_equals_content_hash(tmp_path):
    path = tmp_path / "large.bin"
    payload = b"abc" * 1_000_000
    path.write_bytes(payload)
    assert source_sha256(path) == hashlib.sha256(payload).hexdigest()


@pytest.mark.parametrize("change", [{"block_number":21}, {"address":"0xforeign"}, {"trade_price":"NaN"}])
def test_chart_rejects_invalid_trade_scope(tmp_path, change):
    trade = {"name":"Trade","block_number":10,"block_timestamp":100,"trade_price":1, **change}
    (tmp_path / "events.jsonl").write_text(json.dumps(trade)+"\n")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"market":"0xabc","trade_chart_coverage":{
        "from_block":10,"to_block":20,"path":"events.jsonl"},"event_partitions":[]}))
    with pytest.raises(ValueError):
        build_chart(manifest,tmp_path / "chart.json",8)


def test_concurrent_atomic_writes_have_distinct_temporary_files(tmp_path, monkeypatch):
    barrier = threading.Barrier(2)
    original_replace = Path.replace
    temporary_paths = []
    def replace(source, target):
        temporary_paths.append(source)
        barrier.wait(timeout=5)
        return original_replace(source, target)
    monkeypatch.setattr(Path, "replace", replace)
    destination = tmp_path / "status.json"
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = [workers.submit(atomic_write_bytes, destination, value, durable=False)
                   for value in (b"first", b"second")]
        for result in results:
            result.result()
    assert len(set(temporary_paths)) == 2
    assert destination.read_bytes() in {b"first", b"second"}
    assert not list(tmp_path.glob("*.tmp"))


def test_manager_start_does_not_delete_another_workers_temporary_file(tmp_path):
    job = tmp_path / "job-active"
    job.mkdir()
    temporary = job / "status.json.123.tmp"
    temporary.write_bytes(b"active writer")
    BacktestJobManager(tmp_path)
    assert temporary.read_bytes() == b"active writer"
