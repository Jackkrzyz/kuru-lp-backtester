"""Tests for compact frontend replay artifacts."""

import json
import tempfile
import unittest
from pathlib import Path

from compact_replays import (
    build_compact_replay,
    build_compact_replay_from_records,
    canonical_json_bytes,
    frame_index,
)


def raw_record(sequence: int, *, action: str | None = None, cash: int = 1000) -> dict:
    placement = []
    fill = []
    if action == "placement":
        placement = [
            {
                "order_id": f"strategy-{sequence}",
                "side": "buy",
                "price": 1.0,
                "size": 2.0,
                "status": "pending",
                "decision_book_block": 99 + sequence,
                "eligible_block": 100 + sequence,
                "action_fee": 0,
            }
        ]
    if action == "fill":
        fill = [
            {
                "strategy_order_id": "strategy-1",
                "side": "buy",
                "price": 1.0,
                "size": 1.0,
                "notional": 1.0,
                "maker_fee": 0,
                "transaction_hash": "0x01",
                "historical_depth_consumed_percent": 5,
            }
        ]
    midpoint = 1 + sequence / 100
    return {
        "sequence": sequence,
        "timestamp": f"2026-01-01T00:00:{sequence:02d}Z",
        "block_number": 99 + sequence,
        "record_type": "strategy_fill" if fill else "market_event",
        "market": {"best_bid": midpoint - 0.01, "best_ask": midpoint + 0.01, "midpoint": midpoint},
        "active_simulated_orders": [],
        "cash": cash,
        "inventory": 1000 + (1 if fill else 0),
        "marked_portfolio_value": cash + 1000 + sequence,
        "cumulative_pnl": sequence,
        "placements": placement,
        "activations": [],
        "cancellations": [],
        "fills": fill,
    }


def summary(record_count: int, final: dict) -> dict:
    return {
        "market": "0xmarket",
        "config": {"max_transaction_participation_percent": 10},
        "metrics": {
            "initial_midpoint": 1.01,
            "initial_inventory": 1000,
            "final_cash": final["cash"],
            "final_inventory": final["inventory"],
            "final_portfolio_value": final["marked_portfolio_value"],
            "net_pnl": final["cumulative_pnl"],
            "maximum_drawdown": 1,
            "turnover": 1,
        },
        "hold_baseline": {
            "hold_pnl": 190,
            "strategy_excess_pnl": -171,
        },
        "decomposition": {"buy_fill_count": 1, "sell_fill_count": 0},
        "counterfactual": {
            "run_flagged": False,
            "max_transaction_impact_percent": 5,
        },
        "operational_costs": {
            "total_order_actions": 1,
            "break_even_fixed_cost_per_action": 0,
        },
        "timeline": {"record_count": record_count},
    }


class CompactReplayTests(unittest.TestCase):
    def test_boundary_frame_mapping_is_stable(self) -> None:
        self.assertEqual(frame_index(0, 20, 5), 0)
        self.assertEqual(frame_index(19, 20, 5), 4)
        self.assertTrue(all(0 <= frame_index(index, 20, 5) <= 4 for index in range(20)))

    def test_preserves_actions_financial_changes_and_exact_final_state(self) -> None:
        records = [raw_record(index) for index in range(1, 21)]
        records[2] = raw_record(3, action="placement")
        records[11] = raw_record(12, action="fill", cash=999)
        final = records[-1]
        data = summary(len(records), final)
        with tempfile.TemporaryDirectory() as directory:
            raw_path = Path(directory) / "timeline.jsonl"
            raw_path.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            replay = build_compact_replay(
                raw_path,
                data,
                interval_name="test",
                scenario_name="max_10_percent",
                maximum_frames=5,
            )

        self.assertLessEqual(len(replay["frames"]), 5)
        self.assertEqual(replay["frames"][0]["sequence"], 1)
        self.assertEqual(replay["frames"][-1]["sequence"], 20)
        self.assertEqual(
            sum(len(frame["placements"]) for frame in replay["frames"]), 1
        )
        self.assertEqual(sum(len(frame["fills"]) for frame in replay["frames"]), 1)
        self.assertEqual(replay["frames"][-1]["cash"], final["cash"])
        self.assertEqual(
            replay["frames"][-1]["marked_value"], final["marked_portfolio_value"]
        )
        self.assertEqual(
            replay["compaction"]["preserved_financial_change_count"], len(records)
        )

    def test_output_bytes_are_deterministic(self) -> None:
        payload = {"frames": [{"frame": 0, "cash": 1000}]}
        self.assertEqual(canonical_json_bytes(payload), canonical_json_bytes(payload))

    def test_direct_record_compaction_matches_jsonl_compaction(self) -> None:
        records = [raw_record(index) for index in range(1, 21)]
        records[3] = raw_record(4, action="placement")
        records[12] = raw_record(13, action="fill", cash=999)
        data = summary(len(records), records[-1])
        with tempfile.TemporaryDirectory() as directory:
            raw_path = Path(directory) / "timeline.jsonl"
            raw_path.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            from_file = build_compact_replay(
                raw_path,
                data,
                interval_name="test",
                scenario_name="max_10_percent",
                maximum_frames=5,
            )
        direct = build_compact_replay_from_records(
            records,
            data,
            interval_name="test",
            scenario_name="max_10_percent",
            maximum_frames=5,
        )
        self.assertEqual(canonical_json_bytes(direct), canonical_json_bytes(from_file))


if __name__ == "__main__":
    unittest.main()
