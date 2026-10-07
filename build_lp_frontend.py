#!/usr/bin/env python3
"""Build the compact frontend artifact for the validated Kuru LP slice."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from compact_replays import MAX_FRAMES, build_compact_replay, canonical_json_bytes


ROOT = Path("lp_output/relatively_active/spot_max_10_percent")
DATA = Path("frontend/public/data")
FIXTURE = Path("sdk_reference/fixtures/concentrated_liquidity_v0_0_95.json")


def build() -> dict[str, Any]:
    summary = json.loads((ROOT / "summary.json").read_text(encoding="utf-8"))
    replay = build_compact_replay(
        ROOT / "timeline.jsonl",
        summary,
        interval_name="relatively_active",
        scenario_name="spot_max_10_percent",
        maximum_frames=MAX_FRAMES,
    )
    output_name = "kuru_lp--relatively_active--spot--max_10_percent.json"
    output_bytes = canonical_json_bytes(replay)
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / output_name).write_bytes(output_bytes)
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    preview = {
        "schema_version": "1.0",
        "sdk": fixture["sdk"],
        "inputs": fixture["inputs"],
        "shapes": fixture["shapes"],
    }
    fixture_bytes = canonical_json_bytes(preview)
    fixture_name = "kuru_lp_sdk_fixtures.json"
    (DATA / fixture_name).write_bytes(fixture_bytes)
    raw_size = (ROOT / "timeline.jsonl").stat().st_size
    catalog = {
        "schema_version": "1.0",
        "default": {
            "engine": "kuru_lp",
            "interval": "relatively_active",
            "shape": "spot",
            "scenario": "max_10_percent",
        },
        "market": summary["market"],
        "sdk_reference": summary["sdk_reference"],
        "fixtures": fixture_name,
        "artifacts": [{
            "engine": "kuru_lp",
            "interval": "relatively_active",
            "shape": "spot",
            "scenario": "max_10_percent",
            "path": output_name,
            "raw_record_count": replay["compaction"]["raw_record_count"],
            "compact_frame_count": replay["compaction"]["compact_frame_count"],
            "raw_size_bytes": raw_size,
            "compact_size_bytes": len(output_bytes),
            "sha256": hashlib.sha256(output_bytes).hexdigest(),
        }],
    }
    catalog_bytes = canonical_json_bytes(catalog)
    (DATA / "lp_catalog.json").write_bytes(catalog_bytes)
    return catalog


if __name__ == "__main__":
    print(json.dumps(build(), indent=2, sort_keys=True))
