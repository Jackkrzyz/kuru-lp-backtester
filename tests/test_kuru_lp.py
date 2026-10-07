import json
from decimal import Decimal
from pathlib import Path

import kuru_lp as lp
import l3_vertical_slice as l3
import strategy_vertical_slice as mm


FIXTURE = Path("sdk_reference/fixtures/concentrated_liquidity_v0_0_95.json")


def test_python_position_builder_matches_official_sdk_fixtures_exactly():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    values = {key: int(value) for key, value in payload["inputs"].items()}
    for shape in ("spot", "curve", "bid_ask"):
        actual = lp.generate_distribution(
            shape=shape,
            fee_pps=values["minFeesPps"],
            start_price=values["startPrice"],
            end_price=values["endPrice"],
            best_ask_price=values["bestAskPrice"],
            price_precision=values["pricePrecision"],
            size_precision=values["sizePrecision"],
            quote_decimals=values["quoteAssetDecimals"],
            base_decimals=values["baseAssetDecimals"],
            tick_size=values["tickSize"],
            min_size=values["minSize"],
            quote_liquidity=values["quoteLiquidity"],
            base_liquidity=values["baseLiquidity"] if shape == "spot" else None,
        )
        expected = lp.fixture_distribution(FIXTURE, shape)
        assert actual == expected


def test_initial_lp_orders_activate_at_historical_queue_tail():
    book = l3.L3Book()
    book.append(1, "0x01", 300, 100, True, 0, 0)
    distribution = lp.LPDistribution(
        bids=(lp.LPPosition("buy", 100, 200, 110),),
        asks=(lp.LPPosition("sell", 120, 200, 110),),
        quote_liquidity=2,
        base_liquidity=200,
        min_size_error=False,
    )
    state = lp.LPState(Decimal("1000"), Decimal("1000"))
    lp.initial_orders(distribution, state, 9, 1, 100, 100)
    activations = lp.activate_orders(book, state, 10, 100)
    buy = next(item for item in state.orders.values() if item.side == "buy")
    assert buy.queue_ahead_ids == [1]
    assert buy.queue_ahead_raw == 300
    assert len(activations) == 2


def test_full_fill_creates_paired_opposite_order_without_cancel_replace():
    state = lp.LPState(Decimal("10"), Decimal("0"))
    order = lp.LPOrder("lp-1", "buy", 100, 110, 200, 200, "active", 10, 9)
    lp._reserve(state, order, 100, 100)
    state.orders[order.order_id] = order
    state.next_order_number = 2
    event = l3.ReplayEvent(
        block_number=10,
        transaction_index=1,
        log_index=2,
        transaction_hash="0xabc",
        name="Trade",
        args={
            "filled_size": 200,
            "updated_size": 0,
            "price": 100 * 10**18 // 100,
            "is_buy": False,
            "order_id": 7,
        },
        raw={},
    )
    config = lp.LPConfig("spot", 90, 120, 3000, 1_000_000, 0)
    budget = mm.TransactionFillBudget("0xabc", 200, 200, 200)
    fills, placements = lp.apply_fills(event, state, config, 100, 100, budget, 105)
    flip = state.orders["lp-2"]
    assert len(fills) == 1
    assert placements[0]["kind"] == "flip_create"
    assert flip.side == "sell"
    assert flip.price_raw == 110
    assert flip.flip_price_raw == 100
    assert flip.eligible_block == 10
    assert state.placements == 1
    assert all(item.status != "cancelled" for item in state.orders.values())
    book = l3.L3Book()
    book.append(99, "0x01", 300, 110, False, 0, 0)
    lp.activate_orders(book, state, 10, 100)
    assert flip.queue_ahead_ids == [99]
    assert flip.queue_ahead_raw == 300


