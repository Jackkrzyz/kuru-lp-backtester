"""Tests for deterministic Kuru event reconstruction."""

import tempfile
import unittest
from pathlib import Path

from eth_abi import encode as abi_encode

from kuru_backtester.engine.rebuild_from_chain import (
    EVENTS_BY_TOPIC,
    ReconstructionError,
    add_order,
    cancel_order,
    decode_log,
    make_snapshot,
    open_database,
    reconstructed_best_bid_ask,
    save_vault_state,
    set_order_size,
    update_vault_from_trade,
    vault_state,
)


class EventDecodeTests(unittest.TestCase):
    def make_log(self, event_name: str, values: tuple[object, ...]) -> dict[str, object]:
        spec = next(spec for spec in EVENTS_BY_TOPIC.values() if spec.name == event_name)
        return {
            "topics": [spec.topic],
            "data": "0x" + abi_encode(spec.types, values).hex(),
            "transactionHash": "0x01",
        }

    def test_decodes_order_created(self) -> None:
        log = self.make_log(
            "OrderCreated",
            (7, "0x0000000000000000000000000000000000000123", 900, 44, True),
        )

        name, args = decode_log(log)

        self.assertEqual(name, "OrderCreated")
        self.assertEqual(args["order_id"], 7)
        self.assertEqual(args["owner"], "0x0000000000000000000000000000000000000123")
        self.assertEqual(args["size"], 900)
        self.assertEqual(args["price"], 44)
        self.assertTrue(args["is_buy"])

    def test_decodes_dynamic_order_id_array(self) -> None:
        log = self.make_log(
            "OrdersCanceled",
            ((2, 3, 5), "0x0000000000000000000000000000000000000456"),
        )

        name, args = decode_log(log)

        self.assertEqual(name, "OrdersCanceled")
        self.assertEqual(args["order_ids"], [2, 3, 5])

    def test_preserves_unknown_event(self) -> None:
        name, args = decode_log({"topics": ["0x" + "11" * 32], "data": "0x"})

        self.assertEqual(name, "Unknown")
        self.assertEqual(args, {})


class ReconstructionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.connection = open_database(Path(self.temporary.name) / "state.sqlite3")
        save_vault_state(
            self.connection,
            {
                "best_bid": 90 * 10**18,
                "bid_partially_filled_size": 0,
                "best_ask": 100 * 10**18,
                "ask_partially_filled_size": 0,
                "bid_order_size": 1_000,
                "ask_order_size": 1_000,
                "spread": 100,
            },
        )
        self.connection.commit()

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary.cleanup()

    def order_args(self) -> dict[str, object]:
        return {
            "order_id": 12,
            "owner": "0x0000000000000000000000000000000000000123",
            "size": 500,
            "price": 99,
            "is_buy": True,
        }

    def test_create_update_and_cancel_order(self) -> None:
        args = self.order_args()
        add_order(self.connection, args, 20, 1, 2)
        set_order_size(self.connection, 12, 300)

        level = self.connection.execute(
            "SELECT total_size FROM price_levels WHERE is_buy = 1 AND price = 99"
        ).fetchone()
        self.assertEqual(level[0], "300")

        cancel_args = {**args, "size": 300}
        cancel_order(self.connection, cancel_args)

        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 0)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM price_levels").fetchone()[0], 0
        )

    def test_rejects_cancel_that_does_not_match_state(self) -> None:
        args = self.order_args()
        add_order(self.connection, args, 20, 1, 2)

        with self.assertRaisesRegex(ReconstructionError, "do not match"):
            cancel_order(self.connection, {**args, "size": 499})

    def test_updates_vault_after_partial_and_full_ask_fills(self) -> None:
        update_vault_from_trade(
            self.connection,
            {"is_buy": True, "price": 100 * 10**18, "updated_size": 800},
        )
        self.assertEqual(vault_state(self.connection)["ask_partially_filled_size"], 200)

        update_vault_from_trade(
            self.connection,
            {"is_buy": True, "price": 100 * 10**18, "updated_size": 0},
        )
        state = vault_state(self.connection)
        self.assertEqual(state["ask_partially_filled_size"], 0)
        self.assertEqual(state["ask_order_size"], 995)
        self.assertEqual(state["best_ask"], 101 * 10**18)
        self.assertEqual(state["best_bid"], 100 * 10**18)
        self.assertEqual(state["bid_order_size"], 999)

    def test_snapshot_combines_explicit_and_vault_prices(self) -> None:
        add_order(self.connection, self.order_args(), 20, 1, 2)

        snapshot = make_snapshot(self.connection, 20, 1_700_000_000, 1)

        self.assertEqual(snapshot["explicit_best_bid"], 99)
        self.assertEqual(snapshot["vault_best_bid"], 90)
        self.assertEqual(snapshot["best_bid"], 99)
        self.assertEqual(snapshot["best_ask"], 100)
        self.assertEqual(snapshot["midpoint"], 99.5)
        self.assertEqual(snapshot["spread"], 1)
        self.assertEqual(snapshot["active_orders"], 1)

    def test_zero_size_vault_is_not_liquidity(self) -> None:
        state = vault_state(self.connection)
        state["bid_order_size"] = 0
        state["ask_order_size"] = 0
        save_vault_state(self.connection, state)

        best_bid, best_ask = reconstructed_best_bid_ask(self.connection, 1)

        self.assertEqual(best_bid, 2**256 - 1)
        self.assertEqual(best_ask, 0)


if __name__ == "__main__":
    unittest.main()
