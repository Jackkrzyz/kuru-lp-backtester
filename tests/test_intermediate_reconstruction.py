"""Real-chain and actual-runtime regression for the 70-million-block version."""
import gzip
import hashlib
import json
from pathlib import Path

import pytest

from kuru_backtester.engine import l3_vertical_slice as l3
from kuru_backtester.engine.rebuild_from_chain import MARKET_ADDRESS
from kuru_backtester.history import full_history as history, index_history, reconstruction_profiles as profiles
from kuru_backtester.history.historical_system import deserialize_book
from test_legacy_reconstruction import RecordedRpc
from test_lp_contract_evm import ContractMarket, support


@pytest.fixture(scope="module")
def intermediate():
    payload = json.loads(gzip.decompress((Path(__file__).parent / "fixtures/intermediate_reconstruction.json.gz").read_bytes()))
    runtime = payload["runtime"]
    assert runtime["address"] == profiles.INTERMEDIATE_IMPLEMENTATION_ADDRESS
    assert runtime["sha256"] == profiles.INTERMEDIATE_IMPLEMENTATION
    assert hashlib.sha256(bytes.fromhex(runtime["code"][2:])).hexdigest() == profiles.INTERMEDIATE_IMPLEMENTATION
    return payload


def test_captured_envio_rpc_parity_and_complete_l3(intermediate, tmp_path, monkeypatch):
    rpc = RecordedRpc(intermediate["rpc_responses"])
    monkeypatch.setattr(index_history, "RpcClient", lambda *args, **kwargs: rpc)
    sample = intermediate["intervals"][0]
    expected = sample["summary"]
    (tmp_path / "events.jsonl").write_text(sample["events_jsonl"], encoding="utf-8")
    bounds = expected["interval"]
    progress = history.Progress(tmp_path / "progress.json", bounds)
    assert history.contract_guard(rpc, MARKET_ADDRESS, bounds["from_block"] - 1)["implementation_code_sha256"] == profiles.INTERMEDIATE_IMPLEMENTATION
    for _ in range(2):
        result = history.validate_partition(tmp_path, MARKET_ADDRESS, bounds["from_block"], bounds["to_block"], rpc, progress)
        assert result == expected
        assert result["validation"]["final_l3_checkpoint_match"]
        assert result["validation"]["queue_invariant_checks"] == 7


def test_actual_intermediate_runtime_flip_links_and_queue_tail(intermediate, support):
    # Execute the captured bytes unchanged. No source approximation or network.
    market = ContractMarket(support, runtime=intermediate["runtime"]["code"])
    book = l3.L3Book()
    names = []

    def apply(signature, values, sender=None):
        receipt = market.tx(market.market, signature, values, sender)
        for raw in receipt["logs"]:
            if raw["address"].lower() != market.market.lower():
                continue
            event = l3.decode_event({
                "address": raw["address"], "block_number": receipt["block_number"],
                "transaction_index": 0, "log_index": raw["log_index"],
                "transaction_hash": receipt["transaction_hash"],
                "topic0": raw["topics"][0], "data": raw["data"],
            })
            names.append(event.name)
            profiles.apply_event(book, event, 100, profiles.INTERMEDIATE_IMPLEMENTATION)
        book.check_invariants()
        for order in book.orders.values():
            assert market.order(order.order_id) == (
                order.owner, order.size, profiles.head_metadata(book).get(order.order_id, order.prev), order.next, order.flipped_id,
                order.price, order.flipped_price, order.is_buy,
            )
        # Fully filled normal orders can retain all their old storage fields.
        # Compare reachable active queues, not every historical mapping slot.
        for (is_buy, price), queue in book.levels.items():
            getter = "s_buyPricePoints(uint256)" if is_buy else "s_sellPricePoints(uint256)"
            assert market.call(market.market, getter, [price], ["uint40", "uint40"]) == (queue.head, queue.tail)
            active = []
            current = queue.head
            while current:
                assert current not in active
                active.append(current)
                current = market.order(current)[3]
            assert active == book.queue(is_buy, price)

    class EvmRpc:
        def call(self, method, params):
            assert method == "eth_call"
            return market.evm.call({"from": market.maker, **params[0]}, block_number=int(params[1], 16))

        def batch(self, calls):
            return [self.call(method, params) for method, params in calls]

    def check_checkpoint():
        block = market.evm.get_block_by_number("latest")["number"]
        official, _, _ = profiles.load_checkpoint(EvmRpc(), market.market, block, profiles.INTERMEDIATE_IMPLEMENTATION)
        expected = history.checkpoint_payload(market.market, block, 0, {}, book)
        assert history.checkpoint_payload(market.market, block, 0, {}, official) == expected
        restored = deserialize_book(json.loads(json.dumps(expected["book"])))
        assert restored.orders == book.orders and restored.levels == book.levels
        assert profiles.head_metadata(restored) == profiles.head_metadata(book)

    apply("addFlipBuyOrder(uint32,uint32,uint96,bool)", [100, 120, 200, False])
    apply("addBuyOrder(uint32,uint96,bool)", [100, 100, False])
    for size in (50, 25, 125):
        apply("placeAndExecuteMarketSell(uint96,uint256,bool,bool)", [size, 0, True, False], market.taker)
    assert book.orders[3].flipped_id == 0
    apply("placeAndExecuteMarketBuy(uint96,uint256,bool,bool)", [240, 0, True, False], market.taker)
    assert book.orders[4].prev == 2  # The returning bid must join behind the older bid.
    apply("placeAndExecuteMarketSell(uint96,uint256,bool,bool)", [125, 0, True, False], market.taker)
    assert book.orders[4].flipped_id == 5 and book.orders[5].flipped_id == 4
    check_checkpoint()
    apply("addBuyOrder(uint32,uint96,bool)", [100, 100, False])
    apply("batchCancelFlipOrders(uint40[])", [[4]])
    assert set(book.orders) == {6}
    check_checkpoint()
    apply("batchCancelOrders(uint40[])", [[6]])
    assert not book.orders
    assert {"FlipOrderCreated", "FlippedOrderCreated", "FlipOrderUpdated", "Trade", "OrderCanceled", "FlipOrdersCanceled"}.issubset(names)


@pytest.mark.parametrize("values", [[[999, 2]], [[1, 1]], [[1, 0]], [[1, 2]], [[1, 2], [1, 3]]])
def test_head_metadata_rejects_active_missing_zero_and_duplicate_predecessors(values):
    book = l3.L3Book()
    book.append(1, MARKET_ADDRESS, 100, 100, True)
    with pytest.raises(l3.ReconstructionError):
        profiles.restore_head_metadata(book, values)
