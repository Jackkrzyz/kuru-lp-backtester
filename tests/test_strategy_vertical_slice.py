"""Deterministic causality, queue, capital, and accounting tests."""

import hashlib
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from l3_vertical_slice import L3Book, ReplayEvent
from strategy_vertical_slice import (
    StrategyConfig,
    StrategyOrder,
    StrategyState,
    TransactionFillBudget,
    activate_orders,
    apply_strategy_fills,
    assert_capital_invariants,
    available_cash,
    build_transaction_impacts,
    cancel_and_place_quotes,
    maker_fill_accounting,
    load_verified_source_events,
    marked_value,
    maximum_drawdown,
    order_is_allowed,
    pnl_breakdown,
    should_replace,
    strategy_result_decomposition,
    target_quote_prices,
    transaction_fill_budgets,
    update_queue_ahead,
    write_outputs,
)


OWNER = "0x0000000000000000000000000000000000000123"
PRICE_PRECISION = 100
SIZE_PRECISION = 10


def config(**overrides: object) -> StrategyConfig:
    values: dict[str, object] = {
        "starting_cash": Decimal("100000"),
        "starting_inventory": Decimal("1000"),
        "bid_offset_bps": Decimal("100"),
        "ask_offset_bps": Decimal("100"),
        "order_size": Decimal("20"),
        "minimum_inventory": Decimal("0"),
        "maximum_inventory": Decimal("2000"),
        "cancel_replace": True,
        "replace_threshold_bps": Decimal("1"),
        "maker_fee_bps": Decimal("0"),
        "fixed_action_fee": Decimal("0"),
        "inclusion_delay_blocks": 1,
        "material_impact_threshold_percent": Decimal("25"),
        "max_transaction_participation_percent": None,
    }
    values.update(overrides)
    return StrategyConfig(**values)  # type: ignore[arg-type]


def make_book() -> L3Book:
    book = L3Book()
    book.append(1, OWNER, 100, 9_900, True)
    book.append(2, OWNER, 100, 10_100, False)
    return book


