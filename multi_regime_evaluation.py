#!/usr/bin/env python3
"""Run the fixed default strategy across verified regimes and participation caps."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

import l3_vertical_slice as l3
from rebuild_from_chain import MARKET_ADDRESS, ReconstructionError, RpcClient, market_parameters
from strategy_vertical_slice import (
    StrategyConfig,
    canonical_json_bytes,
    load_verified_source_events,
    run_strategy,
    verify_validation_summary,
    write_outputs,
)


INTERVALS = ("relatively_quiet", "relatively_active", "directional")
SCENARIOS: tuple[tuple[str, Optional[Decimal]], ...] = (
    ("uncapped", None),
    ("max_25_percent", Decimal("25")),
    ("max_10_percent", Decimal("10")),
    ("max_5_percent", Decimal("5")),
)


def default_config(cap: Optional[Decimal]) -> StrategyConfig:
    """Return the unchanged command defaults plus one optional participation cap."""
    return StrategyConfig(
        starting_cash=Decimal("1000"),
        starting_inventory=Decimal("1000"),
        bid_offset_bps=Decimal("10"),
        ask_offset_bps=Decimal("10"),
        order_size=Decimal("200"),
        minimum_inventory=Decimal("0"),
        maximum_inventory=Decimal("2000"),
        cancel_replace=True,
        replace_threshold_bps=Decimal("1"),
        maker_fee_bps=Decimal("0"),
        fixed_action_fee=Decimal("0"),
        inclusion_delay_blocks=1,
        material_impact_threshold_percent=Decimal("25"),
        max_transaction_participation_percent=cap,
    )


def require_evaluation_gates(validation: dict[str, Any]) -> None:
    """Reject an interval unless every required reconstruction gate passed."""
    parity = validation["event_parity"]
    reconstruction = validation["validation"]
    cancellation = validation.get("cancellation_summary_audit", {})
    checkpoint = validation.get("checkpoint", {})
    if parity.get("status") != "pass" or parity.get("missing_from_envio") != 0 or parity.get(
        "missing_from_rpc"
    ) != 0:
        raise RuntimeError("Envio/RPC parity gate failed")
    for key in (
        "maximum_price_level_error_raw",
        "maximum_aggregate_size_error_raw",
        "maximum_midpoint_error",
        "queue_invariant_failures",
    ):
        if reconstruction.get(key) != 0:
            raise RuntimeError(f"reconstruction gate failed: {key}")
    if reconstruction.get("validated_block_count", 0) < 7:
        raise RuntimeError("fewer than seven L2 checkpoints were validated")
    if reconstruction.get("queue_invariant_status") != "pass":
        raise RuntimeError("queue-invariant gate failed")
    if checkpoint.get("block_number") != validation["interval"]["from_block"] - 1:
        raise RuntimeError("checkpoint is not from_block - 1")
    if cancellation.get("status") != "pass":
        raise RuntimeError("cancellation-summary audit failed")
    if cancellation.get("summary_order_ids_applied_as_cancellations") != 0:
        raise RuntimeError("a cancellation summary was applied as an individual cancellation")
    if cancellation.get("duplicate_individual_cancellation_count") != 0:
        raise RuntimeError("duplicate individual cancellations were found")


def comparison_row(
    interval_name: str,
    scenario_name: str,
    validation: dict[str, Any],
    summary: dict[str, Any],
    output_dir: Path,
    manifest: dict[str, str],
) -> dict[str, Any]:
    metrics = summary["metrics"]
    return {
        "interval": interval_name,
        "scenario": scenario_name,
        "from_block": validation["interval"]["from_block"],
        "to_block": validation["interval"]["to_block"],
        "max_transaction_participation_percent": summary["counterfactual"][
            "maximum_transaction_participation_percent"
        ],
        "validation": {
            "envio_event_count": validation["event_parity"]["envio_event_count"],
            "rpc_event_count": validation["event_parity"]["rpc_event_count"],
            "parity_status": validation["event_parity"]["status"],
            "validated_blocks": validation["validation"]["validated_blocks"],
            "maximum_price_level_error_raw": validation["validation"][
                "maximum_price_level_error_raw"
            ],
            "maximum_aggregate_size_error_raw": validation["validation"][
                "maximum_aggregate_size_error_raw"
            ],
            "maximum_midpoint_error": validation["validation"]["maximum_midpoint_error"],
            "queue_invariant_failures": validation["validation"][
                "queue_invariant_failures"
            ],
            "cancellation_summary_status": validation["cancellation_summary_audit"][
                "status"
            ],
        },
        "strategy": {
            "fill_count": metrics["fill_count"],
            "filled_size": metrics["filled_size"],
            "turnover": metrics["turnover"],
            "final_cash": metrics["final_cash"],
            "final_inventory": metrics["final_inventory"],
            "final_portfolio_value": metrics["final_portfolio_value"],
            "net_pnl": metrics["net_pnl"],
            "maximum_drawdown": metrics["maximum_drawdown"],
            "maker_fees": metrics["maker_fees"],
            "action_fees": metrics["action_fees"],
            "hold_baseline": summary["hold_baseline"],
            "decomposition": summary["decomposition"],
            "operational_costs": summary["operational_costs"],
            "counterfactual": {
                "transaction_count": len(summary["counterfactual"]["transaction_impacts"]),
                "material_impact_transaction_count": summary["counterfactual"][
                    "material_impact_transaction_count"
                ],
                "max_transaction_impact_percent": summary["counterfactual"][
                    "max_transaction_impact_percent"
                ],
                "run_flagged": summary["counterfactual"]["run_flagged"],
            },
        },
        "artifacts": {
            "directory": output_dir.as_posix(),
            "summary": (output_dir / "summary.json").as_posix(),
            "timeline": (output_dir / "timeline.jsonl").as_posix(),
            **manifest,
        },
    }


def write_comparison(output_root: Path, rows: list[dict[str, Any]]) -> dict[str, str]:
    payload = {
        "schema_version": "1.0",
        "market": MARKET_ADDRESS,
        "interval_order": list(INTERVALS),
        "scenario_order": [name for name, _ in SCENARIOS],
        "strategy_parameter_policy": (
            "All strategy parameters use the existing defaults. Only the transaction participation "
            "cap changes between scenario reruns."
        ),
        "results": rows,
    }
    output_root.mkdir(parents=True, exist_ok=True)
    comparison_bytes = canonical_json_bytes(payload)
    comparison_path = output_root / "comparison.json"
    comparison_path.write_bytes(comparison_bytes)
    manifest_payload = {
        "comparison_sha256": hashlib.sha256(comparison_bytes).hexdigest(),
        "scenario_artifacts": [
            {
                "interval": row["interval"],
                "scenario": row["scenario"],
                "summary_sha256": row["artifacts"]["summary_sha256"],
                "timeline_sha256": row["artifacts"]["timeline_sha256"],
            }
            for row in rows
        ],
    }
    manifest_bytes = canonical_json_bytes(manifest_payload)
    (output_root / "manifest.json").write_bytes(manifest_bytes)
    return {
        "comparison_sha256": manifest_payload["comparison_sha256"],
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validation-root", type=Path, default=Path("validation_output"))
    parser.add_argument("--output-root", type=Path, default=Path("multi_regime_output"))
    parser.add_argument("--rpc-url", default=l3.DEFAULT_RPC_URL)
    args = parser.parse_args()

    try:
        rpc = RpcClient(args.rpc_url, timeout=180)
        params = market_parameters(rpc, MARKET_ADDRESS)
        price_precision = int(params["price_precision"])
        size_precision = int(params["size_precision"])
        tick_size = int(params["tick_size"])
        rows: list[dict[str, Any]] = []
        for interval_name in INTERVALS:
            validation_path = args.validation_root / interval_name / "summary.json"
            validation = verify_validation_summary(validation_path)
            require_evaluation_gates(validation)
            start_block = int(validation["interval"]["from_block"])
            end_block = int(validation["interval"]["to_block"])
            loaded = load_verified_source_events(validation_path, validation)
            if loaded is None:
                raise RuntimeError(f"{interval_name} does not contain saved verified Envio events")
            events, timestamps = loaded
            expected_count = int(validation["event_parity"]["rpc_event_count"])
            if len(events) != expected_count:
                raise RuntimeError(f"{interval_name} event count changed")
            checkpoint_book, _, _ = l3.load_checkpoint(
                rpc, MARKET_ADDRESS, start_block - 1
            )
            if start_block - 1 not in timestamps:
                timestamps.update(rpc.block_timestamps({start_block - 1}))
            verified_input = {
                "interval_name": interval_name,
                "event_source": validation["event_source"],
                "parity_status": validation["event_parity"]["status"],
                "envio_event_count": validation["event_parity"]["envio_event_count"],
                "rpc_event_count": expected_count,
                "maximum_price_level_error_raw": validation["validation"][
                    "maximum_price_level_error_raw"
                ],
                "maximum_aggregate_size_error_raw": validation["validation"][
                    "maximum_aggregate_size_error_raw"
                ],
                "maximum_midpoint_error": validation["validation"][
                    "maximum_midpoint_error"
                ],
                "historical_executed_volume_raw": validation["interval"].get(
                    "executed_volume_raw"
                ),
                "historical_executed_volume_mon": validation["interval"].get(
                    "executed_volume_mon"
                ),
            }
            for scenario_name, cap in SCENARIOS:
                print(f"running interval={interval_name} scenario={scenario_name}", flush=True)
                summary, timeline = run_strategy(
                    checkpoint_book,
                    events,
                    start_block,
                    end_block,
                    default_config(cap),
                    price_precision,
                    size_precision,
                    tick_size,
                    timestamps,
                    verified_input,
                )
                maximum_impact = Decimal(
                    str(summary["counterfactual"]["max_transaction_impact_percent"])
                )
                if cap is not None and maximum_impact > cap:
                    raise RuntimeError(
                        f"{interval_name}/{scenario_name} exceeded its transaction cap"
                    )
                scenario_dir = args.output_root / "timelines" / interval_name / scenario_name
                output_manifest = write_outputs(scenario_dir, summary, timeline)
                rows.append(
                    comparison_row(
                        interval_name,
                        scenario_name,
                        validation,
                        summary,
                        scenario_dir,
                        output_manifest,
                    )
                )
                del timeline
        hashes = write_comparison(args.output_root, rows)
    except (OSError, ValueError, RuntimeError, ReconstructionError, l3.HyperSyncError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(hashes, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
