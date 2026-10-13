"""Summary audit accepts only proved, reciprocal flip cancellations."""
import copy
import gzip
import json
from pathlib import Path

from kuru_backtester.engine.rebuild_from_chain import RpcClient, MARKET_ADDRESS
from kuru_backtester.engine.l3_vertical_slice import ReplayEvent
from kuru_backtester.history import full_history as history, index_history
from kuru_backtester.history.reconstruction_validation_report import cancellation_summary_audit


def events(summary="FlipOrdersCanceled"):
    return [ReplayEvent(10, 0, i, "tx", name, args, {}) for i, (name, args) in enumerate([
        ("OrderCanceled", {"order_id": 2}), ("OrderCanceled", {"order_id": 1}),
        (summary, {"order_ids": [1]})])]


def test_linked_partner_cancellation_is_not_a_duplicate_summary_mutation():
    audit = cancellation_summary_audit(events(), flip_pairs={("tx", 1): 2, ("tx", 2): 1})
    assert audit["status"] == "pass"
    assert audit["individual_order_cancellation_count"] == 2
    assert audit["summary_referenced_nonzero_order_ids"] == 1
    assert audit["individual_cancellations_not_in_summary"] == 1
    assert audit["verified_paired_flip_cancellations_not_in_summary"] == 1
    assert audit["summary_order_ids_applied_as_cancellations"] == 0


def test_unproven_or_wrong_transaction_pair_still_fails():
    for evidence in ({}, {("tx", 2): 1}, {("other", 1): 2, ("other", 2): 1},
                     {("tx", 1): 3, ("tx", 2): 1}):
        assert cancellation_summary_audit(events(), flip_pairs=evidence)["status"] == "fail"


def test_regular_summary_missing_partner_and_duplicate_individual_still_fail():
    evidence = {("tx", 1): 2, ("tx", 2): 1}
    assert cancellation_summary_audit(events("OrdersCanceled"), flip_pairs=evidence)["status"] == "fail"
    assert cancellation_summary_audit(events()[1:], flip_pairs=evidence)["status"] == "pass"
    assert cancellation_summary_audit(events()[:1] + events()[2:], flip_pairs=evidence)["status"] == "fail"
    assert cancellation_summary_audit(events() + events()[:1], flip_pairs=evidence)["status"] == "fail"


def test_real_failed_partition_now_matches_rpc_l2_and_final_l3(tmp_path, monkeypatch):
    fixture = json.loads(gzip.decompress((Path(__file__).parent / "fixtures/paired_cancellation.json.gz").read_bytes()))
    class RecordedRpc(RpcClient):
        def call(self, method, params):
            return copy.deepcopy(fixture["rpc_responses"][json.dumps([method, params], sort_keys=True)])
        def batch(self, calls):
            return [self.call(method, params) for method, params in calls]
    rpc = RecordedRpc("recorded://no-network")
    monkeypatch.setattr(index_history, "RpcClient", lambda *args, **kwargs: rpc)
    sample = fixture["intervals"][0]
    (tmp_path / "events.jsonl").write_text(sample["events_jsonl"])
    result = history.validate_partition(tmp_path, MARKET_ADDRESS, 43960999, 43965998, rpc,
                                        history.Progress(tmp_path / "progress.json", sample["summary"]["interval"]))
    assert result == sample["summary"]
    assert result["event_parity"]["envio_event_count"] == result["event_parity"]["rpc_event_count"] == 33
    assert result["validation"]["queue_invariant_checks"] == 7
    assert result["validation"]["final_l3_checkpoint_match"] is True
    assert result["cancellation_summary_audit"]["verified_paired_flip_cancellations_not_in_summary"] == 1
