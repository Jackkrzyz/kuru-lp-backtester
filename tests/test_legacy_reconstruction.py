"""Replay real deployment-era RPC captures. Missing responses fail, never fetch."""
import copy
import gzip
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from eth_abi import encode

from kuru_backtester.engine import l3_vertical_slice as l3
from kuru_backtester.engine.rebuild_from_chain import MARKET_ADDRESS, ReconstructionError, RpcClient
from kuru_backtester.history import full_history as history, index_history, reconstruction_profiles as profiles


@pytest.fixture(scope="module")
def captured():
    path = Path(__file__).parent / "fixtures/legacy_reconstruction.json.gz"
    return json.loads(gzip.decompress(path.read_bytes()))


class RecordedRpc(RpcClient):
    def __init__(self, records):
        super().__init__("recorded://no-network")
        self.records = records

    def call(self, method, params):
        return copy.deepcopy(self.records[json.dumps([method, params], sort_keys=True)])

    def batch(self, calls):
        return [self.call(method, params) for method, params in calls]


@pytest.mark.parametrize("index", [0, 1, 2])
def test_real_archive_parity_and_exact_l3_checkpoint(captured, tmp_path, monkeypatch, index):
    sample = captured["intervals"][index]
    expected = sample["summary"]
    rpc = RecordedRpc(captured["rpc_responses"])
    monkeypatch.setattr(index_history, "RpcClient", lambda *args, **kwargs: rpc)
    (tmp_path / "events.jsonl").write_text(sample["events_jsonl"], encoding="utf-8")
    bounds = expected["interval"]
    progress = history.Progress(tmp_path / "progress.json", bounds)
    result = history.validate_partition(tmp_path, MARKET_ADDRESS, bounds["from_block"], bounds["to_block"], rpc, progress)
    assert result == expected
    assert result["validation"]["final_l3_checkpoint_match"]
    assert result["validation"]["queue_invariant_checks"] == 7
    assert result["cancellation_summary_audit"]["status"] == "pass"
    before = (tmp_path / "summary.json").read_bytes()
    history.validate_partition(tmp_path, MARKET_ADDRESS, bounds["from_block"], bounds["to_block"], rpc, progress)
    assert (tmp_path / "summary.json").read_bytes() == before
    if bounds["from_block"] != history.DEPLOYMENT_BLOCK:
        # Reuse the real captured start book, including legacy head metadata.
        # The hash-checked preceding-partition handoff is covered separately.
        previous = history.VerifiedBoundary(
            (tmp_path / "checkpoint.json").read_text(),
            expected["boundary_block_hashes"][str(bounds["from_block"] - 1)],
            expected["contract_guards"][0]["implementation_code_sha256"],
        )
        monkeypatch.setattr(history, "starting_checkpoint", lambda *a: pytest.fail("Must reuse checked boundary"))
        history.validate_partition(tmp_path, MARKET_ADDRESS, bounds["from_block"], bounds["to_block"],
                                   rpc, progress, previous=previous)
        assert progress.value["checkpoint_reused"] is True
        assert (tmp_path / "summary.json").read_bytes() == before


def event(name, **args):
    return l3.ReplayEvent(1, 0, 0, "tx", name, args, {})


@pytest.mark.parametrize("implementation", sorted(profiles.RECIPROCAL_LINK_IMPLEMENTATIONS))
def test_partial_flip_reciprocal_links_full_fill_and_return_leg(implementation):
    book = l3.L3Book()
    owner = "0x" + "11" * 20
    book.append(1, owner, 200, 100, True, flipped_price=120)
    def apply(name, **args):
        profiles.apply_event(book, event(name, **args), 100, implementation)
        book.check_invariants()
    apply("FlippedOrderCreated", order_id=2, owner=owner, size=50, price=120,
          is_buy=False, flipped_price=100, flipped_id=1)
    apply("Trade", order_id=1, updated_size=150, filled_size=50, price=10**18, is_buy=False)
    assert book.orders[1].flipped_id == 2
    assert book.orders[2].flipped_id == 1
    apply("FlipOrderUpdated", order_id=2, size=200)
    apply("Trade", order_id=1, updated_size=0, filled_size=150, price=10**18, is_buy=False)
    assert book.orders[2].flipped_id == 0
    apply("FlippedOrderCreated", order_id=3, owner=owner, size=240, price=100,
          is_buy=True, flipped_price=120, flipped_id=0)
    apply("Trade", order_id=2, updated_size=0, filled_size=200, price=12*10**17, is_buy=True)
    assert set(book.orders) == {3}
    assert book.orders[3].size == 240


def test_current_engine_path_is_unchanged_and_unknown_profiles_fail(monkeypatch):
    calls = []
    monkeypatch.setattr(l3, "apply_event", lambda *args: calls.append(args))
    book, ev = l3.L3Book(), event("OrdersCanceled")
    profiles.apply_event(book, ev, 100, profiles.CURRENT_IMPLEMENTATION)
    assert calls == [(book, ev, 100)]
    with pytest.raises(ReconstructionError, match="Unsupported"):
        profiles.apply_event(book, ev, 100, "unknown")


