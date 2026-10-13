"""Queue completed local L3 archives through the existing RPC verifier.

Planning and file hash checks run in this worker, never in an HTTP handler.
The plan is a fixed snapshot: later downloads require another explicit start.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import time

from kuru_backtester.paths import ROOT
from kuru_backtester.engine.rebuild_from_chain import DEFAULT_RPC_URL, MARKET_ADDRESS, RpcClient
from kuru_backtester.history import full_history as history
from kuru_backtester.history import verify_download
from kuru_backtester.history.historical_system import HistoricalEventStore, ValidationLayer


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def safe_path(path: Path, root: Path) -> Path:
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"Archive path leaves the project: {path}")
    current = path
    while current != root:
        if current.is_symlink():
            raise ValueError(f"Archive path must not be a link: {path}")
        current = current.parent
    return path


def covers(ranges: list[tuple[int, int]], first: int, last: int) -> bool:
    cursor = first
    for a, b in sorted(ranges):
        if a > cursor:
            break
        if b >= cursor:
            cursor = b + 1
        if cursor > last:
            return True
    return False


def downloaded_partitions(root: Path, update) -> list[dict]:
    """Read durable states, not possibly stale UI inventory or progress counters."""
    found = []
    cache = safe_path(root / ".cache", root)
    for directory, children, files in os.walk(cache, followlinks=False):
        children[:] = sorted(name for name in children if not (Path(directory) / name).is_symlink())
        if "job.json" not in files:
            continue
        source = Path(directory)
        request = read(safe_path(source / "job.json", root))
        if request.get("market", "").lower() != MARKET_ADDRESS or "partition_blocks" not in request:
            continue
        # Recognized archive roots contain fixed partitions, not other jobs.
        children[:] = []
        # DirEntry uses directory metadata already returned by the OS. Avoid
        # several WSL/Windows metadata round trips for every absent partition.
        with os.scandir(source) as entries:
            partition_entries = {entry.name: entry for entry in entries}
        ranges = history.partitions(request["from_block"], request["to_block"], request["partition_blocks"])
        for index, (first, last) in enumerate(ranges):
            if index % 128 == 0:
                update(phase="planning", completed_download_partitions=len(found))
            # The archive root was checked above. These fixed child names need
            # only direct link checks, not repeated resolution of every ancestor
            # on the Windows/WSL filesystem for thousands of partitions.
            part = source / f"{first}-{last}"
            entry = partition_entries.get(part.name)
            if entry is None:
                continue
            if entry.is_symlink():
                raise ValueError(f"Archive partition must not be a link: {part}")
            if not entry.is_dir():
                continue
            with os.scandir(part) as entries:
                files = {entry.name: entry for entry in entries}
            if any(files[name].is_symlink() for name in ("state.json", "events.jsonl") if name in files):
                raise ValueError(f"Archive partition must not be a link: {part}")
            if "state.json" not in files:
                continue
            state = read(part / "state.json")
            if state.get("complete") is not True:
                continue
            if "events.jsonl" not in files:
                raise ValueError(f"Completed partition has no event file: {part}")
            state = verify_download.source_state(part, first, last, state=state,
                                                 event_size=files["events.jsonl"].stat().st_size)
            found.append({"source": source.relative_to(root).as_posix(), "from_block": first,
                          "to_block": last, "partition_blocks": request["partition_blocks"],
                          "event_count": state["event_count"]})
        update(phase="planning", completed_download_partitions=len(found))
    return sorted(found, key=lambda item: (item["from_block"], item["to_block"], item["source"]))


def verified_ranges(root: Path, candidates: list[dict], update, on_verified=None) -> list[tuple[int, int]]:
    """Skip only saved coverage with passing reports and intact content hashes."""
    verified = []
    validation = ValidationLayer()
    for path in sorted(root.glob("*manifest.json")):
        metadata = read(safe_path(path, root))
        if (metadata.get("market", "").lower() != MARKET_ADDRESS
                or "event_partitions" not in metadata):
            continue
        store = HistoricalEventStore(path, validation)
        for part in store.partitions:
            if not any(item["from_block"] <= part.to_block and item["to_block"] >= part.from_block for item in candidates):
                continue
            update(phase="checking_saved_hashes", checking_range=[part.from_block, part.to_block])
            # Broken saved outputs are not verified coverage. Keep the source
            # eligible for fresh checks, and record why it was not skipped.
            try:
                safe_path(part.path, root)
                safe_path(part.validation_summary, root)
                summary = validation.verify_partition(part, MARKET_ADDRESS)
                source_hash = summary["event_input"].get("source_sha256")
                # Golden reports predate the source_sha256 report field. Their
                # committed manifest binds both report and event content hashes.
                if source_hash != part.sha256 and not (source_hash is None and part.golden_fixture):
                    raise ValueError("Validation report does not bind the event hash")
                checkpoint = next(item for item in store.manifest["checkpoints"]
                                  if item["block_number"] == part.from_block - 1)
                checkpoint_path = safe_path(root / checkpoint["path"], root)
                validation.verify_file(checkpoint_path, checkpoint["sha256"])
                if read(checkpoint_path)["checkpoint_block"] != part.from_block - 1:
                    raise ValueError("Checkpoint block does not match")
                # Full-history reports also bind the end state and candles.
                for name, field in (("checkpoint.json", "checkpoint_sha256"),
                                    ("end_checkpoint.json", "end_checkpoint_sha256"), ("candles.json", "candles_sha256")):
                    if field in summary:
                        validation.verify_file(safe_path(part.validation_summary.parent / name, root), summary[field])
            except (OSError, ValueError, KeyError, RuntimeError, StopIteration) as error:
                update(warning=f"Saved range {part.from_block}-{part.to_block} needs checks: {error}")
                continue
            verified.append((part.from_block, part.to_block))
            if on_verified is not None:
                on_verified(part.validation_summary.parent)
    return verified


def build_plan(root: Path, update, on_verified=None) -> dict:
    candidates = downloaded_partitions(root, update)
    verified = verified_ranges(root, candidates, update, on_verified)
    selected, groups = [], []
    skipped_verified = skipped_duplicate = 0
    for item in candidates:
        first, last = item["from_block"], item["to_block"]
        if covers(verified, first, last):
            skipped_verified += 1
            continue
        if covers(selected, first, last):
            skipped_duplicate += 1
            continue
        selected.append((first, last))
        if groups and groups[-1]["source"] == item["source"] and groups[-1]["ranges"][-1][1] + 1 == first:
            groups[-1]["ranges"].append([first, last])
        else:
            groups.append({"source": item["source"], "partition_blocks": item["partition_blocks"], "ranges": [[first, last]]})
    return {"schema_version": 1, "market": MARKET_ADDRESS, "groups": groups,
            "total_partitions": len(selected), "skipped_verified": skipped_verified,
            "skipped_duplicate": skipped_duplicate, "completed_download_partitions": len(candidates)}


def run(root: Path, destination: Path, rpc: RpcClient, *, prepare_backtests: bool = False) -> None:
    root, destination = root.resolve(), destination.resolve()
    safe_path(destination, root)
    destination.mkdir(parents=True, exist_ok=True)
    queue_root = root / ".cache/data_suite"
    queue_root.mkdir(parents=True, exist_ok=True)
    value = {"phase": "planning", "completed_partitions": 0, "blocked_partitions": 0,
             "processed_partitions": 0, "total_partitions": 0, "error": None}

    def update(**fields):
        value.update(fields, updated_at=time.time())
        history.atomic_json(destination / "progress.json", value)

    try:
        with history.writer_lock(queue_root / "verify_all.lock"):
            update()
            def prepare_saved(directory):
                from kuru_backtester.history.backtest_readiness import publish
                update(backtest_readiness=publish(directory, rpc, root))
            plan = build_plan(root, update, prepare_saved) if prepare_backtests else build_plan(root, update)
            history.atomic_json(destination / "plan.json", plan)
            update(**{key: val for key, val in plan.items() if key != "groups"}, checking_range=None)
            for group in plan["groups"]:
                identity = json.dumps(group, sort_keys=True).encode()
                job_id = hashlib.sha256(b"verify-all-v1:" + identity).hexdigest()[:16]
                output = safe_path(queue_root / "verifications" / job_id, root)
                manifest = safe_path(root / f"full_history_verify_{job_id}_manifest.json", root)
                history.atomic_json(output / "request.json", {"market": MARKET_ADDRESS, "source": group["source"],
                                    "from_block": group["ranges"][0][0], "to_block": group["ranges"][-1][1]})
                update(phase="verifying", current_job_id=job_id, current_from_block=group["ranges"][0][0],
                       current_to_block=group["ranges"][-1][1], current_partition_blocks=group["partition_blocks"],
                       current_partition_count=len(group["ranges"]))
                result = verify_download.verify(root / group["source"], output, manifest,
                                       [tuple(bounds) for bounds in group["ranges"]], rpc, continue_unsupported=True,
                                       prepare_backtests=prepare_backtests)
                update(completed_partitions=value["completed_partitions"] + result["completed_partitions"],
                       blocked_partitions=value["blocked_partitions"] + result["blocked_partitions"],
                       processed_partitions=value["processed_partitions"] + len(group["ranges"]), current_job_id=None)
            update(phase="complete", current_job_id=None, error=None,
                   warning=f'{value["blocked_partitions"]} unsupported partitions remain unavailable' if value["blocked_partitions"] else None)
    except Exception as error:
        update(phase="failed", error=str(error))
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-root", required=True, type=Path)
    parser.add_argument("--rpc-url", default=DEFAULT_RPC_URL)
    args = parser.parse_args()
    if not args.job_root.resolve().is_relative_to((ROOT / ".cache/data_suite/verification_queues").resolve()):
        parser.error("Use a data-suite verification queue directory")
    run(ROOT, args.job_root, history.validation_rpc(args.rpc_url), prepare_backtests=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
