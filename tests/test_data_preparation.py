"""Offline coordinator tests. Real reconstruction and source writes are unchanged."""
import threading
import time

import pytest

from kuru_backtester.engine.rebuild_from_chain import DEPLOYMENT_BLOCK, MARKET_ADDRESS
from kuru_backtester.server.data_suite import DataSuite, DataSuiteError
from kuru_backtester.history import verify_all_downloads as queue


class Child:
    def __init__(self):
        self.returncode = None
    def poll(self):
        return self.returncode
    def terminate(self):
        self.returncode = -15
    def wait(self, timeout=None):
        return self.returncode


def wait_until(predicate):
    deadline = time.monotonic() + 8
    while not predicate():
        assert time.monotonic() < deadline, "Coordinator did not reach expected state"
        time.sleep(0.01)


@pytest.fixture
def suite(tmp_path, monkeypatch):
    monkeypatch.setenv("ENVIO_API_TOKEN", "offline-test")
    from kuru_backtester.history.full_history import atomic_json
    value = DataSuite(tmp_path, spawn=lambda *a, **kw: Child())
    atomic_json(value.directory / "network.json", {"finalized_block": DEPLOYMENT_BLOCK + 19})
    monkeypatch.setattr(queue, "downloaded_partitions", lambda *a: [])
    monkeypatch.setattr(queue, "verified_ranges", lambda *a: [])
    yield value
    value.close()


def config():
    return {"market": MARKET_ADDRESS, "from_block": DEPLOYMENT_BLOCK,
            "to_block": DEPLOYMENT_BLOCK + 19, "confirm_download": True}


def test_backlog_and_download_run_together_then_final_queue_catches_up(suite, monkeypatch):
    calls, snapshots = [], []
    downloaded = threading.Event()
    def verify():
        calls.append("verify")
        snapshots.append(downloaded.is_set())
        suite.verification_process = Child()
        suite.verification_process.returncode = 0
    def download(payload):
        calls.append("download")
        assert payload["from_block"] == DEPLOYMENT_BLOCK
        assert payload["to_block"] == DEPLOYMENT_BLOCK + 19
        suite.process = Child()
    monkeypatch.setattr(suite, "start_all_verification", verify)
    monkeypatch.setattr(suite, "start", download)
    monkeypatch.setattr(suite, "verification_queue_status", lambda: {"phase": "complete", "progress": {}})
    suite.start_preparation(config())
    wait_until(lambda: calls == ["verify", "download"])
    assert suite.pipeline["running"]
    with pytest.raises(DataSuiteError, match="already running"):
        suite.start_preparation(config())
    downloaded.set()
    suite.process.returncode = 0
    wait_until(lambda: not suite.pipeline["running"])
    assert suite.pipeline["phase"] == "complete"
    assert snapshots == [False, True], "Final snapshot must see newly completed data"


def test_no_download_when_all_data_is_durable(suite, monkeypatch):
    monkeypatch.setattr(queue, "downloaded_partitions", lambda *a: [
        {"from_block": DEPLOYMENT_BLOCK, "to_block": DEPLOYMENT_BLOCK + 19}])
    monkeypatch.setattr(suite, "start", lambda *a: pytest.fail("Already saved data must not be downloaded"))
    def verify():
        suite.verification_process = Child()
        suite.verification_process.returncode = 0
    monkeypatch.setattr(suite, "start_all_verification", verify)
    monkeypatch.setattr(suite, "verification_queue_status", lambda: {"phase": "complete", "progress": {}})
    suite.start_preparation(config())
    wait_until(lambda: not suite.pipeline["running"])
    assert suite.pipeline["phase"] == "complete"


def test_partial_archive_resumes_original_range_not_shifted_gap(suite, monkeypatch):
    from kuru_backtester.history.full_history import atomic_json
    atomic_json(suite.directory / "jobs/0123456789abcdef/request.json", {
        **config(), "download_only": True, "partition_blocks": 5000})
    monkeypatch.setattr(queue, "downloaded_partitions", lambda *a: [
        {"from_block": DEPLOYMENT_BLOCK, "to_block": DEPLOYMENT_BLOCK + 9,
         "source": ".cache/data_suite/jobs/0123456789abcdef"}])
    submitted = []
    def verify():
        suite.verification_process = Child()
    def download(payload):
        submitted.append(payload)
        suite.process = Child()
    monkeypatch.setattr(suite, "start_all_verification", verify)
    monkeypatch.setattr(suite, "start", download)
    suite.start_preparation(config())
    wait_until(lambda: bool(submitted))
    assert submitted[0]["from_block"] == DEPLOYMENT_BLOCK
    assert submitted[0]["to_block"] == DEPLOYMENT_BLOCK + 19
    suite.stop_preparation()
    suite._pipeline_thread.join(3)


def test_later_queue_runs_while_download_is_still_active(suite, monkeypatch):
    calls = []
    def verify():
        calls.append(suite.process is not None and suite.process.poll() is None)
        suite.verification_process = Child()
        suite.verification_process.returncode = 0
    monkeypatch.setattr(suite, "start_all_verification", verify)
    monkeypatch.setattr(suite, "verification_queue_status", lambda: {"phase": "complete", "progress": {}})
    # Advance the idle timer deterministically, without a 30-second test wait.
    counter = iter(range(0, 10000, 31))
    monkeypatch.setattr("kuru_backtester.server.data_suite.time.monotonic", lambda: next(counter))
    suite.start_preparation(config())
    deadline = time.time() + 5
    while len(calls) < 2:
        assert time.time() < deadline
        time.sleep(0.01)
    assert calls[:2] == [False, True]
    suite.stop_preparation()
    suite._pipeline_thread.join(3)


def test_verification_failure_stops_download_and_preserves_error(suite, monkeypatch):
    def verify():
        suite.verification_process = Child()
    monkeypatch.setattr(suite, "start_all_verification", verify)
    monkeypatch.setattr(suite, "verification_queue_status", lambda: {
        "phase": "failed", "progress": {"error": "L2 mismatch"}})
    suite.start_preparation(config())
    wait_until(lambda: suite.process is not None)
    suite.verification_process.returncode = 1
    wait_until(lambda: not suite.pipeline["running"])
    suite._pipeline_thread.join(2)
    assert suite.pipeline["error"] == "L2 mismatch"
    assert suite.process.returncode == -15


def test_stop_owns_both_children_and_blocks_manual_worker_start(suite):
    suite.start_preparation(config())
    wait_until(lambda: suite.process is not None and suite.verification_process is not None)
    with pytest.raises(DataSuiteError, match="owns"):
        suite.start_all_verification()
    suite.stop_preparation()
    suite._pipeline_thread.join(3)
    assert suite.pipeline["phase"] == "stopped"
    assert suite.process.returncode == suite.verification_process.returncode == -15


def test_unconfirmed_or_unbounded_request_never_starts_workers(suite):
    for payload in ({**config(), "confirm_download": False},
                    {**config(), "to_block": DEPLOYMENT_BLOCK + 20}):
        with pytest.raises(DataSuiteError):
            suite.start_preparation(payload)
    assert suite.process is suite.verification_process is None
