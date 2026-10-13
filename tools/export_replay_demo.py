"""Export checked saved LP runs and copied replay assets. No strategy execution.

This is a local release tool. It is outside the static deployment directory.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from kuru_backtester.paths import ROOT
from kuru_backtester.server.saved_backtests import SavedBacktests
from kuru_backtester.server.backtest_service import canonical_bytes, sha256_value, ENGINE_VERSION
from kuru_backtester.history.historical_system import HistoricalEventStore, CheckpointStore

DEFAULT_RUNS = (
    "lp-3dcbac7069fbb06a80b8",
    "lp-2d41d3f8be336bf585c6",
    "lp-c7e25b4ab5e0a3abf683",
)
SUMMARY_KEYS = (
    "run_id", "engine", "engine_version", "normalized_configuration", "interval", "results",
    "generated_initial_bid_count", "generated_initial_ask_count", "starting_mon", "final_mon",
    "starting_usdc", "final_usdc", "fills", "flip_orders_created", "turnover", "gross_pnl",
    "net_pnl", "hold_pnl", "excess_pnl", "maximum_drawdown", "maker_fees", "action_fees",
    "earned_flip_spread", "counterfactual", "initial_order_grid", "grid_validation",
    "summary_sha256", "timeline_sha256", "timeline_metadata",
)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def renderer_source(source: str) -> str:
    """Copy the current Canvas renderer and playback without API/form code."""
    sections = (
        ("function prepareCanvas(", "function syncPriceSelection("),
        ("function activeOrdersAt(", "// Restore the original cached Canvas replay renderer."),
        ("function chartScale(", "async function setMode("),
    )
    chunks = [source[source.index(start):source.index(end)] for start, end in sections]
    return ("/* Copied from frontend/app.js by tools/export_replay_demo.py. */\n" + "\n".join(chunks)).rstrip() + "\n"


def verify_run(store: SavedBacktests, run_id: str) -> tuple[dict, dict]:
    run = store.load(run_id)  # Checks complete marker, summary file, and timeline hashes.
    if run["engine_version"] != ENGINE_VERSION:
        raise ValueError(f"{run_id}: use the current contract-corrected engine")
    directory = store._directory(run_id)
    complete = store._read(directory, "complete.json")
    fills = store._read(directory, "fills.json")
    if sha256_value(fills) != complete["fills_file_sha256"]:
        raise ValueError(f"{run_id}: detailed-fill hash mismatch")
    summary = {key: value for key, value in run.items()
               if key not in {"compact_timeline", "performance", "run_id", "summary_sha256", "timeline_sha256"}}
    if sha256_value(summary) != run["summary_sha256"]:
        raise ValueError(f"{run_id}: deterministic summary hash mismatch")
    if complete["identity_sha256"][:20] != run_id.removeprefix("lp-"):
        raise ValueError(f"{run_id}: run identity mismatch")
    frames = run["compact_timeline"]
    if len(frames) > 1500:
        raise ValueError(f"{run_id}: replay exceeds the compact frame limit")
    if any(frames[-1][frame_key] != run[summary_key] for frame_key, summary_key in (
        ("cash", "final_usdc"), ("inventory", "final_mon"), ("cumulative_pnl", "net_pnl"),
        ("hold_baseline_pnl", "hold_pnl"))):
        raise ValueError(f"{run_id}: final replay state differs from summary")
    if sum(len(frame["fills"]) for frame in frames) != run["fills"] or len(fills) != run["fills"]:
        raise ValueError(f"{run_id}: fill count differs from saved summary")
    return run, complete


def export(run_ids=DEFAULT_RUNS, root=ROOT, destination=None) -> dict:
    root = Path(root).resolve()
    destination = Path(destination or root / "replay-demo").resolve()
    if not destination.is_relative_to(root) or destination == root:
        raise ValueError("Demo output must be a separate directory inside the project")
    store = SavedBacktests(root / ".cache/backtest_runs")
    history = HistoricalEventStore(root / "historical_index_manifest.json")
    checkpoints = CheckpointStore(root / "historical_index_manifest.json", history.validation)
    prepared, records = [], []
    # Preflight every selected run before writing any public recordings.
    for run_id in run_ids:
        run, complete = verify_run(store, run_id)
        config = run["normalized_configuration"]
        first, last = run["interval"]["from_block"], run["interval"]["to_block"]
        if config.get("from_block", first) != first or config.get("to_block", last) != last:
            raise ValueError(f"{run_id}: configuration and interval differ")
        history.require_coverage(config["market"], first, last)
        checkpoint = checkpoints.latest_at_or_before(config["market"], first - 1)
        provenance = []
        for part in history.selected_partitions(first, last):
            history.validation.verify_partition(part, config["market"])
            provenance.append({"from_block": part.from_block, "to_block": part.to_block,
                               "event_sha256": part.sha256, "validation_sha256": part.validation_summary_sha256})
        payload = {key: run[key] for key in SUMMARY_KEYS}
        payload["compact_timeline"] = run["compact_timeline"]
        data = canonical_bytes(payload) + b"\n"
        if len(data) > 3_000_000:
            raise ValueError(f"{run_id}: recording is too large for this limited demo")
        label = {"spot": "Spot", "curve": "Curve", "bid_ask": "Bid-Ask"}[config["shape"]]
        label += f' · {config["fee_percent"]}% spread'
        if run["counterfactual"]["run_flagged"]:
            label += " · impact warning"
        record = {"run_id": run_id, "label": label, "file": f"data/{run_id}.json",
                  "sha256": digest(data), "bytes": len(data), "configuration": config,
                  "interval": run["interval"], "results": run["results"],
                  "frames": len(run["compact_timeline"]), "fills": run["fills"],
                  "summary_sha256": run["summary_sha256"], "timeline_sha256": run["timeline_sha256"],
                  "verification": {"summary_file_sha256": complete["summary_file_sha256"],
                    "fills_sha256": complete["fills_file_sha256"], "history": provenance,
                    "checkpoint_sha256": checkpoint.sha256, "status": "pass"}}
        prepared.append((destination / record["file"], data))
        records.append(record)
    if not records:
        raise ValueError("No valid completed local runs are available; demo export stopped")
    for path, data in prepared:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    assets = destination / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    app_source = (root / "frontend/app.js").read_text(encoding="utf-8")
    (assets / "replay-renderer.js").write_text(renderer_source(app_source), encoding="utf-8")
    for source, target in ((root / "frontend/styles.css", assets / "base.css"),
                           (root / "LICENSE", destination / "LICENSE")):
        target.write_bytes(source.read_bytes())
    catalog = {"schema_version": 1, "engine_version": ENGINE_VERSION,
               "default_run_id": records[0]["run_id"], "recordings": records,
               "renderer_source_sha256": digest(app_source.encode())}
    (destination / "data/catalog.json").write_bytes(canonical_bytes(catalog) + b"\n")
    return catalog


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", action="append", help="Exact saved ID; no failed-export fallback")
    args = parser.parse_args()
    result = export(args.run_id or DEFAULT_RUNS)
    for record in result["recordings"]:
        print(f'{record["run_id"]}: verified · {record["frames"]} frames · {record["fills"]} fills · '
              f'P&L {record["results"]["strategy_pnl"]} USDC · {record["bytes"]} bytes')
