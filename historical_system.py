#!/usr/bin/env python3
"""General local historical reconstruction and Kuru LP backtest components.

This module composes the verified reconstruction and LP engines. It does not
replace their queue, fill, flip, or accounting rules.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

import kuru_lp
import l3_vertical_slice as l3
import strategy_vertical_slice as strategy


ROOT = Path(__file__).resolve().parent
DEFAULT_MANIFEST = ROOT / "historical_index_manifest.json"


class HistoricalRangeError(ValueError):
    """Report a range that local validated data cannot replay."""

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class EventPartition:
    from_block: int
    to_block: int
    event_count: int
    path: Path
    sha256: str
    validation_summary: Path
    validation_summary_sha256: str
    golden_fixture: str | None = None


@dataclass(frozen=True)
class StoredCheckpoint:
    block_number: int
    block_timestamp: int
    path: Path
    sha256: str
    golden_fixture: str | None = None


@dataclass(frozen=True)
class ReconstructedStart:
    market: str
    from_block: int
    state_block: int
    state_timestamp: int
    timestamp_source_block: int
    checkpoint: StoredCheckpoint
    book: l3.L3Book
    parameters: dict[str, int]
    applied_event_count: int


class ValidationLayer:
    """Verify saved parity reports and every local content hash."""

    def __init__(self) -> None:
        self._verified_files: dict[tuple[Path, str], tuple[int, int, int, int]] = {}

    def verify_file(self, path: Path, expected_sha256: str) -> None:
        key = (path, expected_sha256)
        if not path.is_file():
            raise HistoricalRangeError(f"indexed file is missing: {path}")
        stat = path.stat()
        signature = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino)
        if self._verified_files.get(key) == signature:
            return
        actual = sha256_file(path)
        if actual != expected_sha256:
            raise HistoricalRangeError(
                f"indexed file hash does not match: {path}",
                details={"expected_sha256": expected_sha256, "actual_sha256": actual},
            )
        self._verified_files[key] = signature

    def verify_partition(self, partition: EventPartition, market: str | None = None) -> dict[str, Any]:
        self.verify_file(partition.path, partition.sha256)
        self.verify_file(
            partition.validation_summary, partition.validation_summary_sha256
        )
        summary = strategy.verify_validation_summary(partition.validation_summary)
        interval = summary["interval"]
        if market is not None and str(summary.get("market", interval.get("market", ""))).lower() != market.lower():
            raise HistoricalRangeError("validation market does not match the indexed market")
        if (
            int(interval["from_block"]) != partition.from_block
            or int(interval["to_block"]) != partition.to_block
            or int(summary["event_input"]["record_count"]) != partition.event_count
        ):
            raise HistoricalRangeError(
                f"validation metadata does not match partition {partition.path}"
            )
        return summary

    def verify_checkpoint(self, checkpoint: StoredCheckpoint) -> dict[str, Any]:
        self.verify_file(checkpoint.path, checkpoint.sha256)
        payload = json.loads(checkpoint.path.read_text(encoding="utf-8"))
        if int(payload["checkpoint_block"]) != checkpoint.block_number:
            raise HistoricalRangeError("checkpoint block does not match its manifest entry")
        if int(payload["checkpoint_timestamp"]) != checkpoint.block_timestamp:
            raise HistoricalRangeError("checkpoint timestamp does not match its manifest entry")
        return payload


class HistoricalEventStore:
    """Read canonical, hash-verified event partitions by block range."""

    def __init__(
        self,
        manifest_path: Path = DEFAULT_MANIFEST,
        validation: ValidationLayer | None = None,
    ) -> None:
        self.manifest_path = manifest_path.resolve()
        self.project_root = self.manifest_path.parent
        self.manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        self.market = str(self.manifest["market"]).lower()
        self.validation = validation or ValidationLayer()
        self.partitions = tuple(sorted(
            (self._partition(item) for item in self.manifest["event_partitions"]),
            key=lambda item: item.from_block,
        ))
        self._range_counts: dict[tuple[int, int], int] = {}
        self._range_timestamps: dict[tuple[int, int], dict[int, int]] = {}
        self._validate_manifest_ranges()

    def _path(self, value: str) -> Path:
        path = (self.project_root / value).resolve()
        if self.project_root not in path.parents and path != self.project_root:
            raise HistoricalRangeError("historical index path leaves the project root")
        return path

    def _partition(self, item: dict[str, Any]) -> EventPartition:
        return EventPartition(
            from_block=int(item["from_block"]),
            to_block=int(item["to_block"]),
            event_count=int(item["event_count"]),
            path=self._path(str(item["path"])),
            sha256=str(item["sha256"]),
            validation_summary=self._path(str(item["validation_summary"])),
            validation_summary_sha256=str(item["validation_summary_sha256"]),
            golden_fixture=item.get("golden_fixture"),
        )
    def _validate_manifest_ranges(self) -> None:
        previous: EventPartition | None = None
        for partition in self.partitions:
            if partition.from_block > partition.to_block:
                raise HistoricalRangeError("event partition has an invalid block range")
            if previous is not None and partition.from_block <= previous.to_block:
                raise HistoricalRangeError("event partitions overlap")
            previous = partition

    def coverage(self) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for partition in self.partitions:
            if output and partition.from_block == output[-1]["to_block"] + 1:
                output[-1]["to_block"] = partition.to_block
                output[-1]["event_count"] += partition.event_count
                output[-1]["partition_count"] += 1
            else:
                output.append({
                    "from_block": partition.from_block,
                    "to_block": partition.to_block,
                    "event_count": partition.event_count,
                    "partition_count": 1,
                    "complete_validated_l3": True,
                })
        return output

    def covers(self, from_block: int, to_block: int) -> bool:
        return any(
            from_block >= item["from_block"] and to_block <= item["to_block"]
            for item in self.coverage()
        )

    def require_coverage(self, market: str, from_block: int, to_block: int) -> None:
        if market.lower() != self.market:
            raise HistoricalRangeError(
                "market is not present in the local historical index",
                details={"indexed_market": self.market},
            )
        if from_block > to_block:
            raise HistoricalRangeError("from_block must not exceed to_block")
        if not self.covers(from_block, to_block):
            raise HistoricalRangeError(
                "requested range does not have continuous validated L3 coverage",
                details={"coverage": self.coverage()},
            )

    def selected_partitions(self, from_block: int, to_block: int) -> tuple[EventPartition, ...]:
        return tuple(
            item for item in self.partitions
            if item.to_block >= from_block and item.from_block <= to_block
        )

    def iter_events(self, market: str, from_block: int, to_block: int) -> Iterator[l3.ReplayEvent]:
        self.require_coverage(market, from_block, to_block)
        prior_position: tuple[int, int, int] | None = None
        for partition in self.selected_partitions(from_block, to_block):
            self.validation.verify_partition(partition, self.market)
            with partition.path.open("r", encoding="utf-8") as source:
                for line_number, line in enumerate(source, start=1):
                    if not line.strip():
                        continue
                    raw = json.loads(line)
                    block = int(raw["block_number"])
                    if block < from_block:
                        continue
                    if block > to_block:
                        break
                    event = l3.decode_event(raw)
                    position = (
                        event.block_number,
                        event.transaction_index,
                        event.log_index,
                    )
                    if prior_position is not None and position <= prior_position:
                        raise HistoricalRangeError(
                            f"event index is not strictly ordered at {partition.path}:{line_number}"
                        )
                    prior_position = position
                    yield event

    def event_count(self, market: str, from_block: int, to_block: int) -> int:
        key = (from_block, to_block)
        self.timestamps(market, from_block, to_block)
        return self._range_counts[key]

    def timestamps(self, market: str, from_block: int, to_block: int) -> dict[int, int]:
        self.require_coverage(market, from_block, to_block)
        key = (from_block, to_block)
        partitions = self.selected_partitions(from_block, to_block)
        for partition in partitions:
            self.validation.verify_partition(partition, self.market)
        if key in self._range_timestamps:
            return dict(self._range_timestamps[key])
        output: dict[int, int] = {}
        count = 0
        for partition in partitions:
            with partition.path.open("r", encoding="utf-8") as source:
                for line in source:
                    if not line.strip():
                        continue
                    raw = json.loads(line)
                    block = int(raw["block_number"])
                    if block < from_block:
                        continue
                    if block > to_block:
                        break
                    timestamp = int(raw["block_timestamp"])
                    if block in output and output[block] != timestamp:
                        raise HistoricalRangeError(
                            f"conflicting timestamps for indexed block {block}"
                        )
                    output[block] = timestamp
                    count += 1
        if len(self._range_timestamps) >= 128:
            self._range_timestamps.clear()
            self._range_counts.clear()
        self._range_counts[key] = count
        self._range_timestamps[key] = output
        return dict(output)

    def content_identity(self, from_block: int, to_block: int) -> list[dict[str, Any]]:
        return [
            {
                "from_block": item.from_block,
                "to_block": item.to_block,
                "sha256": item.sha256,
                "validation_summary_sha256": item.validation_summary_sha256,
            }
            for item in self.selected_partitions(from_block, to_block)
        ]


class CheckpointStore:
    """Locate and verify the newest L3 checkpoint at or before one block."""

    def __init__(
        self,
        manifest_path: Path = DEFAULT_MANIFEST,
        validation: ValidationLayer | None = None,
    ) -> None:
        self.manifest_path = manifest_path.resolve()
        self.project_root = self.manifest_path.parent
        self.manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        self.market = str(self.manifest["market"]).lower()
        self.validation = validation or ValidationLayer()
        self.checkpoints = tuple(sorted(
            (
                StoredCheckpoint(
                    block_number=int(item["block_number"]),
                    block_timestamp=int(item["block_timestamp"]),
                    path=(self.project_root / str(item["path"])).resolve(),
                    sha256=str(item["sha256"]),
                    golden_fixture=item.get("golden_fixture"),
                )
                for item in self.manifest["checkpoints"]
            ),
            key=lambda item: item.block_number,
        ))
        for checkpoint in self.checkpoints:
            if self.project_root not in checkpoint.path.parents:
                raise HistoricalRangeError("checkpoint path leaves the project root")

    def latest_at_or_before(self, market: str, block_number: int) -> StoredCheckpoint:
        if market.lower() != self.market:
            raise HistoricalRangeError("market is not present in the checkpoint store")
        matches = [item for item in self.checkpoints if item.block_number <= block_number]
        if not matches:
            raise HistoricalRangeError(
                "no verified checkpoint is available at or before the requested block",
                details={"requested_block": block_number},
            )
        checkpoint = matches[-1]
        payload = self.validation.verify_checkpoint(checkpoint)
        if str(payload.get("market", "")).lower() != self.market:
            raise HistoricalRangeError("checkpoint market does not match the indexed market")
        return checkpoint


def deserialize_book(payload: dict[str, Any]) -> l3.L3Book:
    """Restore exact verified FIFO links and aggregate level totals."""
    book = l3.L3Book()
    book.orders = {
        int(item["order_id"]): l3.BookOrder(
            order_id=int(item["order_id"]),
            owner=str(item["owner"]),
            size=int(item["size"]),
            prev=int(item["prev"]),
            next=int(item["next"]),
            flipped_id=int(item["flipped_id"]),
            price=int(item["price"]),
            flipped_price=int(item["flipped_price"]),
            is_buy=bool(item["is_buy"]),
        )
        for item in payload["orders"]
    }
    book.levels = {
        (bool(item["is_buy"]), int(item["price"])): l3.PriceQueue(
            head=int(item["head"]),
            tail=int(item["tail"]),
            total_size=int(item["total_size"]),
        )
        for item in payload["levels"]
    }
    book.check_invariants()
    return book


class ReconstructionEngine:
    """Rebuild the market state immediately before an arbitrary start block."""

    def __init__(self, events: HistoricalEventStore, checkpoints: CheckpointStore) -> None:
        self.events = events
        self.checkpoints = checkpoints

    def reconstruct_start(self, market: str, from_block: int) -> ReconstructedStart:
        state_block = from_block - 1
        checkpoint = self.checkpoints.latest_at_or_before(market, state_block)
        payload = self.checkpoints.validation.verify_checkpoint(checkpoint)
        book = deserialize_book(payload["book"])
        parameters = {key: int(value) for key, value in payload["parameters"].items()}
        applied = 0
        timestamp = checkpoint.block_timestamp
        timestamp_source_block = checkpoint.block_number
        if checkpoint.block_number < state_block:
            replay_from = checkpoint.block_number + 1
            self.events.require_coverage(market, replay_from, state_block)
            timestamps = self.events.timestamps(market, replay_from, state_block)
            for event in self.events.iter_events(market, replay_from, state_block):
                l3.apply_event(book, event, parameters["price_precision"])
                applied += 1
                timestamp = timestamps[event.block_number]
                timestamp_source_block = event.block_number
        book.check_invariants()
        return ReconstructedStart(
            market=market.lower(),
            from_block=from_block,
            state_block=state_block,
            state_timestamp=timestamp,
            timestamp_source_block=timestamp_source_block,
            checkpoint=checkpoint,
            book=book,
            parameters=parameters,
            applied_event_count=applied,
        )


class ReplayCursor(Iterable[l3.ReplayEvent]):
    """Open a new chronological local event stream for each replay pass."""

    def __init__(
        self,
        store: HistoricalEventStore,
        market: str,
        from_block: int,
        to_block: int,
    ) -> None:
        store.require_coverage(market, from_block, to_block)
        self.store = store
        self.market = market.lower()
        self.from_block = from_block
        self.to_block = to_block

    def __iter__(self) -> Iterator[l3.ReplayEvent]:
        return self.store.iter_events(self.market, self.from_block, self.to_block)

    def event_count(self) -> int:
        return self.store.event_count(self.market, self.from_block, self.to_block)

    def __len__(self) -> int:
        """Return the local event count without retaining decoded events."""
        return self.event_count()

    def timestamps(self) -> dict[int, int]:
        return self.store.timestamps(self.market, self.from_block, self.to_block)


class KuruLpBacktestEngine:
    """Run the verified Kuru LP engine over a generalized replay cursor."""

    def __init__(self, maximum_compact_frames: int = 1_500) -> None:
        self.maximum_compact_frames = maximum_compact_frames

    def run(
        self,
        reconstructed: ReconstructedStart,
        cursor: ReplayCursor,
        config: kuru_lp.LPConfig,
        distribution: kuru_lp.LPDistribution,
        verified_input: dict[str, Any],
        *,
        timeline_stride: int | None = None,
        audit_invariants: bool = False,
        progress_callback: Any = None,
        should_cancel: Any = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        count = cursor.event_count()
        if count == 0:
            raise HistoricalRangeError("requested range contains no indexed market events")
        stride = timeline_stride or max(
            1, math.ceil(count / max(1, self.maximum_compact_frames - 2))
        )
        timestamps = cursor.timestamps()
        timestamps[reconstructed.state_block] = reconstructed.state_timestamp
        verified = {
            **verified_input,
            "event_count": count,
            "event_partitions": cursor.store.content_identity(
                cursor.from_block, cursor.to_block
            ),
            "checkpoint_block": reconstructed.checkpoint.block_number,
            "checkpoint_sha256": reconstructed.checkpoint.sha256,
        }
        return kuru_lp.run_lp(
            reconstructed.book,
            cursor,  # type: ignore[arg-type]
            cursor.from_block,
            cursor.to_block,
            config,
            distribution,
            reconstructed.parameters["price_precision"],
            reconstructed.parameters["size_precision"],
            timestamps,
            verified,
            timeline_stride=stride,
            audit_invariants=audit_invariants,
            progress_callback=progress_callback,
            should_cancel=should_cancel,
        )
