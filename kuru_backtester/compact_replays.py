#!/usr/bin/env python3
"""Build compact, deterministic frontend replays from raw strategy timelines."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Optional


DEFAULT_COMPARISON = Path("multi_regime_output/comparison.json")
DEFAULT_INTERVAL_MANIFEST = Path("interval_manifest.json")
from kuru_backtester.paths import LEGACY_REPLAYS

DEFAULT_OUTPUT_DIR = LEGACY_REPLAYS
MAX_FRAMES = 1_500
FINANCIAL_FIELDS = (
    "cash",
    "inventory",
    "marked_portfolio_value",
    "cumulative_pnl",
)


def canonical_json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"


def number(value: Decimal) -> int | float:
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def compact_order(order: dict[str, Any]) -> dict[str, Any]:
    return {
        "order_id": order["order_id"],
        "side": order["side"],
        "status": order["status"],
        "price": order["price"],
        "size": order["size"],
        "remaining_size": order["remaining_size"],
        "queue_ahead_size": order.get("queue_ahead_size", 0),
        "eligible_block": order.get("eligible_block"),
    }


def compact_placement(action: dict[str, Any]) -> dict[str, Any]:
    return {
        "order_id": action["order_id"],
        "side": action["side"],
        "price": action["price"],
        "size": action["size"],
        "status": action["status"],
        "decision_book_block": action.get("decision_book_block"),
        "eligible_block": action["eligible_block"],
        "action_fee": action.get("action_fee", 0),
        "kind": action.get("kind", "initial"),
        "flip_price": action.get("flip_price"),
    }


def compact_activation(action: dict[str, Any]) -> dict[str, Any]:
    return {
        "order_id": action["order_id"],
        "side": action["side"],
        "activation_block": action["activation_block"],
        "queue_ahead_size": action.get("queue_ahead_size", 0),
        "queue_ahead_order_count": action.get("queue_ahead_order_count", 0),
        **({"status": action["status"], "reason": action.get("reason")} if "status" in action else {}),
    }


def compact_cancellation(action: dict[str, Any]) -> dict[str, Any]:
    return {
        "order_id": action["order_id"],
        "side": action["side"],
        "reason": action["reason"],
        "status_before_cancel": action.get("status_before_cancel"),
        "action_fee": action.get("action_fee", 0),
    }


def compact_fill(action: dict[str, Any]) -> dict[str, Any]:
    return {
        "order_id": action["strategy_order_id"],
        "side": action["side"],
        "price": action["price"],
        "size": action["size"],
        "notional": action["notional"],
        "maker_fee": action.get("maker_fee", 0),
        "transaction_hash": action["transaction_hash"],
        "historical_depth_consumed_percent": action.get(
            "historical_depth_consumed_percent",
            (
                100 * action["simulated_fill_size_raw"] / action["historical_event_fill_size_raw"]
                if action.get("historical_event_fill_size_raw")
                else 0
            ),
        ),
        "is_flip_fill": action.get("is_flip_fill", False),
    }


def hold_pnl(raw: dict[str, Any], summary: dict[str, Any]) -> int | float | None:
    midpoint = raw["market"].get("midpoint")
    if midpoint is None:
        return None
    initial_midpoint = Decimal(str(summary["metrics"]["initial_midpoint"]))
    inventory = Decimal(str(summary["metrics"]["initial_inventory"]))
    value = inventory * (Decimal(str(midpoint)) - initial_midpoint)
    return number(value)


def update_order_state(raw: dict[str, Any], order_state: dict[str, dict[str, Any]]) -> None:
    """Apply one complete snapshot or ordered order-state delta."""
    has_delta_schema = "order_snapshot_complete" in raw or "simulated_order_deltas" in raw
    if raw.get("order_snapshot_complete") or not has_delta_schema:
        order_state.clear()
        for order in raw.get("active_simulated_orders", []):
            order_state[str(order["order_id"])] = order
    for order in raw.get("simulated_order_deltas", []):
        order_id = str(order["order_id"])
        if order.get("status") in {"pending", "active"}:
            order_state[order_id] = order
        else:
            order_state.pop(order_id, None)


def compact_state(
    raw: dict[str, Any],
    summary: dict[str, Any],
    order_state: Optional[dict[str, dict[str, Any]]] = None,
    *,
    include_orders: bool = True,
) -> dict[str, Any]:
    market = raw["market"]
    source_orders = raw["active_simulated_orders"] if order_state is None else order_state.values()
    return {
        "sequence": raw["sequence"],
        "timestamp": raw["timestamp"],
        "block": raw["block_number"],
        "bid": market.get("best_bid"),
        "ask": market.get("best_ask"),
        "midpoint": market.get("midpoint"),
        "active_orders": [
            compact_order(order)
            for order in sorted(source_orders, key=lambda item: str(item["order_id"]))
        ] if include_orders else [],
        "cash": raw["cash"],
        "inventory": raw["inventory"],
        "marked_value": raw["marked_portfolio_value"],
        "cumulative_pnl": raw["cumulative_pnl"],
        "hold_baseline_pnl": hold_pnl(raw, summary),
    }


def compact_actions(raw: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    context = {
        "sequence": raw["sequence"],
        "timestamp": raw["timestamp"],
        "block": raw["block_number"],
    }

    def with_context(items: Iterable[dict[str, Any]], converter: Any) -> list[dict[str, Any]]:
        return [{**context, **converter(item)} for item in items]

    return {
        "placements": with_context(raw.get("placements", []), compact_placement),
        "activations": with_context(raw.get("activations", []), compact_activation),
        "cancellations": with_context(raw.get("cancellations", []), compact_cancellation),
        "fills": with_context(raw.get("fills", []), compact_fill),
    }


def has_actions(actions: dict[str, list[dict[str, Any]]]) -> bool:
    return any(actions.values())


def financial_signature(raw: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(raw[field] for field in FINANCIAL_FIELDS)


def frame_index(record_index: int, record_count: int, maximum_frames: int) -> int:
    if record_count <= maximum_frames:
        return record_index
    if record_index == 0:
        return 0
    if record_index == record_count - 1:
        return maximum_frames - 1
    return 1 + ((record_index - 1) * (maximum_frames - 2) // (record_count - 2))


def new_frame(index: int, state: dict[str, Any]) -> dict[str, Any]:
    return {
        "frame": index,
        **state,
        "placements": [],
        "activations": [],
        "cancellations": [],
        "fills": [],
        "financial_changes": [],
        "active_order_deltas": [],
        "order_snapshot_complete": False,
    }


def financial_snapshot(raw: dict[str, Any], state: dict[str, Any]) -> list[Any]:
    """Use a fixed-position row to keep all financial changes small."""
    return [
        raw["sequence"],
        raw["timestamp"],
        raw["block_number"],
        state["bid"],
        state["ask"],
        state["midpoint"],
        state["cash"],
        state["inventory"],
        state["marked_value"],
        state["cumulative_pnl"],
        state["hold_baseline_pnl"],
    ]


def validate_final_state(replay: dict[str, Any], summary: dict[str, Any]) -> None:
    final = replay["frames"][-1]
    expected = summary["metrics"]
    comparisons = {
        "cash": expected["final_cash"],
        "inventory": expected["final_inventory"],
        "marked_value": expected["final_portfolio_value"],
        "cumulative_pnl": expected.get("net_pnl", expected.get("position_pnl")),
        "hold_baseline_pnl": summary["hold_baseline"]["hold_pnl"],
    }
    for field, expected_value in comparisons.items():
        actual_value = final[field]
        if Decimal(str(actual_value)) != Decimal(str(expected_value)):
            raise RuntimeError(
                f"compact final {field} {actual_value} does not match summary {expected_value}"
            )


def build_compact_replay(
    raw_path: Path,
    summary: dict[str, Any],
    *,
    interval_name: str,
    scenario_name: str,
    maximum_frames: int = MAX_FRAMES,
) -> dict[str, Any]:
    """Build a compact replay from a JSONL timeline on disk.

    This wrapper keeps the command-line and historical-artifact interface.
    New product runs use :func:`build_compact_replay_from_records` so they do
    not write and read an intermediate JSONL file.
    """
    with raw_path.open("r", encoding="utf-8") as handle:
        records = (json.loads(line) for line in handle if line.strip())
        return build_compact_replay_from_records(
            records,
            summary,
            interval_name=interval_name,
            scenario_name=scenario_name,
            maximum_frames=maximum_frames,
        )


def build_compact_replay_from_records(
    records: Iterable[dict[str, Any]],
    summary: dict[str, Any],
    *,
    interval_name: str,
    scenario_name: str,
    maximum_frames: int = MAX_FRAMES,
) -> dict[str, Any]:
    """Build the same compact replay directly from decoded timeline records."""
    if maximum_frames < 2:
        raise ValueError("maximum frames must be at least two")
    record_count = int(summary["timeline"]["record_count"])
    frames_by_index: dict[int, dict[str, Any]] = {}
    previous_financial: Optional[tuple[Any, ...]] = None
    read_count = 0
    action_record_count = 0
    financial_change_count = 0
    order_state: dict[str, dict[str, Any]] = {}
    action_counts = {key: 0 for key in ("placements", "activations", "cancellations", "fills")}

    for record_index, raw in enumerate(records):
        read_count += 1
        index = frame_index(record_index, record_count, maximum_frames)
        delta_schema = "order_snapshot_complete" in raw or "simulated_order_deltas" in raw
        update_order_state(raw, order_state)
        include_orders = not delta_schema or bool(raw.get("order_snapshot_complete"))
        state = compact_state(raw, summary, order_state, include_orders=include_orders)
        actions = compact_actions(raw)
        action_record = has_actions(actions)
        signature = financial_signature(raw)
        financial_change = previous_financial is None or signature != previous_financial
        previous_financial = signature
        is_boundary = record_index == 0 or record_index == record_count - 1

        frame = frames_by_index.get(index)
        if frame is None:
            frame = new_frame(index, state)
            frames_by_index[index] = frame
        else:
            for key, value in state.items():
                if key != "active_orders" or include_orders:
                    frame[key] = value
        if delta_schema:
            if raw.get("order_snapshot_complete"):
                frame["order_snapshot_complete"] = True
            frame["active_order_deltas"].extend(
                compact_order(order) for order in raw.get("simulated_order_deltas", [])
            )
        for key, values in actions.items():
            frame[key].extend(values)
            action_counts[key] += len(values)
        if action_record:
            action_record_count += 1
        if financial_change:
            financial_change_count += 1
        if is_boundary or financial_change:
            frame["financial_changes"].append(financial_snapshot(raw, state))

    if read_count != record_count:
        raise RuntimeError(f"read {read_count} raw records; summary declares {record_count}")
    frames = [frames_by_index[index] for index in sorted(frames_by_index)]
    for output_index, frame in enumerate(frames):
        frame["frame"] = output_index
    engine = summary.get("engine", "advanced_mm_bot")
    is_lp = engine == "kuru_lp"
    if is_lp:
        buy_fill_count = sum(1 for item in summary["fills"] if item["side"] == "buy")
        sell_fill_count = sum(1 for item in summary["fills"] if item["side"] == "sell")
        result_payload = {
            "strategy_pnl": summary["metrics"]["position_pnl"],
            "hold_pnl": summary["metrics"]["hold_pnl"],
            "excess_pnl": summary["metrics"]["excess_pnl"],
            "earned_flip_spread": summary["metrics"]["earned_flip_spread"],
            "maximum_drawdown": summary["metrics"]["maximum_drawdown"],
            "turnover": summary["metrics"]["turnover"],
            "ending_inventory": summary["metrics"]["final_inventory"],
            "buy_fill_count": buy_fill_count,
            "sell_fill_count": sell_fill_count,
            "transaction_impact_warning": summary["counterfactual"]["run_flagged"],
            "maximum_transaction_impact_percent": summary["counterfactual"]["max_transaction_impact_percent"],
            "total_order_actions": summary["metrics"]["placement_count"],
            "break_even_action_cost": 0,
            "time_in_range_percent": summary["metrics"]["time_in_range_percent"],
            "capital_utilization_percent": summary["metrics"]["capital_utilization_percent"],
            "ending_mon_percent": summary["metrics"]["ending_mon_percent"],
            "ending_usdc_percent": summary["metrics"]["ending_usdc_percent"],
            "required_mon_usdc_ratio": summary["initial_position"]["required_mon_usdc_ratio"],
        }
    else:
        result_payload = {
            "strategy_pnl": summary["metrics"]["net_pnl"],
            "hold_pnl": summary["hold_baseline"]["hold_pnl"],
            "excess_pnl": summary["hold_baseline"]["strategy_excess_pnl"],
            "maximum_drawdown": summary["metrics"]["maximum_drawdown"],
            "turnover": summary["metrics"]["turnover"],
            "ending_inventory": summary["metrics"]["final_inventory"],
            "buy_fill_count": summary["decomposition"]["buy_fill_count"],
            "sell_fill_count": summary["decomposition"]["sell_fill_count"],
            "transaction_impact_warning": summary["counterfactual"]["run_flagged"],
            "maximum_transaction_impact_percent": summary["counterfactual"]["max_transaction_impact_percent"],
            "total_order_actions": summary["operational_costs"]["total_order_actions"],
            "break_even_action_cost": summary["operational_costs"]["break_even_fixed_cost_per_action"],
        }
    replay = {
        "schema_version": "1.0",
        "engine": engine,
        "market": summary["market"],
        "interval": interval_name,
        "scenario": scenario_name,
        "uncapped": summary["config"]["max_transaction_participation_percent"] is None,
        "config": summary["config"],
        "results": result_payload,
        "compaction": {
            "raw_record_count": record_count,
            "compact_frame_count": len(frames),
            "maximum_frames": maximum_frames,
            "preserved_action_record_count": action_record_count,
            "preserved_financial_change_count": financial_change_count,
            "action_counts": action_counts,
            "financial_change_fields": [
                "sequence",
                "timestamp",
                "block",
                "bid",
                "ask",
                "midpoint",
                "cash",
                "inventory",
                "marked_value",
                "cumulative_pnl",
                "hold_baseline_pnl",
            ],
            "rule": (
                "All action records and financial changes are preserved inside frames. "
                "Only market-only records are sampled for the frame series."
            ),
        },
        "frames": frames,
    }
    if len(frames) > maximum_frames:
        raise RuntimeError("compact replay exceeds its frame limit")
    validate_final_state(replay, summary)
    return replay


def data_scope(interval_manifest: dict[str, Any]) -> dict[str, Any]:
    source = interval_manifest["source"]
    candidates = {
        name: {
            "from_block": interval["from_block"],
            "to_block": interval["to_block"],
            "order_event_count": interval["order_event_count"],
            "order_event_cache": interval["order_event_cache"],
        }
        for name, interval in interval_manifest["intervals"].items()
    }
    return {
        "broad_trade_discovery": {
            "purpose": "rank local windows with Trade events only",
            "from_block": source["from_block"],
            "to_block": source["to_block"],
            "trade_cache": interval_manifest["trade_cache"],
            "full_scan_run": source["full_scan_run"],
        },
        "candidate_order_enrichment": {
            "purpose": "fetch dense order events only for final candidate intervals",
            "intervals": candidates,
        },
    }


def generate_all(
    comparison_path: Path,
    interval_manifest_path: Path,
    output_dir: Path,
    maximum_frames: int,
) -> dict[str, Any]:
    comparison = json.loads(comparison_path.read_text(encoding="utf-8"))
    interval_manifest = json.loads(interval_manifest_path.read_text(encoding="utf-8"))
    output_dir.mkdir(parents=True, exist_ok=True)
    artifacts: list[dict[str, Any]] = []
    for row in comparison["results"]:
        interval_name = row["interval"]
        scenario_name = row["scenario"]
        raw_path = Path(row["artifacts"]["timeline"])
        summary_path = Path(row["artifacts"]["summary"])
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        replay = build_compact_replay(
            raw_path,
            summary,
            interval_name=interval_name,
            scenario_name=scenario_name,
            maximum_frames=maximum_frames,
        )
        output_path = output_dir / f"{interval_name}--{scenario_name}.json"
        output_bytes = canonical_json_bytes(replay)
        output_path.write_bytes(output_bytes)
        raw_size = raw_path.stat().st_size
        artifacts.append(
            {
                "interval": interval_name,
                "scenario": scenario_name,
                "path": output_path.name,
                "raw_path": raw_path.as_posix(),
                "raw_record_count": replay["compaction"]["raw_record_count"],
                "compact_frame_count": replay["compaction"]["compact_frame_count"],
                "raw_size_bytes": raw_size,
                "compact_size_bytes": len(output_bytes),
                "size_reduction_percent": float(
                    (Decimal(raw_size - len(output_bytes)) * Decimal(100) / Decimal(raw_size)).quantize(
                        Decimal("0.0001")
                    )
                ),
                "sha256": hashlib.sha256(output_bytes).hexdigest(),
                "uncapped": replay["uncapped"],
                "transaction_impact_warning": replay["results"][
                    "transaction_impact_warning"
                ],
            }
        )
    catalog = {
        "schema_version": "1.0",
        "default": {
            "interval": "relatively_active",
            "scenario": "max_10_percent",
        },
        "market": comparison["market"],
        "data_scope": data_scope(interval_manifest),
        "artifacts": artifacts,
    }
    catalog_bytes = canonical_json_bytes(catalog)
    (output_dir / "catalog.json").write_bytes(catalog_bytes)
    manifest = {
        "catalog_sha256": hashlib.sha256(catalog_bytes).hexdigest(),
        "artifacts": [
            {"path": row["path"], "sha256": row["sha256"]} for row in artifacts
        ],
    }
    (output_dir / "manifest.json").write_bytes(canonical_json_bytes(manifest))
    return catalog


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--comparison", type=Path, default=DEFAULT_COMPARISON)
    parser.add_argument("--interval-manifest", type=Path, default=DEFAULT_INTERVAL_MANIFEST)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--max-frames", type=int, default=MAX_FRAMES)
    args = parser.parse_args(argv)
    try:
        catalog = generate_all(
            args.comparison,
            args.interval_manifest,
            args.output_dir,
            args.max_frames,
        )
    except (OSError, ValueError, KeyError, RuntimeError, json.JSONDecodeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(catalog, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