def test_one_transaction_budget_is_shared_by_multiple_lp_orders():
    state = lp.LPState(Decimal("20"), Decimal("0"))
    for index, raw_size in enumerate((100, 100), 1):
        order = lp.LPOrder(f"lp-{index}", "buy", 100, 110, raw_size, raw_size, "active", 10, 9)
        lp._reserve(state, order, 100, 100)
        state.orders[order.order_id] = order
    state.next_order_number = 3
    event = l3.ReplayEvent(10, 1, 2, "0xabc", "Trade", {
        "filled_size": 200, "updated_size": 0, "price": 10**18,
        "is_buy": False, "order_id": 7,
    }, {})
    config = lp.LPConfig("spot", 90, 120, 3000, 1_000_000, 0,
                         max_transaction_participation_percent=Decimal("25"))
    budget = mm.TransactionFillBudget("0xabc", 200, 50, 50)
    fills, _ = lp.apply_fills(event, state, config, 100, 100, budget, 105)
    assert sum(item["simulated_fill_size_raw"] for item in fills) == 50
    assert budget.remaining_raw == 0


def test_more_than_ten_same_price_orders_keep_numeric_fifo_order():
    positions = tuple(lp.LPPosition("buy", 100, 100, 110) for _ in range(12))
    distribution = lp.LPDistribution(
        bids=positions,
        asks=(),
        quote_liquidity=12,
        base_liquidity=0,
        min_size_error=False,
    )
    state = lp.LPState(Decimal("100"), Decimal("0"))
    lp.initial_orders(distribution, state, 9, 1, 100, 100)
    lp.activate_orders(l3.L3Book(), state, 10, 100)
    event = l3.ReplayEvent(10, 1, 2, "0xabc", "Trade", {
        "filled_size": 250,
        "updated_size": 0,
        "price": 10**18,
        "is_buy": False,
        "order_id": 7,
    }, {})
    config = lp.LPConfig("spot", 90, 120, 3000, 10_000_000, 0)
    fills, _ = lp.apply_fills(event, state, config, 100, 100, None, 105)
    assert [item["strategy_order_id"] for item in fills] == ["lp-1", "lp-2", "lp-3"]
    assert [item["simulated_fill_size_raw"] for item in fills] == [100, 100, 50]
    assert state.orders["lp-10"].remaining_raw == 100


def test_partial_fill_immediately_creates_and_then_updates_one_paired_flip():
    state = lp.LPState(Decimal("10"), Decimal("0"))
    order = lp.LPOrder("lp-1", "buy", 100, 110, 200, 200, "active", 10, 9)
    lp._reserve(state, order, 100, 100)
    state.orders[order.order_id] = order
    state.next_order_number = 2
    event = l3.ReplayEvent(10, 1, 2, "0xabc", "Trade", {
        "filled_size": 50, "updated_size": 0, "price": 10**18,
        "is_buy": False, "order_id": 7,
    }, {})
    config = lp.LPConfig("spot", 90, 120, 3000, 1_000_000, 0)
    fills, placements = lp.apply_fills(event, state, config, 100, 100, None, 105)
    assert fills[0]["simulated_fill_size_raw"] == 50
    assert placements[0]["kind"] == "flip_create"
    assert order.remaining_raw == 150
    flip = state.orders["lp-2"]
    assert flip.remaining_raw == 50
    assert order.paired_order_id == flip.order_id
    assert flip.paired_order_id == order.order_id

    second = l3.ReplayEvent(11, 1, 2, "0xdef", "Trade", {
        "filled_size": 25, "updated_size": 0, "price": 10**18,
        "is_buy": False, "order_id": 8,
    }, {})
    _, updates = lp.apply_fills(second, state, config, 100, 100, None, 105)
    assert updates[0]["kind"] == "flip_update"
    assert len(state.orders) == 2
    assert flip.remaining_raw == 75