def test_arbitrary_start_uses_legacy_profile_from_validated_partition(captured, tmp_path, monkeypatch):
    from kuru_backtester.history.historical_system import ReconstructionEngine, StoredCheckpoint, ValidationLayer
    sample = captured["intervals"][2]
    summary = sample["summary"]
    bounds = summary["interval"]
    rpc = RecordedRpc(captured["rpc_responses"])
    monkeypatch.setattr(index_history, "RpcClient", lambda *args, **kwargs: rpc)
    path = tmp_path / "checkpoint.json"
    digest = index_history.capture_checkpoint(rpc.url, MARKET_ADDRESS, bounds["from_block"] - 1, path)
    payload = json.loads(path.read_text())
    checkpoint = StoredCheckpoint(bounds["from_block"] - 1, payload["checkpoint_timestamp"], path, digest)
    decoded = [l3.decode_event(json.loads(line)) for line in sample["events_jsonl"].splitlines()]
    events = SimpleNamespace(
        require_coverage=lambda *args: None,
        timestamps=lambda *args: rpc.block_timestamps({e.block_number for e in decoded}),
        selected_partitions=lambda *args: [SimpleNamespace(**bounds)],
        validation=SimpleNamespace(verify_partition=lambda *args: summary),
        iter_events=lambda *args: iter(decoded),
    )
    checkpoints = SimpleNamespace(latest_at_or_before=lambda *args: checkpoint, validation=ValidationLayer())
    state = ReconstructionEngine(events, checkpoints).reconstruct_start(MARKET_ADDRESS, bounds["to_block"] + 1)
    official, _, _ = l3.load_checkpoint(rpc, MARKET_ADDRESS, bounds["to_block"])
    assert state.book.orders == official.orders
    assert state.book.levels == official.levels


def test_legacy_adapter_rejects_inconsistent_pair():
    book = l3.L3Book()
    ev = event("FlippedOrderCreated", order_id=2, flipped_id=999, owner=MARKET_ADDRESS,
               size=100, price=120, flipped_price=100, is_buy=False)
    with pytest.raises(ReconstructionError, match="inconsistent source"):
        profiles.apply_event(book, ev, 100, profiles.DEPLOYMENT_IMPLEMENTATION)
    assert not book.orders


def deployment_logs(captured):
    return next(value for key, value in captured["rpc_responses"].items()
                if json.loads(key)[0] == "eth_getLogs" and len(value) == 2
                and int(value[0]["blockNumber"], 16) == history.DEPLOYMENT_BLOCK)


def test_deployment_admin_events_are_exact_and_not_a_general_exclusion(captured):
    logs = copy.deepcopy(deployment_logs(captured))
    canonical, administrative = history.separate_deployment_logs(logs, MARKET_ADDRESS)
    assert canonical == [] and len(administrative) == 2
    for malformed in (logs[:1], logs + logs[:1], list(reversed(logs))):
        with pytest.raises(ReconstructionError):
            history.separate_deployment_logs(malformed, MARKET_ADDRESS)
    bad = copy.deepcopy(logs)
    bad[1]["data"] = "0x" + f"{2:064x}"
    with pytest.raises(ReconstructionError, match="payload"):
        history.separate_deployment_logs(bad, MARKET_ADDRESS)
    for field, value in (("blockNumber", hex(history.DEPLOYMENT_BLOCK + 1)),
                         ("transactionHash", "0x" + "99"*32),
                         ("address", "0x" + "11"*20)):
        changed = [{**log, field: value} for log in logs]
        assert history.separate_deployment_logs(changed, MARKET_ADDRESS) == (changed, [])


def test_old_contract_guard_still_rejects_unknown_code_and_active_vault(captured):
    rpc = RecordedRpc(captured["rpc_responses"])
    assert history.contract_guard(rpc, MARKET_ADDRESS, history.DEPLOYMENT_BLOCK)["implementation_code_sha256"] == profiles.DEPLOYMENT_IMPLEMENTATION
    records = copy.deepcopy(captured["rpc_responses"])
    for key in records:
        if json.loads(key)[0] == "eth_getCode":
            records[key] = "0x6000"
    with pytest.raises(ReconstructionError, match="Unsupported contract"):
        history.contract_guard(RecordedRpc(records), MARKET_ADDRESS, history.DEPLOYMENT_BLOCK)
    rpc.contract_call = lambda *args: "0x" + encode(
        ["address", "uint256", "uint96", "uint256", "uint96", "uint96", "uint96", "uint96"],
        [MARKET_ADDRESS, 0, 0, 0, 0, 1, 0, 30]).hex()
    with pytest.raises(ReconstructionError, match="Active vault"):
        history.contract_guard(rpc, MARKET_ADDRESS, history.DEPLOYMENT_BLOCK)
