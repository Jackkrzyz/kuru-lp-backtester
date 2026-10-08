"""Tests for the separate reconstruction validation report."""

import json
import tempfile
import unittest
from pathlib import Path

from reconstruction_validation_report import (
    L3Book,
    ReplayEvent,
    apply_event,
    cancellation_summary_audit,
    combine_errors,
    compute_level_errors,
    event_parity,
    load_staged_envio_logs,
    load_named_interval,
    stable_json,
    timeline_record,
    validation_blocks,
)


OWNER = "0x0000000000000000000000000000000000000123"


class ErrorMetricTests(unittest.TestCase):
    def test_exact_levels_have_zero_error(self) -> None:
        errors = compute_level_errors({10: 100, 9: 200}, {10: 100, 9: 200})

        self.assertEqual(errors.maximum_price_error_raw, 0)
        self.assertEqual(errors.maximum_size_error_raw, 0)
        self.assertEqual(errors.missing_local_levels, 0)
        self.assertEqual(errors.extra_local_levels, 0)

    def test_level_and_size_errors_are_reported(self) -> None:
        errors = compute_level_errors(
            {9: 100, 11: 250}, {10: 100, 12: 200, 13: 50}
        )

        self.assertEqual(errors.maximum_price_error_raw, 1)
        self.assertEqual(errors.maximum_size_error_raw, 250)
        self.assertEqual(errors.missing_local_levels, 3)
        self.assertEqual(errors.extra_local_levels, 2)

    def test_combines_bid_and_ask_errors(self) -> None:
        combined = combine_errors(
            compute_level_errors({10: 100}, {11: 100}),
            compute_level_errors({20: 100}, {20: 90}),
        )

        self.assertEqual(combined.maximum_price_error_raw, 1)
        self.assertEqual(combined.maximum_size_error_raw, 100)


class OutputSchemaTests(unittest.TestCase):
    def make_book(self) -> L3Book:
        book = L3Book()
        book.append(1, OWNER, 100, 99, True)
        book.append(2, OWNER, 100, 101, False)
        return book

    def test_timeline_record_has_frontend_fields(self) -> None:
        record = timeline_record(
            sequence=1,
            timestamp=1_700_000_000,
            block=10,
            transaction_index=2,
            log_index=3,
            record_type="market_event",
            market_event={"name": "OrderCreated", "args": {}},
            book=self.make_book(),
            price_precision=1,
            simulated_order=None,
            fills=[],
            cancellations=[],
            cash=5.0,
            inventory=2.0,
        )

        self.assertEqual(record["market"]["midpoint"], 100.0)
        self.assertEqual(record["active_simulated_orders"], [])
        self.assertEqual(record["fills"], [])
        self.assertEqual(record["cancellations"], [])
        self.assertEqual(record["marked_portfolio_value"], 205.0)
        self.assertEqual(record["cumulative_pnl"], 205.0)

    def test_json_output_is_deterministic(self) -> None:
        left = stable_json({"b": 2, "a": [3, 1]})
        right = stable_json({"a": [3, 1], "b": 2})

        self.assertEqual(left, right)
        self.assertEqual(json.loads(left), {"a": [3, 1], "b": 2})

    def test_missing_envio_is_not_a_parity_pass(self) -> None:
        result = event_parity(None, [{"any": "rpc log"}])

        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["rpc_event_count"], 1)
        self.assertIsNone(result["envio_event_count"])

    def test_even_validation_points_include_bounds_and_five_intermediates(self) -> None:
        blocks = validation_blocks([], 100, 199, 7)

        self.assertEqual(blocks[0], 100)
        self.assertEqual(blocks[-1], 199)
        self.assertEqual(len(blocks), 7)

    def test_cancellation_summary_is_not_applied_twice(self) -> None:
        book = self.make_book()
        summary = ReplayEvent(
            block_number=10,
            transaction_index=0,
            log_index=0,
            transaction_hash="0x01",
            name="OrdersCanceled",
            args={"order_ids": [1, 0], "owner": OWNER},
            raw={},
        )
        individual = ReplayEvent(
            block_number=10,
            transaction_index=0,
            log_index=1,
            transaction_hash="0x01",
            name="OrderCanceled",
            args={
                "order_id": 1,
                "owner": OWNER,
                "price": 99,
                "size": 100,
                "is_buy": True,
            },
            raw={},
        )

        apply_event(book, summary, 1)
        self.assertIn(1, book.orders)
        apply_event(book, individual, 1)
        self.assertNotIn(1, book.orders)
        audit = cancellation_summary_audit([summary, individual])
        self.assertEqual(audit["status"], "pass")
        self.assertEqual(audit["summary_order_ids_applied_as_cancellations"], 0)
        self.assertEqual(audit["summary_zero_sentinel_count"], 1)

    def test_named_interval_loads_from_manifest(self) -> None:
        payload = {
            "market": "0x01",
            "size_precision": 10,
            "trade_cache": "trade.jsonl",
            "intervals": {
                "active": {
                    "from_block": 10,
                    "to_block": 20,
                    "order_event_cache": "orders.jsonl",
                }
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "intervals.json"
            path.write_text(json.dumps(payload), encoding="utf-8")

            _, interval = load_named_interval(path, "active")

        self.assertEqual(interval["name"], "active")
        self.assertEqual(interval["from_block"], 10)
        self.assertEqual(interval["trade_cache"], "trade.jsonl")

    def test_staged_envio_file_keeps_exact_order_and_timestamps(self) -> None:
        rows = [
            {
                "address": "0xabc", "block_number": 10, "block_timestamp": 100,
                "transaction_hash": "0x01", "transaction_index": 1,
                "log_index": 2, "topic0": "0x11", "topic1": None,
                "topic2": None, "topic3": None, "data": "0x",
            },
            {
                "address": "0xabc", "block_number": 11, "block_timestamp": 101,
                "transaction_hash": "0x02", "transaction_index": 0,
                "log_index": 1, "topic0": "0x22", "topic1": None,
                "topic2": None, "topic3": None, "data": "0x",
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.write_text(
                "\n".join(json.dumps(row) for row in rows) + "\n",
                encoding="utf-8",
            )
            logs, timestamps = load_staged_envio_logs(path, "0xabc", 10, 11)

        self.assertEqual(logs, rows)
        self.assertEqual(timestamps, {10: 100, 11: 101})


if __name__ == "__main__":
    unittest.main()
