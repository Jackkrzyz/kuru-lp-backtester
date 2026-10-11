"""Data-page controls never change engine state or start jobs during polling."""
import json
from pathlib import Path
import pytest

from kuru_backtester.server.data_suite import DataSuite, DataSuiteError, merge_ranges
from kuru_backtester.engine.rebuild_from_chain import DEPLOYMENT_BLOCK, MARKET_ADDRESS
from kuru_backtester.history.index_history import EVENT_TOPICS, SCHEMA_VERSION


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def test_range_union_preserves_gaps():
    assert merge_ranges([{"from_block": 10, "to_block": 20}, {"from_block": 15, "to_block": 25},
                         {"from_block": 30, "to_block": 40}]) == [
        {"from_block": 10, "to_block": 25}, {"from_block": 30, "to_block": 40}]


def test_new_server_observes_saved_job_without_claiming_ownership(tmp_path):
    suite = DataSuite(tmp_path)
    save(suite.directory / "jobs" / "external" / "progress.json", {"phase": "downloading", "downloaded_events": 100})
    progress = suite.progress()
    assert progress["observed_job_id"] == "external"
    assert progress["managed_by_this_server"] is False
    assert progress["downloaded_events"] == 100
    with pytest.raises(DataSuiteError, match="No download"):
        suite.stop()


def test_inventory_separates_trade_discovery_and_partial_l3(tmp_path):
    first = DEPLOYMENT_BLOCK
    for name, topics, end in [("archive", EVENT_TOPICS, first + 9), ("trade", EVENT_TOPICS[:1], first + 99)]:
        directory = tmp_path / ".cache" / name
        save(directory / "state.json", {"signature": {"market": MARKET_ADDRESS, "topics": topics,
             "schema_version": SCHEMA_VERSION, "log_fields": ["address"],
             "from_block": first, "to_block": first + 99}, "next_block": end + 1})
        (directory / "events.jsonl").write_text("{}\n")
    suite = DataSuite(tmp_path, spawn=lambda *args, **kwargs: pytest.fail("Polling must not spawn a worker"))
    suite.inventory()  # A completed scan is published to the non-blocking status route.
    value = suite.status()
    assert value["downloaded_ranges"] == [{"from_block": first, "to_block": first + 9}]
    assert value["verified_blocks"] == 0
    assert not value["scope_is_finalized"]
    assert value["total_saved_bytes"] == sum(path.stat().st_size for path in (tmp_path / ".cache").rglob("*") if path.is_file())


def test_manifest_missing_files_not_counted_and_overlaps_deduplicated(tmp_path):
    first = DEPLOYMENT_BLOCK
    save(tmp_path / "events.jsonl", {})
    save(tmp_path / "summary.json", {})
    save(tmp_path / "checkpoint.json", {})
    entry = {"from_block": first, "to_block": first+9, "path": "events.jsonl", "sha256": "recorded",
             "validation_summary": "summary.json", "validation_summary_sha256": "recorded"}
    manifest = {"market": MARKET_ADDRESS, "event_partitions": [entry],
                "checkpoints": [{"block_number": first-1, "path": "checkpoint.json", "sha256": "recorded"}]}
    save(tmp_path / "historical_index_manifest.json", manifest)
    save(tmp_path / "full_history_manifest.json", {**manifest, "event_partitions": [entry, {**entry, "path": "missing.jsonl"}]})
    value = DataSuite(tmp_path).inventory()
    assert value["downloaded_blocks"] == value["verified_blocks"] == 10
    assert len(value["warnings"]) == 1


class Process:
    returncode = None
    def poll(self):
        return self.returncode
    def terminate(self):
        self.returncode = -15
    def wait(self, timeout=None):
        return self.returncode


@pytest.fixture
def ready(tmp_path, monkeypatch):
    monkeypatch.setenv("ENVIO_API_TOKEN", "test-token")
    calls = []
    def spawn(command, **kwargs):
        calls.append((command, kwargs))
        return Process()
    suite = DataSuite(tmp_path, spawn=spawn)
    save(suite.directory / "network.json", {"finalized_block": DEPLOYMENT_BLOCK + 100, "checked_at": 1})
    yield suite, calls
    suite.close()
    if suite._inventory_thread:
        suite._inventory_thread.join(2)


def request():
    return {"market": MARKET_ADDRESS, "from_block": DEPLOYMENT_BLOCK, "to_block": DEPLOYMENT_BLOCK+9, "confirm_download": True}


def test_start_stop_resume_same_cursor_directory(ready):
    suite, calls = ready
    created = suite.start(request())
    command, options = calls[0]
    assert "--download-only" in command and "--execute" in command
    assert command[command.index("--from-block")+1] == str(DEPLOYMENT_BLOCK)
    assert options["env"]["ENVIO_API_TOKEN"] == "test-token"
    assert suite.status()["jobs"][0]["running"]
    with pytest.raises(DataSuiteError, match="already running"):
        suite.start(request())
    suite.stop()
    assert suite.progress()["phase"] == "stopped"
    assert suite.start(request())["job_id"] == created["job_id"]
    assert calls[0][0] == calls[1][0]
    assert "test-token" not in json.dumps(suite.status())


@pytest.mark.parametrize("change", [{"confirm_download": False}, {"from_block": 1},
    {"to_block": DEPLOYMENT_BLOCK+101}, {"from_block": 1.5}, {"from_block": True}, {"market": "0xforeign"}])
def test_invalid_download_cannot_spawn(ready, change):
    suite, calls = ready
    with pytest.raises(DataSuiteError):
        suite.start({**request(), **change})
    assert not calls


