"""Verify completed archive partitions without taking the downloader's writer lock.

Source files are read-only. Each completed partition is copied to a separate
job before the existing validator runs. An incomplete source is never repaired
or truncated here. This command makes RPC calls, but no HyperSync calls.
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from kuru_backtester.paths import ROOT
from kuru_backtester.engine.rebuild_from_chain import DEFAULT_RPC_URL, DEPLOYMENT_BLOCK, MARKET_ADDRESS, RpcClient
from kuru_backtester.history import full_history as history
from kuru_backtester.history.historical_system import HistoricalEventStore
from kuru_backtester.history.index_history import BLOCK_FIELDS, EVENT_TOPICS, LOG_FIELDS, SCHEMA_VERSION, content_hash


def completed_bounds(request: dict, progress: dict) -> tuple[int, int] | None:
    """Use telemetry to propose bounds; source cursor checks remain authoritative."""
    first, last = int(request["from_block"]), int(request["to_block"])
    size = int(request["partition_blocks"])
    if not 1 <= size <= 10_000:
        raise ValueError("Invalid archive partition size")
    downloaded = min(last, int(progress.get("downloaded_block", first - 1)))
    end = last if downloaded == last else first + max(0, (downloaded - first + 1) // size) * size - 1
    return (first, end) if end >= first else None


def source_state(directory: Path, first: int, last: int, *, state=None, event_size=None) -> dict:
    """Check durable completion without constructing a mutable staging object."""
    if state is None:
        state = json.loads((directory / "state.json").read_text(encoding="utf-8"))
    signature = state.get("signature", {})
    if (signature.get("market", "").lower() != MARKET_ADDRESS
            or signature.get("schema_version") != SCHEMA_VERSION
            or signature.get("from_block") != first or signature.get("to_block") != last
            or sorted(signature.get("topics", [])) != EVENT_TOPICS
            or not set(LOG_FIELDS).issubset(signature.get("log_fields", []))
            or not set(BLOCK_FIELDS).issubset(signature.get("block_fields", []))):
        raise ValueError("Archive identity or event fields do not match the supported market")
    if (state.get("complete") is not True or state.get("next_block") != last + 1
            or state.get("durable_page_schema") != 1):
        raise ValueError(f"Partition {first}-{last} is not durably complete; leave the download running")
    actual_size = (directory / "events.jsonl").stat().st_size if event_size is None else event_size
    if actual_size != state.get("committed_bytes"):
        raise ValueError("Archive bytes do not match the committed cursor")
    return {key: state[key] for key in ("signature", "complete", "next_block", "committed_bytes", "event_count")}


def selected_ranges(source: Path, first: int | None, last: int | None) -> list[tuple[int, int]]:
    request = json.loads((source / "job.json").read_text(encoding="utf-8"))
    if request.get("market", "").lower() != MARKET_ADDRESS:
        raise ValueError("Only the verified MON / USDC market is supported")
    if (first is None) != (last is None):
        raise ValueError("Supply both from-block and to-block")
    if first is None:
        progress = json.loads((source / "progress.json").read_text(encoding="utf-8"))
        bounds = completed_bounds(request, progress)
        if bounds is None:
            raise ValueError("No complete downloaded partition is available yet")
        first, last = bounds
    ranges = list(history.partitions(request["from_block"], request["to_block"], request["partition_blocks"]))
    selected = [(a, b) for a, b in ranges if a >= first and b <= last]
    if not selected or selected[0][0] != first or selected[-1][1] != last:
        raise ValueError("Verification bounds must match whole downloaded partitions")
    return selected


def verify(source: Path, destination: Path, manifest: Path, ranges: list[tuple[int, int]], rpc: RpcClient,
           *, continue_unsupported: bool = False, prepare_backtests: bool = False) -> dict:
    destination.mkdir(parents=True, exist_ok=True)
    locks = manifest.parent / ".cache" / "verification_locks"
    locks.mkdir(parents=True, exist_ok=True)
    with history.writer_lock(destination / "writer.lock"), history.writer_lock(locks / (manifest.name + ".lock")):
        identity = {"source_job": str(source.resolve()), "manifest": str(manifest.resolve()),
                    "ranges": [list(bounds) for bounds in ranges], "schema_version": 1}
        identity_path = destination / "job.json"
        if identity_path.exists() and json.loads(identity_path.read_text(encoding="utf-8")) != identity:
            raise ValueError("Verification job identity changed; use its original settings or a new output directory")
        history.atomic_json(identity_path, identity)
        configuration = {"market": MARKET_ADDRESS, "from_block": ranges[0][0], "to_block": ranges[-1][1],
                         "source_job": str(source), "reconstruct_only": True, "download_only": False}
        progress = history.Progress(destination / "progress.json", configuration)
        try:
            progress.update(force=True, phase="contract_preflight")
            if int(rpc.call("eth_chainId", []), 16) != 143:
                raise ValueError("RPC must use Monad Mainnet, chain ID 143")
            first = ranges[0][0]
            # Each partition checks its own contract. A known unsupported
            # first partition must not prevent later independent checkpoints.
            history.ensure_manifest(manifest, MARKET_ADDRESS, DEPLOYMENT_BLOCK)
            total = 0
            previous = None
            verified_ranges, blocked = [], []
            completed = 0
            for first, last in ranges:
                original = source / f"{first}-{last}"
                # Reject links to files that another writer or external path can replace.
                for path in (original, original / "events.jsonl", original / "state.json"):
                    if path.is_symlink() or not path.resolve().is_relative_to(source.resolve()):
                        raise ValueError("Archive partition must stay inside its source job")
                state = source_state(original, first, last)
                output = destination / f"{first}-{last}"
                output.mkdir(parents=True, exist_ok=True)
                progress.update(force=True, phase="copying_downloaded_partition", current_partition=[first, last],
                                l2_checks_current_partition=0)
                store = HistoricalEventStore(manifest)
                existing = store.selected_partitions(first, last)
                if existing:
                    if len(existing) != 1 or (existing[0].from_block, existing[0].to_block) != (first, last):
                        raise ValueError("Verification manifest has overlapping partition bounds")
                    summary = store.validation.verify_partition(existing[0], MARKET_ADDRESS)
                    if content_hash(original / "events.jsonl") != summary["event_input"]["source_sha256"]:
                        raise ValueError("Downloaded source changed after verification")
                    for name, field in (("checkpoint.json", "checkpoint_sha256"),
                                        ("end_checkpoint.json", "end_checkpoint_sha256"), ("candles.json", "candles_sha256")):
                        if content_hash(output / name) != summary[field]:
                            raise ValueError(f"Saved verification hash mismatch: {name}")
                else:
                    if shutil.disk_usage(destination).free < state["committed_bytes"] + 256 * 1024 * 1024:
                        raise ValueError("Not enough free disk space to copy this partition safely")
                    temporary = output / "events.copying"
                    shutil.copyfile(original / "events.jsonl", temporary)
                    if source_state(original, first, last) != state:
                        raise ValueError("Source cursor changed while copying; retry verification")
                    copied_hash = content_hash(temporary)
                    if temporary.stat().st_size != state["committed_bytes"] or content_hash(original / "events.jsonl") != copied_hash:
                        raise ValueError("Source bytes changed while copying; retry verification")
                    with temporary.open(encoding="utf-8") as rows:
                        if sum(bool(line.strip()) for line in rows) != state["event_count"]:
                            raise ValueError("Downloaded event count does not match its cursor")
                    temporary.replace(output / "events.jsonl")
                    try:
                        summary = history.validate_partition(output, MARKET_ADDRESS, first, last, rpc, progress, total, previous)
                    except history.UnsupportedHistoryError as error:
                        if not continue_unsupported:
                            raise
                        previous = None
                        issue = {"from_block": first, "to_block": last, "reason": str(error),
                                 "source_sha256": copied_hash}
                        history.atomic_json(output / "unsupported.json", issue)
                        blocked.append(issue)
                        progress.update(force=True, phase="unsupported_partition", warning=str(error),
                                        blocked_partitions=len(blocked), completed_partitions=completed,
                                        processed_partitions=completed + len(blocked), verified_ranges=verified_ranges)
                        continue
                    history.promote(manifest, output / "events.jsonl", output / "summary.json", output / "checkpoint.json")
                previous = history.VerifiedBoundary.from_result(output, summary)
                if prepare_backtests:
                    from kuru_backtester.history.backtest_readiness import publish
                    readiness = publish(output, rpc)
                    progress.action("backtest_readiness", **readiness)
                    progress.update(force=True, backtest_readiness=readiness)
                total += summary["event_input"]["record_count"]
                completed += 1
                if verified_ranges and verified_ranges[-1]["to_block"] + 1 == first:
                    verified_ranges[-1]["to_block"] = last
                else:
                    verified_ranges.append({"from_block": first, "to_block": last})
                progress.update(force=True, phase="partition_validated", downloaded_events=total,
                                reconstructed_events=total, reconstructed_block=last,
                                validated_events=total, validated_block=last,
                                l2_checks_current_partition=summary["validation"]["queue_invariant_checks"],
                                event_parity_status="pass", queue_invariant_failures=0,
                                maximum_price_level_error_raw=0, maximum_aggregate_size_error_raw=0,
                                maximum_midpoint_error=0, verified_ranges=verified_ranges,
                                completed_partitions=completed, blocked_partitions=len(blocked),
                                processed_partitions=completed + len(blocked))
            history.atomic_json(destination / "unsupported_partitions.json", blocked)
            progress.update(force=True, phase="complete", manifest=str(manifest), error=None,
                            warning=f"{len(blocked)} unsupported partitions remain unavailable" if blocked else None)
            return {"completed_partitions": completed, "blocked_partitions": len(blocked)}
        except Exception as error:
            progress.update(force=True, phase="failed", error=str(error))
            raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-job", type=Path, required=True)
    parser.add_argument("--job-root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--from-block", type=int)
    parser.add_argument("--to-block", type=int)
    parser.add_argument("--rpc-url", default=DEFAULT_RPC_URL)
    args = parser.parse_args()
    source, destination, manifest = args.source_job.resolve(), args.job_root.resolve(), args.manifest.resolve()
    if (not source.is_relative_to((ROOT / ".cache").resolve()) or not destination.is_relative_to((ROOT / ".cache").resolve())
            or source == destination or source.is_relative_to(destination) or destination.is_relative_to(source)
            or manifest.parent != ROOT or not manifest.name.startswith("full_history_verify_")):
        parser.error("Use separate .cache source/output jobs and a root full_history_verify_* manifest")
    try:
        ranges = selected_ranges(source, args.from_block, args.to_block)
    except Exception as error:
        destination.mkdir(parents=True, exist_ok=True)
        with history.writer_lock(destination / "writer.lock"):
            history.atomic_json(destination / "progress.json", {"phase": "failed", "error": str(error)})
        raise
    verify(source, destination, manifest, ranges, history.validation_rpc(args.rpc_url), prepare_backtests=True)
    print(f"Verified downloaded data: {ranges[0][0]}–{ranges[-1][1]}. Manifest: {manifest}")
    print("Supported LP partitions are checked for backtest readiness. Other runtimes remain reconstruction-only.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