class VerifiedEventInputTests(unittest.TestCase):
    def test_loads_timestamped_events_in_raw_replay_order(self) -> None:
        logs = [
            {
                "address": "0x065c9d28e428a0db40191a54d33d5b7c71a9c394",
                "block_number": 10,
                "block_timestamp": 1_700_000_000,
                "transaction_hash": "0x01",
                "transaction_index": 1,
                "log_index": 2,
                "data": "0x",
                "topic0": "0x" + "11" * 32,
                "topic1": None,
                "topic2": None,
                "topic3": None,
            }
        ]
        with tempfile.TemporaryDirectory() as directory:
            summary_path = Path(directory) / "summary.json"
            event_path = Path(directory) / "source_events.jsonl"
            event_path.write_text(
                "".join(json.dumps(log) + "\n" for log in logs),
                encoding="utf-8",
            )

            result = load_verified_source_events(
                summary_path,
                {"event_input": {"path": event_path.name, "record_count": 1}},
            )

        self.assertIsNotNone(result)
        events, timestamps = result or ([], {})
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].block_number, 10)
        self.assertEqual(timestamps, {10: 1_700_000_000})

    def test_decoded_cache_round_trip_and_source_hash_invalidation(self) -> None:
        log = {
            "address": "0x065c9d28e428a0db40191a54d33d5b7c71a9c394",
            "block_number": 10,
            "block_timestamp": 1_700_000_000,
            "transaction_hash": "0x01",
            "transaction_index": 1,
            "log_index": 2,
            "data": "0x",
            "topic0": "0x" + "11" * 32,
            "topic1": None,
            "topic2": None,
            "topic3": None,
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            summary_path = root / "summary.json"
            event_path = root / "source_events.jsonl"
            event_path.write_text(json.dumps(log) + "\n", encoding="utf-8")
            source_hash = hashlib.sha256(event_path.read_bytes()).hexdigest()
            cache_path = root / "decoded.sqlite3"
            validation = {"event_input": {"path": event_path.name, "record_count": 1}}
            first_metrics = {}
            first = load_verified_source_events(
                summary_path,
                validation,
                first_metrics,
                source_sha256=source_hash,
                decoded_cache_path=cache_path,
            )
            second_metrics = {}
            second = load_verified_source_events(
                summary_path,
                validation,
                second_metrics,
                source_sha256=source_hash,
                decoded_cache_path=cache_path,
            )
            stale_metrics = {}
            stale = load_verified_source_events(
                summary_path,
                validation,
                stale_metrics,
                source_sha256="0" * 64,
                decoded_cache_path=cache_path,
            )

        self.assertEqual(first, second)
        self.assertEqual(first, stale)
        self.assertFalse(first_metrics["decoded_event_cache_hit"])
        self.assertTrue(second_metrics["decoded_event_cache_hit"])
        self.assertTrue(stale_metrics["decoded_event_cache_rebuilt"])


def replay_event(
    name: str,
    *,
    block: int,
    log: int,
    order_id: int,
    is_buy: bool = False,
    price_raw: int = 9_900,
    filled_size: int = 100,
    updated_size: int = 0,
    size: int = 100,
    transaction_hash: str | None = None,
) -> ReplayEvent:
    args: dict[str, object]
    if name == "Trade":
        args = {
            "order_id": order_id,
            "is_buy": is_buy,
            "price": price_raw * 10**18 // PRICE_PRECISION,
            "filled_size": filled_size,
            "updated_size": updated_size,
        }
    elif name == "OrderCanceled":
        args = {
            "order_id": order_id,
            "owner": OWNER,
            "size": size,
            "price": price_raw,
            "is_buy": is_buy,
        }
    else:
        args = {"order_id": order_id}
    return ReplayEvent(
        block_number=block,
        transaction_index=0,
        log_index=log,
        transaction_hash=transaction_hash or f"0x{block:062x}{log:02x}",
        name=name,
        args=args,
        raw={},
    )


def make_state(cash: str = "100000", inventory: str = "1000") -> StrategyState:
    return StrategyState(cash=Decimal(cash), inventory=Decimal(inventory))


def impact_fill(
    event: ReplayEvent,
    size_raw: int,
    strategy_order_id: str,
) -> dict[str, object]:
    return {
        "strategy_order_id": strategy_order_id,
        "transaction_hash": event.transaction_hash,
        "historical_event_block_number": event.block_number,
        "historical_event_transaction_index": event.transaction_index,
        "historical_event_log_index": event.log_index,
        "historical_taker_is_buy": bool(event.args["is_buy"]),
        "simulated_fill_size_raw": size_raw,
    }


def place_quotes(
    book: L3Book,
    state: StrategyState,
    settings: StrategyConfig,
    block: int = 10,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    return cancel_and_place_quotes(
        block_number=block,
        transaction_index=0,
        book=book,
        price_precision=PRICE_PRECISION,
        size_precision=SIZE_PRECISION,
        state=state,
        config=settings,
    )


class CausalityTests(unittest.TestCase):
    def test_decision_after_block_cannot_fill_from_same_block(self) -> None:
        book = make_book()
        state = make_state()
        settings = config()
        place_quotes(book, state, settings, block=10)
        buy = state.active_orders["buy"]

        fills = apply_strategy_fills(
            replay_event("Trade", block=10, log=1, order_id=90),
            state,
            settings,
            PRICE_PRECISION,
            SIZE_PRECISION,
        )

        self.assertEqual(fills, [])
        self.assertEqual(buy.status, "pending")
        self.assertEqual(buy.eligible_block, 11)

    def test_default_and_configurable_inclusion_delay(self) -> None:
        book = make_book()
        state = make_state()
        settings = config(inclusion_delay_blocks=2)
        place_quotes(book, state, settings, block=10)

        self.assertEqual(activate_orders(book, state, 11, SIZE_PRECISION), [])
        activations = activate_orders(book, state, 12, SIZE_PRECISION)

        self.assertEqual(len(activations), 2)
        self.assertTrue(all(order.status == "active" for order in state.active_orders.values()))
        self.assertEqual(config().inclusion_delay_blocks, 1)

    def test_decision_records_the_processed_book_block(self) -> None:
        book = make_book()
        state = make_state()
        settings = config()
        expected = target_quote_prices(book, settings)
        place_quotes(book, state, settings, block=25)

        self.assertEqual(state.active_orders["buy"].price_raw, expected["buy"])
        self.assertEqual(state.active_orders["sell"].price_raw, expected["sell"])
        self.assertEqual(state.active_orders["buy"].decision_book_block, 25)


class QueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.book = make_book()
        self.state = make_state()
        self.settings = config()
        place_quotes(self.book, self.state, self.settings, block=10)
        activate_orders(self.book, self.state, 11, SIZE_PRECISION)

    def test_new_orders_join_the_price_level_tail(self) -> None:
        buy = self.state.active_orders["buy"]
        sell = self.state.active_orders["sell"]

        self.assertEqual(buy.queue_ahead_ids, [1])
        self.assertEqual(buy.queue_ahead_raw, 100)
        self.assertEqual(sell.queue_ahead_ids, [2])

    def test_partial_fills_reduce_queue_ahead_before_strategy_fill(self) -> None:
        event = replay_event(
            "Trade", block=11, log=1, order_id=1, filled_size=40, updated_size=60
        )
        fills = apply_strategy_fills(
            event, self.state, self.settings, PRICE_PRECISION, SIZE_PRECISION
        )
        update_queue_ahead(event, self.state)

        self.assertEqual(fills, [])
        self.assertEqual(self.state.active_orders["buy"].queue_ahead_raw, 60)

    def test_cancellations_ahead_improve_queue_position(self) -> None:
        event = replay_event(
            "OrderCanceled", block=11, log=1, order_id=1, is_buy=True, size=100
        )
        update_queue_ahead(event, self.state)

        self.assertEqual(self.state.active_orders["buy"].queue_ahead_ids, [])
        self.assertEqual(self.state.active_orders["buy"].queue_ahead_raw, 0)

    def test_orders_created_behind_do_not_enter_queue_ahead(self) -> None:
        self.book.append(3, OWNER, 500, 9_900, True)
        buy = self.state.active_orders["buy"]

        self.assertEqual(buy.queue_ahead_ids, [1])
        self.assertNotIn(3, buy.queue_ahead_ids)

    def test_replaced_order_cannot_fill_after_cancel(self) -> None:
        old_id = self.state.active_orders["buy"].strategy_order_id
        self.book.remove(1)
        self.book.append(4, OWNER, 100, 9_700, True)
        place_quotes(self.book, self.state, self.settings, block=11)
        self.assertNotEqual(self.state.active_orders["buy"].strategy_order_id, old_id)

        event = replay_event(
            "Trade", block=12, log=1, order_id=90, price_raw=9_900, filled_size=200
        )
        fills = apply_strategy_fills(
            event, self.state, self.settings, PRICE_PRECISION, SIZE_PRECISION
        )

        self.assertFalse(any(fill["strategy_order_id"] == old_id for fill in fills))

    def test_multiple_trades_partially_fill_once_without_double_counting(self) -> None:
        buy = self.state.active_orders["buy"]
        buy.queue_ahead_ids = []
        buy.queue_ahead_raw = 0
        first = replay_event("Trade", block=11, log=1, order_id=90, filled_size=50)
        second = replay_event("Trade", block=11, log=2, order_id=91, filled_size=150)

        first_fills = apply_strategy_fills(
            first, self.state, self.settings, PRICE_PRECISION, SIZE_PRECISION
        )
        second_fills = apply_strategy_fills(
            second, self.state, self.settings, PRICE_PRECISION, SIZE_PRECISION
        )

        self.assertEqual(first_fills[0]["size"], 5)
        self.assertEqual(second_fills[0]["size"], 15)
        self.assertEqual(self.state.filled_size, Decimal("20"))
        self.assertNotIn("buy", self.state.active_orders)


class CapitalTests(unittest.TestCase):
    def test_bid_reserves_complete_notional_and_positive_fee(self) -> None:
        state = make_state()
        settings = config(maker_fee_bps=Decimal("10"))
        place_quotes(make_book(), state, settings)
        buy = state.active_orders["buy"]
        expected = Decimal("20") * Decimal("99") * Decimal("1.001")

        self.assertEqual(buy.reserved_cash, expected)
        self.assertEqual(state.reserved_cash, expected)

    def test_ask_reserves_complete_inventory(self) -> None:
        state = make_state()
        place_quotes(make_book(), state, config())

        self.assertEqual(state.active_orders["sell"].reserved_inventory, Decimal("20"))
        self.assertEqual(state.reserved_inventory, Decimal("20"))

    def test_active_orders_cannot_reuse_reserved_cash(self) -> None:
        state = make_state(cash="2000")
        settings = config(starting_cash=Decimal("2000"), maker_fee_bps=Decimal("10"))
        place_quotes(make_book(), state, settings)

        self.assertFalse(
            order_is_allowed(
                "buy", state, settings, 9_900, PRICE_PRECISION, SIZE_PRECISION
            )
        )
        self.assertLess(available_cash(state), state.active_orders["buy"].reserved_cash)

    def test_cash_inventory_and_limits_hold_after_fill(self) -> None:
        book = make_book()
        state = make_state()
        settings = config(maximum_inventory=Decimal("1020"))
        place_quotes(book, state, settings, block=10)
        activate_orders(book, state, 11, SIZE_PRECISION)
        buy = state.active_orders["buy"]
        buy.queue_ahead_ids = []
        buy.queue_ahead_raw = 0
        apply_strategy_fills(
            replay_event("Trade", block=11, log=1, order_id=90, filled_size=200),
            state,
            settings,
            PRICE_PRECISION,
            SIZE_PRECISION,
        )

        assert_capital_invariants(state, settings, SIZE_PRECISION)
        self.assertEqual(state.inventory, Decimal("1020"))
        self.assertGreaterEqual(state.cash, state.reserved_cash)

    def test_replacement_releases_old_reservation_before_new_reservation(self) -> None:
        book = make_book()
        state = make_state()
        settings = config(maker_fee_bps=Decimal("10"))
        place_quotes(book, state, settings, block=10)
        old_reserved = state.reserved_cash
        book.remove(1)
        book.append(4, OWNER, 100, 9_700, True)

        place_quotes(book, state, settings, block=11)
        expected = state.active_orders["buy"].reserved_cash

        self.assertEqual(state.reserved_cash, expected)
        self.assertNotEqual(expected, old_reserved)
        assert_capital_invariants(state, settings, SIZE_PRECISION)


class AccountingTests(unittest.TestCase):
    def test_each_fill_conserves_cash_and_inventory(self) -> None:
        cash_delta, inventory_delta, fee, notional = maker_fill_accounting(
            "buy", Decimal("2"), Decimal("5"), Decimal("10")
        )

        self.assertEqual(inventory_delta, Decimal("2"))
        self.assertEqual(cash_delta + notional + fee, Decimal("0"))

    def test_maker_fee_sign_and_quote_denomination(self) -> None:
        buy = maker_fill_accounting("buy", Decimal("2"), Decimal("5"), Decimal("10"))
        sell = maker_fill_accounting("sell", Decimal("2"), Decimal("5"), Decimal("10"))
        rebate = maker_fill_accounting("sell", Decimal("2"), Decimal("5"), Decimal("-10"))

        self.assertEqual(buy[2], Decimal("0.01"))
        self.assertEqual(sell[2], Decimal("0.01"))
        self.assertEqual(rebate[2], Decimal("-0.01"))
        self.assertEqual(rebate[0], Decimal("10.01"))

    def test_fixed_action_fee_is_charged_once_per_action(self) -> None:
        book = make_book()
        state = make_state()
        settings = config(fixed_action_fee=Decimal("0.25"))
        place_quotes(book, state, settings, block=10)
        self.assertEqual(state.action_fees, Decimal("0.50"))

        book.remove(1)
        book.append(4, OWNER, 100, 9_700, True)
        place_quotes(book, state, settings, block=11)

        self.assertEqual(state.cancellations, 2)
        self.assertEqual(state.placements, 4)
        self.assertEqual(state.action_fees, Decimal("1.50"))

    def test_portfolio_value_is_cash_plus_inventory_times_mark(self) -> None:
        state = make_state(cash="100", inventory="2")
        value = marked_value(state, make_book(), PRICE_PRECISION)

        self.assertEqual(value, Decimal("300"))

    def test_net_pnl_identity_includes_applicable_costs(self) -> None:
        result = pnl_breakdown(
            Decimal("100"), Decimal("107"), Decimal("2"), Decimal("1")
        )

        self.assertEqual(result["gross_pnl_before_costs"], Decimal("10"))
        self.assertEqual(result["applicable_costs"], Decimal("3"))
        self.assertEqual(result["net_pnl"], Decimal("7"))

    def test_turnover_comes_only_from_executed_notional(self) -> None:
        book = make_book()
        state = make_state()
        settings = config()
        place_quotes(book, state, settings, block=10)
        self.assertEqual(state.turnover, Decimal("0"))
        activate_orders(book, state, 11, SIZE_PRECISION)
        buy = state.active_orders["buy"]
        buy.queue_ahead_ids = []
        buy.queue_ahead_raw = 0
        apply_strategy_fills(
            replay_event("Trade", block=11, log=1, order_id=90, filled_size=50),
            state,
            settings,
            PRICE_PRECISION,
            SIZE_PRECISION,
        )

        self.assertEqual(state.turnover, Decimal("495"))

    def test_maximum_drawdown_uses_high_water_mark(self) -> None:
        values = [Decimal("100"), Decimal("110"), Decimal("105"), Decimal("108"), Decimal("90")]

        self.assertEqual(maximum_drawdown(values), Decimal("20"))


class CounterfactualAndOutputTests(unittest.TestCase):
    def test_transaction_cap_is_consumed_during_replay_and_remainder_stays_active(self) -> None:
        transaction_hash = "0x" + "12" * 32
        first = replay_event(
            "Trade",
            block=23,
            log=1,
            order_id=41,
            filled_size=100,
            transaction_hash=transaction_hash,
        )
        second = replay_event(
            "Trade",
            block=23,
            log=2,
            order_id=42,
            filled_size=200,
            transaction_hash=transaction_hash,
        )
        settings = config(max_transaction_participation_percent=Decimal("25"))
        budgets = transaction_fill_budgets(
            [first, second], settings.max_transaction_participation_percent
        )
        budget = budgets[transaction_hash]
        self.assertIsInstance(budget, TransactionFillBudget)
        self.assertEqual(budget.maximum_simulated_raw, 75)

        book = make_book()
        state = make_state()
        place_quotes(book, state, settings, block=22)
        activate_orders(book, state, 23, SIZE_PRECISION)
        buy = state.active_orders["buy"]
        buy.queue_ahead_ids = []
        buy.queue_ahead_raw = 0
        first_fills = apply_strategy_fills(
            first,
            state,
            settings,
            PRICE_PRECISION,
            SIZE_PRECISION,
            budget,
            10_000,
        )
        second_fills = apply_strategy_fills(
            second,
            state,
            settings,
            PRICE_PRECISION,
            SIZE_PRECISION,
            budget,
            10_000,
        )

        self.assertEqual(first_fills[0]["simulated_fill_size_raw"], 75)
        self.assertEqual(second_fills, [])
        self.assertEqual(budget.remaining_raw, 0)
        self.assertEqual(state.active_orders["buy"].remaining_raw, 125)

    def test_uncapped_transaction_keeps_existing_fill_behavior(self) -> None:
        self.assertEqual(transaction_fill_budgets([], None), {})
        book = make_book()
        state = make_state()
        settings = config()
        place_quotes(book, state, settings, block=10)
        activate_orders(book, state, 11, SIZE_PRECISION)
        buy = state.active_orders["buy"]
        buy.queue_ahead_ids = []
        buy.queue_ahead_raw = 0
        fills = apply_strategy_fills(
            replay_event("Trade", block=11, log=1, order_id=90, filled_size=300),
            state,
            settings,
            PRICE_PRECISION,
            SIZE_PRECISION,
        )

        self.assertEqual(fills[0]["simulated_fill_size_raw"], 200)

    def test_fill_keeps_event_percentage_as_diagnostic(self) -> None:
        book = make_book()
        state = make_state()
        settings = config(material_impact_threshold_percent=Decimal("25"))
        place_quotes(book, state, settings, block=10)
        activate_orders(book, state, 11, SIZE_PRECISION)
        buy = state.active_orders["buy"]
        buy.queue_ahead_ids = []
        buy.queue_ahead_raw = 0

        fills = apply_strategy_fills(
            replay_event("Trade", block=11, log=1, order_id=90, filled_size=400),
            state,
            settings,
            PRICE_PRECISION,
            SIZE_PRECISION,
        )

        self.assertEqual(fills[0]["historical_depth_consumed_percent"], 50)
        self.assertTrue(fills[0]["event_material_impact_diagnostic"])

    def test_multi_match_transaction_uses_one_transaction_denominator(self) -> None:
        transaction_hash = "0x" + "ab" * 32
        first = replay_event(
            "Trade",
            block=20,
            log=1,
            order_id=11,
            filled_size=100,
            transaction_hash=transaction_hash,
        )
        second = replay_event(
            "Trade",
            block=20,
            log=2,
            order_id=12,
            filled_size=200,
            transaction_hash=transaction_hash,
        )
        impacts = build_transaction_impacts(
            [first, second],
            [impact_fill(first, 80, "sim-1"), impact_fill(second, 120, "sim-2")],
            SIZE_PRECISION,
            Decimal("25"),
        )

        self.assertEqual(len(impacts), 1)
        self.assertEqual(impacts[0]["total_historical_aggressor_size_raw"], 300)
        self.assertEqual(impacts[0]["total_simulated_fill_size_raw"], 200)
        self.assertAlmostEqual(impacts[0]["simulated_to_historical_percent"], 200 / 3)
        self.assertTrue(impacts[0]["material_impact"])

    def test_displaced_volume_uses_each_downstream_trade_once(self) -> None:
        transaction_hash = "0x" + "cd" * 32
        first = replay_event(
            "Trade",
            block=21,
            log=1,
            order_id=21,
            filled_size=100,
            transaction_hash=transaction_hash,
        )
        second = replay_event(
            "Trade",
            block=21,
            log=2,
            order_id=22,
            filled_size=200,
            transaction_hash=transaction_hash,
        )
        impacts = build_transaction_impacts(
            [first, second],
            [impact_fill(first, 80, "sim-1"), impact_fill(first, 70, "sim-2")],
            SIZE_PRECISION,
            Decimal("25"),
        )
        allocations = impacts[0]["displaced_historical_trade_events"]

        self.assertEqual(impacts[0]["historical_downstream_maker_volume_displaced_raw"], 150)
        self.assertEqual([row["displaced_size_raw"] for row in allocations], [80, 20, 50])
        self.assertEqual(sum(row["displaced_size_raw"] for row in allocations), 150)

    def test_transaction_volume_cannot_fill_simulated_orders_twice(self) -> None:
        transaction_hash = "0x" + "ef" * 32
        trade = replay_event(
            "Trade",
            block=22,
            log=1,
            order_id=31,
            filled_size=100,
            transaction_hash=transaction_hash,
        )

        with self.assertRaisesRegex(RuntimeError, "exceed total Trade volume"):
            build_transaction_impacts(
                [trade],
                [impact_fill(trade, 60, "sim-1"), impact_fill(trade, 60, "sim-2")],
                SIZE_PRECISION,
                Decimal("25"),
            )

    def test_deterministic_outputs_have_stable_hashes_and_jsonl(self) -> None:
        summary = {"value": 1}
        timeline = [{"sequence": 1}, {"sequence": 2}]
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            first_manifest = write_outputs(Path(first), summary, timeline)
            second_manifest = write_outputs(Path(second), summary, timeline)

            self.assertEqual(first_manifest, second_manifest)
            self.assertEqual(
                len((Path(first) / "timeline.jsonl").read_text(encoding="utf-8").splitlines()),
                2,
            )

    def test_excess_pnl_decomposition_uses_fill_time_midpoint(self) -> None:
        fills = [
            {
                "side": "buy",
                "simulated_fill_size_raw": 20,
                "price_raw": 9_900,
                "reference_midpoint_raw": 10_000,
            },
            {
                "side": "sell",
                "simulated_fill_size_raw": 10,
                "price_raw": 10_200,
                "reference_midpoint_raw": 10_100,
            },
        ]
        result = strategy_result_decomposition(
            fills,
            initial_inventory=Decimal("10"),
            final_inventory=Decimal("11"),
            final_mark_raw=10_300,
            price_precision=100,
            size_precision=10,
            maker_fees=Decimal("0.25"),
            action_fees=Decimal("0.10"),
        )

        self.assertEqual(result["buy_fill_count"], 1)
        self.assertEqual(result["sell_fill_count"], 1)
        self.assertEqual(result["ending_inventory_change"], Decimal("1"))
        self.assertEqual(result["realized_spread_capture"], Decimal("3"))
        self.assertEqual(result["inventory_mark_to_market_contribution"], Decimal("4"))
        self.assertEqual(result["strategy_excess_pnl"], Decimal("6.65"))


class ReplacementMathTests(unittest.TestCase):
    def test_replace_threshold_is_deterministic(self) -> None:
        order = StrategyOrder(
            strategy_order_id="strategy-1",
            side="buy",
            price_raw=10_000,
            size_raw=10,
            remaining_raw=10,
            status="active",
            eligible_block=1,
            decision_book_block=0,
            queue_ahead_ids=[],
            queue_ahead_raw=0,
            placed_block=0,
            placed_transaction_index=0,
        )

        self.assertFalse(should_replace(order, 10_001, Decimal("2")))
        self.assertTrue(should_replace(order, 10_003, Decimal("2")))


if __name__ == "__main__":
    unittest.main()
