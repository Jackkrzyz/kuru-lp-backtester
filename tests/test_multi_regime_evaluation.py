"""Tests for the deterministic multi-regime evaluation wrapper."""

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from multi_regime_evaluation import (
    default_config,
    require_evaluation_gates,
    write_comparison,
)


def passing_validation() -> dict[str, object]:
    return {
        "interval": {"from_block": 101, "to_block": 200},
        "checkpoint": {"block_number": 100},
        "event_parity": {
            "status": "pass",
            "missing_from_envio": 0,
            "missing_from_rpc": 0,
        },
        "validation": {
            "maximum_price_level_error_raw": 0,
            "maximum_aggregate_size_error_raw": 0,
            "maximum_midpoint_error": 0,
            "queue_invariant_failures": 0,
            "queue_invariant_status": "pass",
            "validated_block_count": 7,
        },
        "cancellation_summary_audit": {
            "status": "pass",
            "summary_order_ids_applied_as_cancellations": 0,
            "duplicate_individual_cancellation_count": 0,
        },
    }


class MultiRegimeTests(unittest.TestCase):
    def test_default_parameters_change_only_the_participation_cap(self) -> None:
        uncapped = default_config(None)
        capped = default_config(Decimal("10"))

        self.assertEqual(uncapped.starting_cash, Decimal("1000"))
        self.assertEqual(uncapped.order_size, Decimal("200"))
        self.assertEqual(uncapped.bid_offset_bps, Decimal("10"))
        self.assertIsNone(uncapped.max_transaction_participation_percent)
        self.assertEqual(capped.max_transaction_participation_percent, Decimal("10"))
        self.assertEqual(
            {**uncapped.__dict__, "max_transaction_participation_percent": Decimal("10")},
            capped.__dict__,
        )

    def test_all_reconstruction_gates_are_required(self) -> None:
        payload = passing_validation()
        require_evaluation_gates(payload)  # type: ignore[arg-type]
        payload["validation"]["maximum_midpoint_error"] = 1  # type: ignore[index]

        with self.assertRaisesRegex(RuntimeError, "maximum_midpoint_error"):
            require_evaluation_gates(payload)  # type: ignore[arg-type]

    def test_comparison_hash_is_deterministic(self) -> None:
        rows = [
            {
                "interval": "relatively_quiet",
                "scenario": "uncapped",
                "artifacts": {
                    "summary_sha256": "a" * 64,
                    "timeline_sha256": "b" * 64,
                },
            }
        ]
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            first_hashes = write_comparison(Path(first), rows)
            second_hashes = write_comparison(Path(second), rows)

            self.assertEqual(first_hashes, second_hashes)
            self.assertEqual(
                json.loads((Path(first) / "comparison.json").read_text(encoding="utf-8")),
                json.loads((Path(second) / "comparison.json").read_text(encoding="utf-8")),
            )


if __name__ == "__main__":
    unittest.main()
