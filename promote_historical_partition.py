#!/usr/bin/env python3
"""Add one fully validated local partition and checkpoint to the index manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import strategy_vertical_slice as strategy


ROOT = Path(__file__).resolve().parent


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def relative_project_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(ROOT).as_posix()
    except ValueError as error:
        raise ValueError(f"validated file must stay inside {ROOT}") from error


def event_count(path: Path) -> int:
    with path.open(encoding="utf-8") as source:
        return sum(1 for line in source if line.strip())


def promote(
    manifest_path: Path,
    event_path: Path,
    validation_path: Path,
    checkpoint_path: Path,
) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    validation = strategy.verify_validation_summary(validation_path)
    interval = validation["interval"]
    first = int(interval["from_block"])
    last = int(interval["to_block"])
    market = str(validation.get("market", interval.get("market", ""))).lower()
    if market != str(manifest["market"]).lower():
        raise ValueError("validation market does not match the historical manifest")
    count = event_count(event_path)
    if count != int(validation["event_input"]["record_count"]):
        raise ValueError("event count does not match the passing validation report")
    expected_event_hash = validation["event_input"].get("source_sha256")
    actual_event_hash = file_hash(event_path)
    if not expected_event_hash:
        raise ValueError("validation report does not bind the indexed event file hash")
    if actual_event_hash != expected_event_hash:
        raise ValueError("event file hash does not match the passing validation report")
    for item in manifest["event_partitions"]:
        if not (last < int(item["from_block"]) or first > int(item["to_block"])):
            raise ValueError("validated event partition overlaps existing coverage")

    checkpoint_bytes = checkpoint_path.read_bytes()
    checkpoint = json.loads(checkpoint_bytes)
    checkpoint_block = int(checkpoint["checkpoint_block"])
    if checkpoint_block != first - 1:
        raise ValueError("checkpoint must be from_block - 1")
    if str(checkpoint.get("market", market)).lower() != market:
        raise ValueError("checkpoint market does not match the partition market")

    manifest["event_partitions"].append({
        "from_block": first,
        "to_block": last,
        "event_count": count,
        "path": relative_project_path(event_path),
        "sha256": actual_event_hash,
        "validation_summary": relative_project_path(validation_path),
        "validation_summary_sha256": file_hash(validation_path),
    })
    manifest["event_partitions"].sort(key=lambda item: int(item["from_block"]))
    manifest["checkpoints"].append({
        "block_number": checkpoint_block,
        "block_timestamp": int(checkpoint["checkpoint_timestamp"]),
        "path": relative_project_path(checkpoint_path),
        "sha256": hashlib.sha256(checkpoint_bytes).hexdigest(),
    })
    manifest["checkpoints"].sort(key=lambda item: int(item["block_number"]))
    temporary = manifest_path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(manifest, indent=2, sort_keys=False) + "\n", encoding="utf-8"
    )
    temporary.replace(manifest_path)
    return {
        "from_block": first,
        "to_block": last,
        "event_count": count,
        "event_sha256": actual_event_hash,
        "checkpoint_block": checkpoint_block,
        "checkpoint_sha256": hashlib.sha256(checkpoint_bytes).hexdigest(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=ROOT / "historical_index_manifest.json")
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--validation-summary", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    args = parser.parse_args()
    result = promote(args.manifest, args.events, args.validation_summary, args.checkpoint)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
