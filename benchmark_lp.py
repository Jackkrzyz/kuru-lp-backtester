#!/usr/bin/env python3
"""Benchmark a bounded Kuru LP event prefix with one saved workload."""

from __future__ import annotations

import argparse
import json
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

try:
    import resource
except ImportError:  # pragma: no cover - not available in Windows Python.
    resource = None

import backtest_service
import kuru_lp


DEFAULT_WORKLOAD = Path("benchmarks/large_grid_workload.json")


def memory_bytes() -> int:
    """Return the process peak resident memory on Linux."""
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * 1024 if resource else 0


def load_workload(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def run_prefix(workload: dict[str, Any], event_limit: int, *, product_mode: bool) -> dict[str, Any]:
    stages: dict[str, float] = {}
    wall_start = time.perf_counter()
    cpu_start = time.process_time()

    service = backtest_service.BacktestService()
    stage = time.perf_counter()
    data = service.load_interval(workload["interval"]["name"])
    stages["load_and_parse_events_seconds"] = time.perf_counter() - stage

    request = {**workload["configuration"], "market": workload["market"], "interval": workload["interval"]["name"]}
    normalized, sdk_input = service.normalize_request(request, data)
    stage = time.perf_counter()
    grid = backtest_service.normalize_grid(backtest_service.canonical_sdk_grid(sdk_input))
    stages["sdk_grid_seconds"] = time.perf_counter() - stage

    positions = [
        kuru_lp.LPPosition(
            side=item["side"],
            price=int(item["price_raw"]),
            liquidity=int(item["size_raw"]),
            flip_price=int(item["flip_price_raw"]),
        )
        for item in grid["orders"]
    ]
    distribution = kuru_lp.LPDistribution(
        bids=tuple(item for item in positions if item.side == "buy"),
        asks=tuple(item for item in positions if item.side == "sell"),
        quote_liquidity=int(grid["required_usdc_raw"]),
        base_liquidity=int(grid["required_mon_raw"]),
        min_size_error=bool(grid["min_size_error"]),
    )
    cap = normalized["max_transaction_participation_percent"]
    config = kuru_lp.LPConfig(
        shape=normalized["shape"],
        minimum_price_raw=int(grid["minimum_price_raw"]),
        maximum_price_raw=int(grid["maximum_price_raw"]),
        fee_pps=int(grid["fee_pps"]),
        quote_deposit_raw=backtest_service.raw_amount(normalized["usdc_amount"], backtest_service.QUOTE_DECIMALS),
        base_deposit_raw=backtest_service.raw_amount(normalized["mon_amount"], backtest_service.BASE_DECIMALS),
        maker_fee_bps=Decimal(normalized["maker_fee_bps"]),
        fixed_action_fee=Decimal(normalized["fixed_action_fee"]),
        inclusion_delay_blocks=int(normalized["inclusion_delay_blocks"]),
        max_transaction_participation_percent=None if cap is None else Decimal(cap),
        material_impact_threshold_percent=Decimal(normalized["material_impact_threshold_percent"]),
    )
    events = data.events[:event_limit]
    if not events:
        raise ValueError("event limit must select at least one event")
    interval = data.validation["interval"]
    stage = time.perf_counter()
    summary, timeline = kuru_lp.run_lp(
        data.book,
        events,
        int(interval["from_block"]),
        events[-1].block_number,
        config,
        distribution,
        data.parameters["price_precision"],
        data.parameters["size_precision"],
        data.timestamps,
        {"benchmark": workload["name"]},
        timeline_stride=80,
        audit_invariants=not product_mode,
    )
    stages["simulation_and_timeline_seconds"] = time.perf_counter() - stage
    stage = time.perf_counter()
    encoded = backtest_service.canonical_bytes({"summary": summary, "timeline": timeline})
    stages["serialization_seconds"] = time.perf_counter() - stage
    wall = time.perf_counter() - wall_start
    cpu = time.process_time() - cpu_start
    return {
        "schema_version": "1.0",
        "workload": workload["name"],
        "event_limit": len(events),
        "generated_order_count": len(positions),
        "mode": "product" if product_mode else "audit",
        "wall_seconds": wall,
        "cpu_seconds": cpu,
        "waiting_seconds": max(0.0, wall - cpu),
        "events_per_second": len(events) / wall,
        "peak_rss_bytes": memory_bytes(),
        "retained_timeline_records": len(timeline),
        "full_timeline_records": summary["timeline"]["full_record_count"],
        "serialized_bytes": len(encoded),
        "fill_count": summary["metrics"]["fill_count"],
        "stages": stages,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workload", type=Path, default=DEFAULT_WORKLOAD)
    parser.add_argument("--event-limit", type=int, required=True)
    parser.add_argument("--interval", choices=("relatively_quiet", "relatively_active", "directional"))
    parser.add_argument("--shape", choices=("spot", "curve", "bid_ask"))
    parser.add_argument("--minimum-price")
    parser.add_argument("--maximum-price")
    parser.add_argument("--fee-percent")
    parser.add_argument("--mon-amount")
    parser.add_argument("--usdc-amount")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--product-mode", action="store_true")
    args = parser.parse_args()
    workload = load_workload(args.workload)
    if args.interval:
        workload["interval"]["name"] = args.interval
    for argument, field in (
        (args.shape, "shape"),
        (args.minimum_price, "minimum_price"),
        (args.maximum_price, "maximum_price"),
        (args.fee_percent, "fee_percent"),
        (args.mon_amount, "mon_amount"),
        (args.usdc_amount, "usdc_amount"),
    ):
        if argument is not None:
            workload["configuration"][field] = argument
    result = run_prefix(workload, args.event_limit, product_mode=args.product_mode)
    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
