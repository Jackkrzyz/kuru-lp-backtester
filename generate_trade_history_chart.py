#!/usr/bin/env python3
"""Build a bounded chart from local Trade events only.

The chart is for range selection. It does not claim L3 backtest coverage.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
DEFAULT_MANIFEST = ROOT / "historical_index_manifest.json"
DEFAULT_OUTPUT = ROOT / "frontend" / "public" / "data" / "trade_history_chart.json"


def downsample_trades(rows: list[dict[str, Any]], maximum_points: int) -> list[dict[str, Any]]:
    """Keep deterministic first, low, high, and last points per block bucket."""
    if maximum_points < 4:
        raise ValueError("maximum_points must be at least 4")
    if len(rows) <= maximum_points:
        return rows
    bucket_count = max(1, maximum_points // 4)
    first_block = int(rows[0]["block"])
    last_block = int(rows[-1]["block"])
    width = max(1, (last_block - first_block + bucket_count) // bucket_count)
    buckets: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        key = (int(row["block"]) - first_block) // width
        buckets.setdefault(key, []).append(row)
    kept: dict[tuple[int, int, int], dict[str, Any]] = {}
    for values in buckets.values():
        choices = (
            values[0],
            min(values, key=lambda item: (item["price"], item["block"], item["timestamp"])),
            max(values, key=lambda item: (item["price"], -item["block"], -item["timestamp"])),
            values[-1],
        )
        for item in choices:
            kept[(item["block"], item["timestamp"], item["ordinal"])] = item
    return sorted(kept.values(), key=lambda item: (item["block"], item["ordinal"]))[:maximum_points]


def build_chart(manifest_path: Path, output_path: Path, maximum_points: int) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    root = manifest_path.resolve().parent
    scope = manifest["trade_chart_coverage"]
    source = (root / scope["path"]).resolve()
    rows: list[dict[str, Any]] = []
    with source.open(encoding="utf-8") as stream:
        for ordinal, line in enumerate(stream):
            if not line.strip():
                continue
            item = json.loads(line)
            if item.get("name") != "Trade" or item.get("trade_price") is None:
                continue
            rows.append({
                "block": int(item["block_number"]),
                "timestamp": int(item["block_timestamp"]),
                "price": float(item["trade_price"]),
                "volume_raw": str(item.get("trade_size", 0)),
                "ordinal": ordinal,
            })
    rows.sort(key=lambda item: (item["block"], item["ordinal"]))
    points = downsample_trades(rows, maximum_points)
    for item in points:
        item.pop("ordinal", None)
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    payload = {
        "schema_version": "1.0",
        "market": manifest["market"],
        "complete_lifetime": False,
        "notice": (
            "This local chart contains Trade events only. A chart range is "
            "backtestable only when it is inside one validated L3 coverage segment."
        ),
        "trade_only_coverage": {
            "from_block": int(scope["from_block"]),
            "to_block": int(scope["to_block"]),
            "source_sha256": source_hash,
            "trade_count": len(rows),
        },
        "validated_l3_coverage": [
            {
                "from_block": int(item["from_block"]),
                "to_block": int(item["to_block"]),
                "complete_validated_l3": True,
            }
            for item in manifest["event_partitions"]
        ],
        "point_count": len(points),
        "points": points,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--maximum-points", type=int, default=1_000)
    args = parser.parse_args()
    result = build_chart(args.manifest, args.output, args.maximum_points)
    print(
        f"Wrote {result['point_count']} Trade chart points to {args.output}. "
        "This file does not add L3 coverage."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