def test_env_file_reads_only_known_keys(tmp_path, monkeypatch):
    monkeypatch.delenv("ENVIO_API_TOKEN", raising=False)
    (tmp_path / ".env").write_text('ENVIO_API_TOKEN="local-token" # secret\nUNRELATED_KEY=ignored\n')
    environment = DataSuite(tmp_path).environment()
    assert environment["ENVIO_API_TOKEN"] == "local-token"
    assert "UNRELATED_KEY" not in environment


def test_refresh_finalized_uses_only_two_rpc_calls(tmp_path, monkeypatch):
    import kuru_backtester.server.data_suite as module
    calls = []
    class Rpc:
        def __init__(self, *args, **kwargs):
            pass
        def call(self, method, arguments):
            calls.append((method, arguments))
            return "0x8f" if method == "eth_chainId" else {"number": hex(DEPLOYMENT_BLOCK+100)}
    monkeypatch.setattr(module, "RpcClient", Rpc)
    suite = DataSuite(tmp_path)
    suite.refresh_head()
    assert suite.inventory()["scope_to_block"] == DEPLOYMENT_BLOCK+100
    assert calls == [("eth_chainId", []), ("eth_getBlockByNumber", ["finalized", False])]


def test_data_suite_ui_request_and_coverage_rendering():
    import subprocess
    from kuru_backtester.server.backtest_service import resolve_node
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([resolve_node(), "test_data_suite_ui.js"], cwd=root / "sdk_reference",
                            capture_output=True, text=True, check=True)
    assert "tests passed" in result.stdout


def test_http_download_controls_receive_exact_range(ready):
    import threading
    from http.server import ThreadingHTTPServer
    from urllib.request import urlopen, Request
    from urllib.error import HTTPError
    from kuru_backtester.server.backtest_server import BacktestHandler
    suite, calls = ready
    handler = type("DataTestHandler", (BacktestHandler,), {"data_suite": suite})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        initial = json.load(urlopen(base + "/api/data-suite"))
        assert initial["verified_blocks"] in (None, 0)
        assert calls == []
        created = json.load(urlopen(Request(base + "/api/data-suite/download", data=json.dumps(request()).encode(),
                                           headers={"Content-Type": "application/json"})))
        assert created["request"]["from_block"] == DEPLOYMENT_BLOCK
        assert len(calls) == 1
        with pytest.raises(HTTPError) as error:
            urlopen(Request(base + "/api/data-suite/stop", data=b"{}", headers={"Origin": "https://foreign.test"}))
        assert error.value.code == 403
        json.load(urlopen(Request(base + "/api/data-suite/stop", data=b"{}")))
        assert not suite.status()["jobs"][0]["running"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_slow_inventory_does_not_block_http_status_or_progress(ready, monkeypatch):
    import threading
    from http.server import ThreadingHTTPServer
    from urllib.request import urlopen
    from kuru_backtester.server.backtest_server import BacktestHandler
    suite, calls = ready
    suite.active_id = "isolated"
    entered, release = threading.Event(), threading.Event()
    scan_count = []
    original = suite._scan_inventory
    def scan():
        scan_count.append(1)
        entered.set()
        assert release.wait(5)
        return original()
    monkeypatch.setattr(suite, "_scan_inventory", scan)
    handler = type("SlowScanHandler", (BacktestHandler,), {"data_suite": suite})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        first = json.load(urlopen(base + "/api/data-suite", timeout=1))
        assert entered.wait(1)
        assert first["inventory_ready"] is False
        assert first["total_saved_bytes"] is None
        for _ in range(3):
            assert json.load(urlopen(base + "/api/data-suite", timeout=1))["inventory_refreshing"]
            assert json.load(urlopen(base + "/api/indexing-progress", timeout=1))["phase"] == "starting"
        assert len(scan_count) == 1
        assert not calls
    finally:
        release.set()
        suite._inventory_thread.join(2)
        server.shutdown()
        server.server_close()
        thread.join()
    assert suite.inventory_snapshot()["inventory_ready"]


def test_cache_age_starts_after_scan_and_refresh_preserves_previous_totals(tmp_path, monkeypatch):
    import kuru_backtester.server.data_suite as module
    suite = DataSuite(tmp_path)
    clock = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    original = suite._scan_inventory
    def scan():
        result = original()
        clock[0] += 20
        return result
    monkeypatch.setattr(suite, "_scan_inventory", scan)
    first = suite.inventory()
    assert suite.cached_at == 120
    assert suite.inventory_seconds == 20
    assert suite.inventory() is first
    assert clock[0] == 120
    clock[0] = 181
    def fail():
        raise OSError("disk scan unavailable")
    monkeypatch.setattr(suite, "_scan_inventory", fail)
    previous = suite.inventory_snapshot()
    assert previous["total_saved_bytes"] == first["total_saved_bytes"]
    suite._inventory_thread.join(2)
    snapshot = suite.inventory_snapshot()
    assert snapshot["inventory_ready"]
    assert snapshot["inventory_error"] == "disk scan unavailable"
    assert snapshot["inventory_refreshing"] is False
    assert snapshot["inventory_updated_at"] == previous["inventory_updated_at"]


def test_inventory_ignores_symlinked_files_and_directories(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "large.json").write_text("not in archive")
    cache = tmp_path / ".cache"
    cache.mkdir()
    (cache / "retained.json").write_text("123")
    try:
        (cache / "linked").symlink_to(outside, target_is_directory=True)
        (cache / "linked.json").symlink_to(outside / "large.json")
    except OSError:
        pytest.skip("Host does not permit symlinks")
    value = DataSuite(tmp_path).inventory()
    assert value["storage_bytes"][".cache"] == 3
    assert value["saved_file_count"] == 1