def test_subminimum_partial_flips_use_one_contract_internal_accumulator():
    state = lp.LPState(Decimal("10"), Decimal("0"))
    order = lp.LPOrder("lp-1", "buy", 100, 110, 200, 200, "active", 10, 9)
    lp._reserve(state, order, 100, 100)
    state.orders[order.order_id] = order
    state.next_order_number = 2
    config = lp.LPConfig(
        "spot", 90, 120, 3000, 1_000_000, 0, min_size_raw=100,
    )
    first = l3.ReplayEvent(10, 1, 2, "0xabc", "Trade", {
        "filled_size": 25, "updated_size": 0, "price": 10**18,
        "is_buy": False, "order_id": 7,
    }, {})
    _, first_placements = lp.apply_fills(first, state, config, 100, 100, None, 105)
    flip = state.orders["lp-2"]
    assert first_placements[0]["kind"] == "flip_create"
    assert first_placements[0]["contract_internal_flip"] is True
    assert first_placements[0]["below_market_minimum"] is True
    assert flip.remaining_raw == 25

    second = l3.ReplayEvent(11, 1, 2, "0xdef", "Trade", {
        "filled_size": 75, "updated_size": 0, "price": 10**18,
        "is_buy": False, "order_id": 8,
    }, {})
    _, updates = lp.apply_fills(second, state, config, 100, 100, None, 105)
    assert len(state.orders) == 2
    assert updates[0]["kind"] == "flip_update"
    assert updates[0]["below_market_minimum"] is False
    assert flip.remaining_raw == 100
    lp.assert_flip_minimum_semantics(state, config.min_size_raw)


def test_initial_order_below_minimum_is_never_treated_as_an_internal_flip():
    state = lp.LPState(Decimal("10"), Decimal("10"))
    state.orders["lp-1"] = lp.LPOrder("lp-1", "buy", 100, 110, 99, 99, "pending", 10, 9)
    try:
        lp.assert_flip_minimum_semantics(state, 100)
        assert False, "a small initial order must fail"
    except RuntimeError as error:
        assert "initial LP order" in str(error)


def test_sell_fill_uses_contract_price_ratio_for_paired_bid_size():
    state = lp.LPState(Decimal("0"), Decimal("2"))
    order = lp.LPOrder("lp-1", "sell", 120, 100, 100, 100, "active", 10, 9)
    lp._reserve(state, order, 100, 100)
    state.orders[order.order_id] = order
    state.next_order_number = 2
    event = l3.ReplayEvent(10, 1, 2, "0xabc", "Trade", {
        "filled_size": 50, "updated_size": 0, "price": 120 * 10**18 // 100,
        "is_buy": True, "order_id": 7,
    }, {})
    config = lp.LPConfig("spot", 90, 130, 3000, 0, 2 * 10**18)
    lp.apply_fills(event, state, config, 100, 100, None, 110)
    assert state.orders["lp-2"].side == "buy"
    assert state.orders["lp-2"].remaining_raw == 60


def test_sdk_reference_is_pinned():
    package = json.loads(Path("sdk_reference/package.json").read_text(encoding="utf-8"))
    assert package["dependencies"]["@kuru-labs/kuru-sdk"] == "0.0.95"
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert fixture["sdk"]["npm_git_head"] == "c6a1e9eced12d634fa879ec46c52baff8a50f3c3"


def test_initial_position_rejects_insufficient_mon_or_usdc_deposit():
    distribution = lp.LPDistribution(
        bids=(lp.LPPosition("buy", 100, 200, 110),),
        asks=(lp.LPPosition("sell", 120, 200, 110),),
        quote_liquidity=2,
        base_liquidity=200,
        min_size_error=False,
    )
    low_cash = lp.LPState(Decimal("1"), Decimal("10"))
    try:
        lp.initial_orders(distribution, low_cash, 9, 1, 100, 100)
        assert False, "insufficient USDC must fail"
    except RuntimeError as error:
        assert "USDC" in str(error)

    low_mon = lp.LPState(Decimal("10"), Decimal("1"))
    try:
        lp.initial_orders(distribution, low_mon, 9, 1, 100, 100)
        assert False, "insufficient MON must fail"
    except RuntimeError as error:
        assert "MON" in str(error)


def test_batch_action_cost_is_charged_once_and_flip_orders_are_automatic():
    config = lp.LPConfig(
        "spot", 90, 120, 3000, 10_000_000, 100 * 10**18,
        fixed_action_fee=Decimal("0.25"),
    )
    state = lp.LPState(Decimal("10"), Decimal("100"))
    lp.charge_initial_action_fee(state, config)
    assert state.cash == Decimal("9.75")
    assert state.action_fees == Decimal("0.25")
    # An internal flip update does not call the fee function.
    assert state.action_fees == Decimal("0.25")
