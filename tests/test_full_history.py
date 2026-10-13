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


def downloaded_fixture(tmp_path, monkeypatch):
    """Create a complete partition next to an incomplete active partition."""
    from kuru_backtester.history import verify_download as verify
    monkeypatch.setattr(history, "ROOT", tmp_path)
    monkeypatch.setattr(promotion, "ROOT", tmp_path)
    install_fake_contract(monkeypatch)
    source = tmp_path / ".cache" / "archive"
    part = source / "10-12"
    part.mkdir(parents=True)
    events = "".join(json.dumps(row) + "\n" for row in rows()).encode()
    (part / "events.jsonl").write_bytes(events)
    state = {"signature": {"market": MARKET, "schema_version": verify.SCHEMA_VERSION,
             "from_block": 10, "to_block": 12, "topics": verify.EVENT_TOPICS,
             "log_fields": verify.LOG_FIELDS, "block_fields": verify.BLOCK_FIELDS},
             "complete": True, "next_block": 13, "durable_page_schema": 1,
             "committed_bytes": len(events), "event_count": len(rows())}
    history.atomic_json(part / "state.json", state)
    history.atomic_json(source / "job.json", {"market": MARKET, "from_block": 10, "to_block": 15, "partition_blocks": 3})
    history.atomic_json(source / "progress.json", {"downloaded_block": 14})
    unfinished = source / "13-15"
    unfinished.mkdir()
    (unfinished / "events.jsonl").write_bytes(b"unfinished page")
    rpc = FakeRpc()
    rpc.call = lambda method, params: "0x8f"
    return verify, source, tmp_path / ".cache" / "verification", tmp_path / "full_history_verify_test_manifest.json", rpc


def test_verified_candles_publish_before_download_queue_finishes(tmp_path, monkeypatch):
    from kuru_backtester.server.market_chart import MarketChart
    verify, source, output, manifest, rpc = downloaded_fixture(tmp_path, monkeypatch)
    verify.verify(source, output, manifest, [(10, 12)], rpc)
    # The second downloaded partition remains incomplete. No LP promotion or
    # completion of the whole download queue is needed to display checked Trades.
    index = MarketChart(tmp_path, ready_only=False, verified_only=True)
    index.build()
    result = index.snapshot()
    assert result["verified_only"] and not result["backtest_ready_only"]
    assert result["indexed_ranges"] == [{"from_block": 10, "to_block": 12}]
    assert sum(bar["trades"] for bar in result["candles"]) == 1
    assert not (tmp_path / "historical_index_manifest.json").exists()
    # A modified source must be excluded even if its old candles are cached.
    with (output / "10-12/events.jsonl").open("a") as stream:
        stream.write("{}\n")
    index.build()
    assert index.coverage == []
    assert index.bars == []
    assert "hash" in index.state["warning"]
    index.close()


def test_new_verified_partition_keeps_existing_candles_during_refresh(tmp_path, monkeypatch):
    import threading
    from kuru_backtester.server.market_chart import MarketChart
    verify, source, output, manifest, rpc = downloaded_fixture(tmp_path, monkeypatch)
    verify.verify(source, output, manifest, [(10, 12)], rpc)
    index = MarketChart(tmp_path, verified_only=True)
    index.build()
    original = index.snapshot()["candles"]
    later = output / "13-19"
    later.mkdir()
    (later / "events.jsonl").write_text("")
    history.validate_partition(later, MARKET, 13, 19, rpc,
                               history.Progress(later / "progress.json", {"from_block": 13, "to_block": 19}))
    history.promote(manifest, later / "events.jsonl", later / "summary.json", later / "checkpoint.json")
    entered, release = threading.Event(), threading.Event()
    build = index.build
    def delayed():
        entered.set()
        assert release.wait(5)
        build()
    monkeypatch.setattr(index, "build", delayed)
    try:
        assert index.snapshot()["candles"] == original
        assert entered.wait(2)
        assert index.snapshot()["candles"] == original
    finally:
        release.set()
        index.thread.join(5)
        index.close()
    assert index.coverage == [(10, 19)]
    assert index.snapshot()["candles"] == original


