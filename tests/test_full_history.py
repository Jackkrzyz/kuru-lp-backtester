"""Bounded ingestion and reconstruction tests; no external network calls."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from eth_abi import encode
import pytest

from kuru_backtester.history import full_history as history
from kuru_backtester.engine import l3_vertical_slice as l3
from kuru_backtester.history import promote_historical_partition as promotion
from kuru_backtester.engine.rebuild_from_chain import EVENT_SPECS, MARKET_ADDRESS, ReconstructionError
from kuru_backtester.server.full_history_monitor import read_progress

MARKET = MARKET_ADDRESS
OWNER = "0x" + "11" * 20
PARAMETERS = {"price_precision": 100, "size_precision": 1000, "tick_size": 1,
              "min_size": 1, "base_decimals": 18, "quote_decimals": 6}


def row(name, values, block, index=0):
    spec = next(item for item in EVENT_SPECS if item.name == name)
    return {"address": MARKET, "block_number": block, "block_timestamp": 3600 + block,
            "transaction_hash": "0x" + f"{block:064x}", "transaction_index": 0,
            "log_index": index, "topic0": spec.topic, "data": "0x" + encode(spec.types, values).hex()}


def rows():
    return [row("OrderCreated", (1, OWNER, 1000, 100, True), 10),
            row("OrderCreated", (2, OWNER, 1000, 110, False), 10, 1),
            row("Trade", (2, OWNER, True, 110 * 10**18 // 100, 800, OWNER, OWNER, 200), 12)]


def book_at(block):
    book = l3.L3Book()
    for item in rows():
        if item["block_number"] <= block:
            l3.apply_event(book, l3.decode_event(item), PARAMETERS["price_precision"])
    return book


class FakeRpc:
    url = "test://rpc"
    def logs(self, market, first, last):
        return [l3.normalize_hypersync_log(item) for item in rows() if first <= item["block_number"] <= last]
    def batch(self, calls):
        return [{"hash": f"hash-{int(params[0],16)}"} for method, params in calls]
    def block_timestamps(self, blocks):
        return {block: 3600 + block for block in blocks}


def install_fake_contract(monkeypatch):
    monkeypatch.setattr(history, "contract_guard", lambda *args: {"implementation_code_sha256": history.KNOWN_IMPLEMENTATION})
    def capture(url, market, block, destination):
        history.atomic_json(destination, history.checkpoint_payload(market, block, 3600 + block, PARAMETERS, book_at(block)))
        return history.content_hash(destination)
    monkeypatch.setattr(history, "capture_checkpoint", capture)
    monkeypatch.setattr(history, "market_parameters", lambda *args: dict(PARAMETERS))
    monkeypatch.setattr(l3, "load_checkpoint", lambda rpc, market, block: (book_at(block), *book_at(block).aggregate()))
    def compare(book, rpc, market, block):
        assert book.aggregate() == book_at(block).aggregate()
        return 1, 1
    monkeypatch.setattr(l3, "compare_l2", compare)


def test_partition_plan_is_bounded_and_inclusive():
    assert list(history.partitions(10, 20, 4)) == [(10, 13), (14, 17), (18, 20)]
    for args in ((20, 10, 4), (0, 10, 4), (1, 100, 10001)):
        with pytest.raises(ValueError):
            list(history.partitions(*args))


def test_action_trace_reports_real_pages_and_is_bounded(tmp_path):
    progress = history.Progress(tmp_path / "progress.json", {"from_block": 10, "to_block": 20})
    progress.observe({"kind": "request", "from_block": 10, "to_block_exclusive": 21, "lookup_only": False})
    progress.observe({"kind": "response", "headers": {}, "decoded_response_bytes": 100, "log_count": 3, "block_count": 2, "next_block": 21})
    progress.observe({"kind": "page_committed", "next_block": 21, "page_events": 3, "new_events": 3, "event_counts": {"Trade": 1, "OrderCreated": 2}})
    saved = json.loads(progress.path.read_text())
    assert saved["last_response"]["log_count"] == 3
    assert saved["last_committed_page"]["event_counts"] == {"Trade": 1, "OrderCreated": 2}
    assert saved["validated_events"] == 0
    progress.observe({"kind": "rate_limit_wait", "seconds": 3, "headers": {"Retry-After": "3"}})
    assert progress.value["phase"] == "rate_limit_wait"
    assert progress.value["rate_limit_waits"] == 1
    for _ in range(100):
        progress.observe({"kind": "request", "from_block": 10, "to_block_exclusive": 21})
    assert len(progress.value["actions"]) == 60
    assert progress.value["actions"][-1]["sequence"] > 100


def test_interrupted_page_recovery_and_identity(tmp_path):
    signature = {"from_block": 10, "to_block": 20}
    partition = history.DurablePartition(tmp_path, signature)
    partition.append(rows()[:1])
    partition.state["next_block"] = 11
    partition.save()
    partition.append(rows()[1:])  # Crash before committing the cursor.
    with partition.events_path.open("ab") as output:
        output.write(b'{"incomplete')
    resumed = history.DurablePartition(tmp_path, signature)
    assert resumed.state["next_block"] == 11
    assert len(resumed.identities) == 1
    assert resumed.append(rows()) == 2
    resumed.save()
    assert len(history.DurablePartition(tmp_path, signature).identities) == 3
    with pytest.raises(ValueError, match="identity"):
        history.DurablePartition(tmp_path, {"from_block": 9, "to_block": 20})


def test_writer_lock_excludes_second_process_handle(tmp_path):
    with history.writer_lock(tmp_path / "lock"):
        with pytest.raises(RuntimeError, match="Another process"):
            with history.writer_lock(tmp_path / "lock"):
                pass
    with history.writer_lock(tmp_path / "lock"):
        pass


def test_trade_candles_only_and_raw_integer_volume():
    source = rows()
    candles = history.trade_candles(source, [l3.decode_event(item) for item in source], 1000)
    assert len(candles) == 1
    assert candles[0]["trade_count"] == 1
    assert candles[0]["open"] == "1.1"
    assert candles[0]["volume_mon"] == "0.2"
    assert candles[0]["volume_raw"] == "200"


def test_candle_export_merges_hours_and_is_idempotent(tmp_path):
    for first, price in ((10, 2 * 10**18), (20, 10**18)):
        folder = tmp_path / f"{first}-{first+9}"
        folder.mkdir()
        history.atomic_json(folder / "summary.json", {})
        trade = row("Trade", (2, OWNER, True, price, 0, OWNER, OWNER, 200), first)
        history.atomic_json(folder / "candles.json", history.trade_candles([trade], [l3.decode_event(trade)], 1000))
    history.export_candles(tmp_path)
    before = (tmp_path / "candles.jsonl").read_bytes()
    history.export_candles(tmp_path)
    assert (tmp_path / "candles.jsonl").read_bytes() == before
    candle = json.loads(before)
    assert (candle["open"], candle["close"], candle["high"], candle["low"]) == ("2", "1", "2", "1")
    assert candle["volume_raw"] == "400" and candle["trade_count"] == 2


def test_reconstruction_gates_and_deterministic_report(tmp_path, monkeypatch):
    install_fake_contract(monkeypatch)
    history.atomic_json(tmp_path / "config.json", {})
    events = tmp_path / "events.jsonl"
    events.write_text("".join(history.canonical_json(item)+"\n" for item in rows()))
    progress = history.Progress(tmp_path / "progress.json", {"from_block": 10})
    summary = history.validate_partition(tmp_path, MARKET, 10, 16, FakeRpc(), progress)
    before = (tmp_path / "summary.json").read_bytes()
    history.validate_partition(tmp_path, MARKET, 10, 16, FakeRpc(), progress)
    assert (tmp_path / "summary.json").read_bytes() == before
    assert summary["event_parity"]["envio_event_count"] == 3
    assert summary["validation"]["queue_invariant_checks"] == 7
    assert summary["validation"]["final_l3_checkpoint_match"] is True
    assert summary["event_counts"]["Trade"] == 1
    assert summary["event_input"]["source_sha256"] == hashlib.sha256(events.read_bytes()).hexdigest()
    assert not (tmp_path / "timeline.jsonl").exists()


@pytest.mark.parametrize("fault", ["parity", "timestamp", "queue", "vault"])
def test_validation_fails_closed(tmp_path, monkeypatch, fault):
    install_fake_contract(monkeypatch)
    source = rows()
    rpc = FakeRpc()
    if fault == "parity":
        rpc.logs = lambda *args: []
    elif fault == "timestamp":
        source[0]["block_timestamp"] = 0
    elif fault == "queue":
        source[2] = row("Trade", (999, OWNER, True, 110 * 10**18 // 100, 0, OWNER, OWNER, 200), 12)
        rpc.logs = lambda *args: [l3.normalize_hypersync_log(item) for item in source]
    elif fault == "vault":
        source[2] = row("Trade", (0, OWNER, True, 110 * 10**18 // 100, 0, OWNER, OWNER, 200), 12)
        rpc.logs = lambda *args: [l3.normalize_hypersync_log(item) for item in source]
    (tmp_path / "events.jsonl").write_text("".join(history.canonical_json(item)+"\n" for item in source))
    progress = history.Progress(tmp_path / "progress.json", {"from_block": 10})
    with pytest.raises(ReconstructionError):
        history.validate_partition(tmp_path, MARKET, 10, 16, rpc, progress)
    assert not (tmp_path / "summary.json").exists()


def test_telemetry_rate_wait_and_missing_job(tmp_path):
    progress = history.Progress(tmp_path / "progress.json", {"from_block": 10})
    progress.observe({"kind": "request", "from_block": 10})
    progress.observe({"kind": "rate_limit_wait", "seconds": 15, "headers": {"retry-after": "15"}})
    saved = read_progress(tmp_path / "progress.json")
    assert saved["phase"] == "rate_limit_wait"
    assert saved["requests"] == 1
    assert saved["validated_events"] == 0
    assert read_progress(tmp_path / "missing")["phase"] == "idle"


def test_empty_network_partition_can_complete(tmp_path):
    partition = history.DurablePartition(tmp_path, {"from_block": 10, "to_block": 16})
    client = SimpleNamespace(query=lambda *args, **kwargs: {"next_block": 17, "data": []})
    history.run_index(client, partition, MARKET, 17)
    assert partition.state["complete"]
    assert partition.state["event_count"] == 0
    assert partition.state["event_file_sha256"] == hashlib.sha256(b"").hexdigest()


def test_full_pipeline_resume_without_second_download(tmp_path, monkeypatch):
    install_fake_contract(monkeypatch)
    monkeypatch.setattr(history, "ROOT", tmp_path)
    monkeypatch.setattr(promotion, "ROOT", tmp_path)
    manifest = tmp_path / "manifest.json"
    job = tmp_path / ".cache/job"
    job.mkdir(parents=True)
    configuration = {"market": MARKET, "from_block": 10, "to_block": 16, "partition_blocks": 7, "deployment_block": 10}
    calls = []
    def query(*args, **kwargs):
        calls.append(args)
        return {"next_block": 17, "data": [{"blocks": [{"number": block, "timestamp": 3600+block} for block in (10,12)], "logs": rows()}]}
    client = SimpleNamespace(url="test://hypersync", query=query)
    progress = history.Progress(job / "progress.json", configuration)
    history.run_pipeline(configuration, job, manifest, client, FakeRpc(), progress)
    first_hash = history.content_hash(manifest)
    history.run_pipeline(configuration, job, manifest, client, FakeRpc(), progress)
    assert len(calls) == 1
    assert history.content_hash(manifest) == first_hash
    assert progress.value["validated_events"] == 3
    assert progress.value["phase"] == "complete"
    assert len(json.loads(manifest.read_text())["event_partitions"]) == 1


def test_download_only_never_promotes(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "ROOT", tmp_path)
    configuration = {"market": MARKET, "from_block": 10, "to_block": 16, "partition_blocks": 7, "deployment_block": 10, "download_only": True}
    client = SimpleNamespace(url="test://hypersync", query=lambda *args, **kwargs: {"next_block": 17, "data": []})
    progress = history.Progress(tmp_path / "progress.json", configuration)
    history.run_pipeline(configuration, tmp_path / "job", tmp_path / "manifest.json", client, FakeRpc(), progress)
    assert progress.value["validated_events"] == 0
    assert not (tmp_path / "manifest.json").exists()


def test_monitor_http_paths(tmp_path):
    import threading
    from urllib.request import urlopen
    from urllib.error import HTTPError
    from http.server import ThreadingHTTPServer
    from kuru_backtester.server.full_history_monitor import MonitorHandler
    handler = type("TestHandler", (MonitorHandler,), {"progress_path": tmp_path / "missing"})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        root = f"http://127.0.0.1:{server.server_port}"
        assert json.load(urlopen(root + "/api/indexing-progress"))["phase"] == "idle"
        page = urlopen(root).read()
        assert b"Onchain history, reconstructed with Envio." in page
        assert b"<header>" not in page
        assert b"ENVIO HYPERSYNC" not in page
        with pytest.raises(HTTPError) as error:
            urlopen(root + "/../historical_index_manifest.json")
        assert error.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_minimal_monitor_renders_real_states():
    import subprocess
    from kuru_backtester.server.backtest_service import resolve_node
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([resolve_node(), "test_indexing_ui.js"], cwd=root / "sdk_reference",
                            check=True, capture_output=True, text=True)
    assert "tests passed" in result.stdout
