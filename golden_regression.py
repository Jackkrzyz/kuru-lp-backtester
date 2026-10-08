#!/usr/bin/env python3
"""Compare generalized block-range runs with all named golden LP fixtures."""

from __future__ import annotations

import json
from pathlib import Path

from backtest_service import BacktestService, canonical_sdk_grid


ROOT = Path(__file__).resolve().parent
MARKET = "0x065c9d28e428a0db40191a54d33d5b7c71a9c394"
COMPARE_KEYS = (
    "generated_initial_bid_count", "generated_initial_ask_count",
    "starting_mon", "final_mon", "starting_usdc", "final_usdc", "fills",
    "flip_orders_created", "turnover", "gross_pnl", "net_pnl", "hold_pnl",
    "excess_pnl", "maximum_drawdown", "maker_fees", "action_fees",
    "earned_flip_spread", "results", "semantic_timeline_sha256",
)


def base_request() -> dict:
    return {
        "engine": "kuru_lp",
        "market": MARKET,
        "market_symbol": "MON / USDC",
        "shape": "spot",
        "range_mode": "percentage",
        "lower_range_percent": "3",
        "upper_range_percent": "3",
        "fee_percent": "0.30",
        "mon_amount": "6000",
        "usdc_amount": "180",
        "amount_driver": "both",
        "inclusion_delay_blocks": 1,
        "max_transaction_participation_percent": 10,
        "maker_fee_bps": 0,
        "fixed_action_fee": 0,
        "material_impact_threshold_percent": 25,
    }


def with_grid(service: BacktestService, request: dict, data: object) -> dict:
    complete = dict(request)
    _, sdk_input = service.normalize_request(complete, data)  # type: ignore[arg-type]
    complete["order_grid"] = canonical_sdk_grid(sdk_input)
    return complete


def run_report(output: Path) -> dict:
    service = BacktestService()
    rows = []
    for metadata in service.interval_metadata()["intervals"]:
        name = metadata["name"]
        legacy_data = service.load_interval(name)
        legacy_request = {**base_request(), "interval": name}
        legacy = service.run(with_grid(service, legacy_request, legacy_data))

        range_data = service.load_range(
            MARKET, int(metadata["from_block"]), int(metadata["to_block"])
        )
        range_request = {
            **base_request(),
            "from_block": int(metadata["from_block"]),
            "to_block": int(metadata["to_block"]),
        }
        generalized = service.run(with_grid(service, range_request, range_data))
        differences = [key for key in COMPARE_KEYS if legacy[key] != generalized[key]]
        if legacy["detailed_fills"]["sha256"] != generalized["detailed_fills"]["sha256"]:
            differences.append("detailed_fills.sha256")
        rows.append({
            "golden_fixture": name,
            "from_block": metadata["from_block"],
            "to_block": metadata["to_block"],
            "status": "pass" if not differences else "fail",
            "differences": differences,
            "legacy": {
                "run_id": legacy["run_id"],
                "summary_sha256": legacy["summary_sha256"],
                "timeline_sha256": legacy["timeline_sha256"],
                "semantic_timeline_sha256": legacy["semantic_timeline_sha256"],
            },
            "generalized": {
                "run_id": generalized["run_id"],
                "summary_sha256": generalized["summary_sha256"],
                "timeline_sha256": generalized["timeline_sha256"],
                "semantic_timeline_sha256": generalized["semantic_timeline_sha256"],
            },
            "fills": generalized["fills"],
            "net_pnl": generalized["net_pnl"],
        })
    report = {
        "schema_version": "1.0",
        "comparison": "named wrapper versus generalized block-range service",
        "all_passed": all(item["status"] == "pass" for item in rows),
        "fixtures": rows,
    }
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def main() -> int:
    output = ROOT / "golden_regression_report.json"
    report = run_report(output)
    for item in report["fixtures"]:
        print(
            f"{item['golden_fixture']}: {item['status']} "
            f"fills={item['fills']} pnl={item['net_pnl']}"
        )
    print(f"Report: {output}")
    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