def test_verify_download_preserves_source_and_resumes_without_rpc_replay(tmp_path, monkeypatch):
    verify, source, destination, manifest, rpc = downloaded_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(history, "RateLimitedHyperSyncClient", lambda *args, **kwargs: pytest.fail("Verification must not use HyperSync"))
    before = {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    assert verify.selected_ranges(source, None, None) == [(10, 12)]
    # Holding the download lock must not stop verification of a completed copy.
    with history.writer_lock(source / "writer.lock"):
        verify.verify(source, destination, manifest, [(10, 12)], rpc)
    for path, content in before.items():
        assert (source / path).read_bytes() == content
    summary = json.loads((destination / "10-12/summary.json").read_text())
    assert summary["event_parity"]["envio_event_count"] == 3
    assert summary["validation"]["final_l3_checkpoint_match"] is True
    assert summary["validation"]["maximum_price_level_error_raw"] == 0
    assert not (tmp_path / "historical_index_manifest.json").exists()
    original = manifest.read_bytes()
    monkeypatch.setattr(history, "validate_partition", lambda *args: pytest.fail("Must reuse hash-verified output"))
    verify.verify(source, destination, manifest, [(10, 12)], rpc)
    assert manifest.read_bytes() == original
    progress = json.loads((destination / "progress.json").read_text())
    assert progress["phase"] == "complete"
    assert progress["reconstructed_block"] == 12
    assert progress["l2_checks_current_partition"] == summary["validation"]["queue_invariant_checks"]


def test_unsupported_partition_does_not_stop_later_verified_coverage(tmp_path, monkeypatch):
    verify, source, destination, manifest, rpc = downloaded_fixture(tmp_path, monkeypatch)
    second = source / "13-15"
    (second / "events.jsonl").write_text("")
    state = json.loads((source / "10-12/state.json").read_text())
    state.update(next_block=16, event_count=0, committed_bytes=0)
    state["signature"].update(from_block=13, to_block=15)
    history.atomic_json(second / "state.json", state)
    validate = history.validate_partition
    def unsupported_first(directory, market, first, *args):
        if first == 10:
            raise history.UnsupportedHistoryError("Active vault at block 11")
        return validate(directory, market, first, *args)
    monkeypatch.setattr(history, "validate_partition", unsupported_first)
    result = verify.verify(source, destination, manifest, [(10, 12), (13, 15)], rpc, continue_unsupported=True)
    assert result == {"completed_partitions": 1, "blocked_partitions": 1}
    progress = json.loads((destination / "progress.json").read_text())
    assert progress["verified_ranges"] == [{"from_block": 13, "to_block": 15}]
    assert progress["processed_partitions"] == 2
    assert progress["blocked_partitions"] == 1
    assert progress["phase"] == "complete"
    assert not (destination / "10-12/summary.json").exists()
    assert [(part["from_block"], part["to_block"]) for part in json.loads(manifest.read_text())["event_partitions"]] == [(13, 15)]


def test_vault_preflight_uses_no_rpc(tmp_path):
    event = row("VaultParamsUpdated", (1, 0, 1, 0, 110, 100), 10)
    (tmp_path / "events.jsonl").write_text(json.dumps(event) + "\n")
    progress = history.Progress(tmp_path / "progress.json", {"from_block": 10})
    with pytest.raises(history.UnsupportedHistoryError, match="block 10"):
        history.validate_partition(tmp_path, MARKET, 10, 16, object(), progress)


@pytest.mark.parametrize("fault", [None, "runtime", "bytes", "parameters", "boundary"])
def test_backtest_readiness_gate_and_idempotent_publication(tmp_path, monkeypatch, fault):
    from kuru_backtester.history import backtest_readiness as ready
    install_fake_contract(monkeypatch)
    monkeypatch.setattr(history, "contract_guard", lambda *a: {
        "implementation_code_sha256": history.KNOWN_IMPLEMENTATION, "vault_active": False})
    monkeypatch.setattr(promotion, "ROOT", tmp_path)
    part = tmp_path / "partition"
    part.mkdir()
    (part / "events.jsonl").write_text("".join(json.dumps(item) + "\n" for item in rows()))
    progress = history.Progress(part / "progress.json", {"from_block": 10})
    rpc = FakeRpc()
    summary = history.validate_partition(part, MARKET, 10, 16, rpc, progress)
    manifest = tmp_path / "historical_index_manifest.json"
    history.atomic_json(manifest, {"market": MARKET, "event_partitions": [], "checkpoints": []})
    monkeypatch.setattr(ready, "market_parameters", lambda *a: dict(PARAMETERS))
    rpc.contract_call = lambda *a: "0x" + encode(
        ("address", "uint256", "uint96", "uint256", "uint96", "uint96", "uint96", "uint96"),
        (OWNER, 0, 0, 0, 0, 0, 0, 30)).hex()
    if fault == "runtime":
        summary["contract_guards"][0]["implementation_code_sha256"] = "unsupported"
        history.atomic_json(part / "summary.json", summary)
        assert ready.publish(part, object(), tmp_path)["status"] == "reconstruction_only"
    elif fault:
        if fault == "bytes":
            (part / "events.jsonl").write_text("changed")
        elif fault == "parameters":
            monkeypatch.setattr(ready, "market_parameters", lambda *a: {**PARAMETERS, "min_size": 999})
        else:
            rpc.batch = lambda calls: [{"hash": "changed"} for _ in calls]
        with pytest.raises(ValueError, match="[Hh]ash|parameters"):
            ready.publish(part, rpc, tmp_path)
    else:
        from kuru_backtester.server.backtest_service import BacktestService
        from kuru_backtester.history.historical_system import HistoricalEventStore
        from kuru_backtester.server.market_chart import MarketChart
        service = BacktestService()
        old_store = HistoricalEventStore(manifest)
        service._event_store = old_store
        stat = manifest.stat()
        service._history_index_stamp = (stat.st_mtime_ns, stat.st_size)
        assert ready.publish(part, rpc, tmp_path)["status"] == "ready"
        event_store, _ = service.refresh_historical_index()
        assert len(event_store.partitions) == 1 and old_store.partitions == ()
        monkeypatch.setattr("kuru_backtester.server.market_chart.downloaded_partitions",
                            lambda *a: pytest.fail("Ready chart must not scan downloaded archives"))
        index = MarketChart(tmp_path)
        index.build()
        assert index.state["phase"] == "complete"
        assert sum(bar["trades"] for bar in index.snapshot()["candles"]) == 1
        assert index.coverage == [(10, 16)]
        # An archive overview must never be shown as ready chart data.
        assert index.directory.name == "market_chart_ready"
        index.close()
        before = manifest.read_bytes()
        assert ready.publish(part, object(), tmp_path)["status"] == "already_indexed"
        assert manifest.read_bytes() == before
        rules = json.loads((tmp_path / ".cache/lp_contract_rules.json").read_text())
        assert rules["checkpoints"]["9"]["checkpoint_sha256"] == summary["checkpoint_sha256"]
        rules["checkpoints"]["9"]["implementation_code_sha256"] = "unsupported"
        history.atomic_json(tmp_path / ".cache/lp_contract_rules.json", rules)
        index = MarketChart(tmp_path)
        assert index.overview is None
        index.build()
        assert index.coverage == [] and index.bars == []
        index.close()
        return
    assert json.loads(manifest.read_text())["event_partitions"] == []


@pytest.mark.parametrize("change", [{"complete": False}, {"next_block": 12}, {"committed_bytes": 1},
                                    {"event_count": 99}, {"durable_page_schema": 0}])
def test_verify_download_rejects_incomplete_or_changed_source(tmp_path, monkeypatch, change):
    verify, source, destination, manifest, rpc = downloaded_fixture(tmp_path, monkeypatch)
    state_path = source / "10-12/state.json"
    history.atomic_json(state_path, {**json.loads(state_path.read_text()), **change})
    with pytest.raises(ValueError):
        verify.verify(source, destination, manifest, [(10, 12)], rpc)
    assert json.loads(manifest.read_text())["event_partitions"] == []
    assert json.loads((destination / "progress.json").read_text())["phase"] == "failed"


def test_verify_download_rejects_changed_copy_and_unknown_contract(tmp_path, monkeypatch):
    verify, source, destination, manifest, rpc = downloaded_fixture(tmp_path, monkeypatch)
    source_bytes = (source / "10-12/events.jsonl").read_bytes()
    copy = verify.shutil.copyfile
    def changed_copy(original, output):
        copy(original, output)
        with original.open("ab") as file:
            file.write(b" ")
    monkeypatch.setattr(verify.shutil, "copyfile", changed_copy)
    with pytest.raises(ValueError, match="committed"):
        verify.verify(source, destination, manifest, [(10, 12)], rpc)
    assert json.loads(manifest.read_text())["event_partitions"] == []
    (source / "10-12/events.jsonl").write_bytes(source_bytes)
    monkeypatch.setattr(verify.shutil, "copyfile", copy)
    def unsupported(*args):
        raise ReconstructionError("Unsupported contract implementation")
    monkeypatch.setattr(history, "contract_guard", unsupported)
    with pytest.raises(ReconstructionError, match="Unsupported"):
        verify.verify(source, destination, manifest, [(10, 12)], rpc)
    assert "Unsupported" in json.loads((destination / "progress.json").read_text())["error"]


def test_verify_download_bounds_and_source_identity(tmp_path, monkeypatch):
    verify, source, destination, manifest, rpc = downloaded_fixture(tmp_path, monkeypatch)
    for first, last in ((11, 12), (10, 14), (None, 12), (1, 12)):
        with pytest.raises(ValueError):
            verify.selected_ranges(source, first, last)
    assert verify.completed_bounds({"from_block": 10, "to_block": 14, "partition_blocks": 3}, {"downloaded_block": 14}) == (10, 14)
    assert verify.completed_bounds({"from_block": 10, "to_block": 14, "partition_blocks": 3}, {"downloaded_block": 11}) is None
    path = source / "10-12/state.json"
    state = json.loads(path.read_text())
    state["signature"]["topics"] = []
    history.atomic_json(path, state)
    with pytest.raises(ValueError, match="identity"):
        verify.source_state(path.parent, 10, 12)


def test_verify_download_failure_cannot_promote_and_can_retry(tmp_path, monkeypatch):
    verify, source, destination, manifest, rpc = downloaded_fixture(tmp_path, monkeypatch)
    correct_logs = rpc.logs
    rpc.logs = lambda *args: []
    with pytest.raises(ReconstructionError, match="parity failed"):
        verify.verify(source, destination, manifest, [(10, 12)], rpc)
    assert json.loads(manifest.read_text())["event_partitions"] == []
    rpc.logs = correct_logs
    verify.verify(source, destination, manifest, [(10, 12)], rpc)
    assert len(json.loads(manifest.read_text())["event_partitions"]) == 1
    with pytest.raises(ValueError, match="identity changed"):
        verify.verify(source, destination, manifest, [(10, 15)], rpc)
    (destination / "10-12/end_checkpoint.json").write_text("{}")
    with pytest.raises(ValueError, match="hash mismatch"):
        verify.verify(source, destination, manifest, [(10, 12)], rpc)


def test_verify_all_selects_durable_data_deduplicates_and_skips_verified(tmp_path, monkeypatch):
    from kuru_backtester.history import verify_all_downloads as batch
    import shutil
    verify, source, destination, manifest, rpc = downloaded_fixture(tmp_path, monkeypatch)
    # Discovery manifests are not L3 indexes, even when their market matches.
    history.atomic_json(tmp_path / "interval_manifest.json", {"market": MARKET, "intervals": {}})
    # Include a CLI archive, not just jobs created by the web server.
    shutil.copytree(source, tmp_path / ".cache/duplicate_archive")
    updates = []
    plan = batch.build_plan(tmp_path, lambda **fields: updates.append(fields))
    assert plan["total_partitions"] == 1
    assert plan["skipped_duplicate"] == 1
    assert plan["groups"][0]["ranges"] == [[10, 12]]
    assert plan["completed_download_partitions"] == 2
    before = {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    monkeypatch.setattr(history, "RateLimitedHyperSyncClient", lambda *a, **k: pytest.fail("No Envio calls"))
    queue = tmp_path / ".cache/data_suite/verification_queues/test"
    batch.run(tmp_path, queue, rpc)
    progress = batch.read(queue / "progress.json")
    assert progress["phase"] == "complete" and progress["completed_partitions"] == 1
    assert not (tmp_path / "historical_index_manifest.json").exists()
    for path, content in before.items():
        assert (source / path).read_bytes() == content
    # Repeat after success: verify saved hashes, but do no network work.
    monkeypatch.setattr(verify, "verify", lambda *a, **k: pytest.fail("Already verified"))
    batch.run(tmp_path, queue, object())
    progress = batch.read(queue / "progress.json")
    assert progress["phase"] == "complete" and progress["total_partitions"] == 0
    assert progress["skipped_verified"] == 2
    # A modified checkpoint must not be counted as verified coverage.
    saved_manifest = next(tmp_path.glob("full_history_verify_*manifest.json"))
    checkpoint = batch.read(saved_manifest)["checkpoints"][0]["path"]
    (tmp_path / checkpoint).write_text("{}")
    plan = batch.build_plan(tmp_path, lambda **fields: updates.append(fields))
    assert plan["total_partitions"] == 1 and plan["skipped_verified"] == 0
    assert any("warning" in update for update in updates)


def test_verify_all_accepts_hash_bound_golden_reports_without_new_fields(tmp_path, monkeypatch):
    from kuru_backtester.history import verify_all_downloads as batch
    verify, source, destination, manifest, rpc = downloaded_fixture(tmp_path, monkeypatch)
    verify.verify(source, destination, manifest, [(10, 12)], rpc)
    summary_path = destination / "10-12/summary.json"
    summary = batch.read(summary_path)
    del summary["event_input"]["source_sha256"]
    history.atomic_json(summary_path, summary)
    index = batch.read(manifest)
    index["event_partitions"][0].update(golden_fixture="test_golden",
        validation_summary_sha256=history.content_hash(summary_path))
    history.atomic_json(manifest, index)
    assert batch.build_plan(tmp_path, lambda **k: None)["skipped_verified"] == 1
    # Missing binding is not accepted for a new, non-golden report.
    del index["event_partitions"][0]["golden_fixture"]
    history.atomic_json(manifest, index)
    assert batch.build_plan(tmp_path, lambda **k: None)["total_partitions"] == 1


def test_verify_all_stops_on_failure_and_retries_only_remaining_partitions(tmp_path, monkeypatch):
    from kuru_backtester.history import verify_all_downloads as batch
    verify, source, _, _, rpc = downloaded_fixture(tmp_path, monkeypatch)
    part = source / "13-15"
    (part / "events.jsonl").write_text("")
    state = batch.read(source / "10-12/state.json")
    state.update(next_block=16, event_count=0, committed_bytes=0)
    state["signature"].update(from_block=13, to_block=15)
    history.atomic_json(part / "state.json", state)
    queue = tmp_path / ".cache/data_suite/verification_queues/test"
    validate = history.validate_partition
    def fail_second(directory, market, first, *args):
        if first == 13:
            raise ValueError("RPC unavailable")
        return validate(directory, market, first, *args)
    monkeypatch.setattr(history, "validate_partition", fail_second)
    with pytest.raises(ValueError, match="RPC unavailable"):
        batch.run(tmp_path, queue, rpc)
    assert batch.read(queue / "progress.json")["phase"] == "failed"
    manifest = next(tmp_path.glob("full_history_verify_*manifest.json"))
    saved = batch.read(manifest)["event_partitions"]
    assert [(item["from_block"], item["to_block"]) for item in saved] == [(10, 12)]
    def only_remaining(directory, market, first, *args):
        assert first == 13
        return validate(directory, market, first, *args)
    monkeypatch.setattr(history, "validate_partition", only_remaining)
    batch.run(tmp_path, queue, rpc)
    plan = batch.read(queue / "plan.json")
    assert plan["skipped_verified"] == 1
    assert plan["groups"][0]["ranges"] == [[13, 15]]
    assert batch.read(queue / "progress.json")["phase"] == "complete"
    assert batch.read(manifest)["event_partitions"] == saved


def test_verify_all_empty_and_invalid_completed_archives(tmp_path, monkeypatch):
    from kuru_backtester.history import verify_all_downloads as batch
    queue = tmp_path / ".cache/data_suite/verification_queues/test"
    batch.run(tmp_path, queue, object())
    assert batch.read(queue / "progress.json")["total_partitions"] == 0
    verify, source, _, _, _ = downloaded_fixture(tmp_path, monkeypatch)
    state = batch.read(source / "10-12/state.json")
    state["committed_bytes"] += 1
    history.atomic_json(source / "10-12/state.json", state)
    with pytest.raises(ValueError, match="committed cursor"):
        batch.run(tmp_path, queue, object())
    assert batch.read(queue / "progress.json")["phase"] == "failed"
    assert not list(tmp_path.glob("*manifest.json"))


def test_verify_all_rejects_linked_partition_files(tmp_path, monkeypatch):
    from kuru_backtester.history import verify_all_downloads as batch
    _, source, _, _, _ = downloaded_fixture(tmp_path, monkeypatch)
    events = source / "10-12/events.jsonl"
    other = tmp_path / "other-events.jsonl"
    events.rename(other)
    events.symlink_to(other)
    with pytest.raises(ValueError, match="must not be a link"):
        batch.build_plan(tmp_path, lambda **fields: None)


def test_verify_all_snapshots_work_and_rejects_second_worker(tmp_path, monkeypatch):
    from kuru_backtester.history import verify_all_downloads as batch
    verify, source, _, _, rpc = downloaded_fixture(tmp_path, monkeypatch)
    queue = tmp_path / ".cache/data_suite/verification_queues/test"
    queue.mkdir(parents=True)
    with history.writer_lock(tmp_path / ".cache/data_suite/verify_all.lock"):
        with pytest.raises(RuntimeError, match="Another process"):
            batch.run(tmp_path, queue, rpc)
    original = verify.verify
    def finish_download_then_verify(*args, **kwargs):
        part = source / "13-15"
        (part / "events.jsonl").write_text("")
        state = batch.read(source / "10-12/state.json")
        state.update(next_block=16, event_count=0, committed_bytes=0)
        state["signature"].update(from_block=13, to_block=15)
        history.atomic_json(part / "state.json", state)
        return original(*args, **kwargs)
    monkeypatch.setattr(verify, "verify", finish_download_then_verify)
    batch.run(tmp_path, queue, rpc)
    assert batch.read(queue / "plan.json")["total_partitions"] == 1
    assert batch.build_plan(tmp_path, lambda **k: None)["groups"][0]["ranges"] == [[13, 15]]


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


def test_contiguous_checkpoint_handoff_matches_uncached_validation(tmp_path, monkeypatch):
    install_fake_contract(monkeypatch)
    captures = []
    capture = history.capture_checkpoint
    def counted(*args):
        captures.append(args[2])
        return capture(*args)
    monkeypatch.setattr(history, "capture_checkpoint", counted)
    previous = None
    for first, last in [(10, 16), (17, 23)]:
        directory = tmp_path / str(first)
        directory.mkdir()
        (directory / "events.jsonl").write_text("".join(history.canonical_json(item) + "\n"
            for item in rows() if first <= item["block_number"] <= last))
        progress = history.Progress(directory / "progress.json", {"from_block": first})
        summary = history.validate_partition(directory, MARKET, first, last, FakeRpc(), progress, previous=previous)
        expected = (directory / "summary.json").read_bytes()
        assert progress.value["checkpoint_reused"] is (first == 17)
        previous = history.VerifiedBoundary.from_result(directory, summary)
        if first == 17:
            assert captures == [9]
            history.validate_partition(directory, MARKET, first, last, FakeRpc(), progress)
            assert captures == [9, 16]
            assert (directory / "summary.json").read_bytes() == expected
            assert summary["validation"]["queue_invariant_checks"] == 7


@pytest.mark.parametrize("fault", ["hash", "contract", "parameters", "checkpoint_bytes"])
def test_checkpoint_handoff_fails_closed(tmp_path, monkeypatch, fault):
    install_fake_contract(monkeypatch)
    (tmp_path / "events.jsonl").write_text("".join(history.canonical_json(item) + "\n" for item in rows()))
    progress = history.Progress(tmp_path / "progress.json", {"from_block": 10})
    summary = history.validate_partition(tmp_path, MARKET, 10, 16, FakeRpc(), progress)
    if fault == "checkpoint_bytes":
        (tmp_path / "end_checkpoint.json").write_text("{}")
        with pytest.raises(ReconstructionError, match="handoff hash mismatch"):
            history.VerifiedBoundary.from_result(tmp_path, summary)
        return
    previous = history.VerifiedBoundary.from_result(tmp_path, summary)
    header = {"hash": "hash-16"}
    if fault == "hash":
        header["hash"] = "changed"
    elif fault == "contract":
        monkeypatch.setattr(history, "contract_guard", lambda *a: {"implementation_code_sha256": "changed"})
    else:
        monkeypatch.setattr(history, "market_parameters", lambda *a: {**PARAMETERS, "min_size": 999})
    with pytest.raises(ReconstructionError, match="handoff"):
        previous.write_start(FakeRpc(), MARKET, 17, header, tmp_path / "next.json")
    assert not (tmp_path / "next.json").exists()
    # Gaps and other markets must use independent RPC checkpoints.
    assert previous.write_start(FakeRpc(), MARKET, 18, header, tmp_path / "next.json") is None
    assert previous.write_start(FakeRpc(), "0xother", 17, header, tmp_path / "next.json") is None


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
        assert b"kuruLab" in page
        assert b'id="activity-log"' in page
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
