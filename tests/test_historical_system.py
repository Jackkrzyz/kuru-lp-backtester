"""Tests for generalized local historical reconstruction and replay."""

from __future__ import annotations

import itertools

import pytest

from historical_system import (
    CheckpointStore,
    HistoricalEventStore,
    HistoricalRangeError,
    ReconstructionEngine,
    ReplayCursor,
)


MARKET = "0x065c9d28e428a0db40191a54d33d5b7c71a9c394"
GOLDENS = {
    "relatively_active": {
        "from": 109097028,
        "to": 109107027,
        "events": 98178,
        "event_sha": "04bc2d4e2f59206586f6b78e95a9700820bcff7a554f1eb060486409f331809d",
        "checkpoint": 109097027,
        "checkpoint_sha": "8cd3d48f75e07c2edb8e59fd09ab1c1ae85b5fc7f707e4bb63ea816985462fb1",
    },
    "directional": {
        "from": 109117028,
        "to": 109127027,
        "events": 96656,
        "event_sha": "aaea711b6719b6f9d3aad74caf01d581e64fb677d186716c611bd320fcd3bc8a",
        "checkpoint": 109117027,
        "checkpoint_sha": "2c99550a9d7de5908a17f15ceaf6d31678c904c9d7f459a8cd81034bf83a9113",
    },
    "relatively_quiet": {
        "from": 109132028,
        "to": 109142027,
        "events": 69672,
        "event_sha": "b47918e80a80621fd489ee6d7a0c5e0f90da535d26f4bc3967b30afbe8658c76",
        "checkpoint": 109132027,
        "checkpoint_sha": "24713ce8fb6b024b1ba03c175daa09227248d27dcfeea4cbf15bf7d339682b88",
    },
}


@pytest.fixture(scope="module")
def system() -> tuple[HistoricalEventStore, CheckpointStore, ReconstructionEngine]:
    events = HistoricalEventStore()
    checkpoints = CheckpointStore(validation=events.validation)
    return events, checkpoints, ReconstructionEngine(events, checkpoints)


def identity(event: object) -> tuple[int, int, int]:
    return (
        event.block_number,  # type: ignore[attr-defined]
        event.transaction_index,  # type: ignore[attr-defined]
        event.log_index,  # type: ignore[attr-defined]
    )


def test_interval_begins_at_checkpoint_boundary(system) -> None:
    _, _, reconstruction = system
    start = reconstruction.reconstruct_start(MARKET, 109097028)
    assert start.state_block == 109097027
    assert start.checkpoint.block_number == start.state_block
    assert start.applied_event_count == 0
    start.book.check_invariants()


def test_interval_begins_between_checkpoints(system) -> None:
    events, _, reconstruction = system
    start = reconstruction.reconstruct_start(MARKET, 109097100)
    expected = events.event_count(MARKET, 109097028, 109097099)
    assert start.checkpoint.block_number == 109097027
    assert start.applied_event_count == expected
    assert expected > 0
    start.book.check_invariants()


def test_overlapping_arbitrary_intervals_share_exact_event_identities(system) -> None:
    events, _, _ = system
    left = list(ReplayCursor(events, MARKET, 109097050, 109097150))
    right = list(ReplayCursor(events, MARKET, 109097100, 109097200))
    overlap = list(ReplayCursor(events, MARKET, 109097100, 109097150))
    left_ids = {identity(item) for item in left}
    right_ids = {identity(item) for item in right}
    assert [identity(item) for item in overlap] == sorted(left_ids & right_ids)


def test_each_complete_indexed_range_streams_all_events(system) -> None:
    events, _, _ = system
    for golden in GOLDENS.values():
        cursor = ReplayCursor(events, MARKET, golden["from"], golden["to"])
        assert cursor.event_count() == golden["events"]
        first = next(iter(cursor))
        last = next(itertools.islice(iter(cursor), golden["events"] - 1, None))
        assert first.block_number >= golden["from"]
        assert last.block_number <= golden["to"]


def test_invalid_and_unavailable_ranges_fail_closed(system) -> None:
    events, _, reconstruction = system
    with pytest.raises(HistoricalRangeError, match="from_block"):
        events.require_coverage(MARKET, 20, 10)
    unavailable_block = max(item["to_block"] for item in events.coverage()) + 1
    with pytest.raises(HistoricalRangeError, match="continuous validated L3"):
        ReplayCursor(events, MARKET, unavailable_block, unavailable_block)
    with pytest.raises(HistoricalRangeError, match="checkpoint"):
        reconstruction.reconstruct_start(MARKET, 43_660_999)


def test_reconstruction_is_deterministic_for_repeated_runs(system) -> None:
    _, _, reconstruction = system
    first = reconstruction.reconstruct_start(MARKET, 109097250)
    second = reconstruction.reconstruct_start(MARKET, 109097250)
    assert first.book.aggregate() == second.book.aggregate()
    assert first.applied_event_count == second.applied_event_count
    assert first.state_timestamp == second.state_timestamp


def test_existing_three_golden_fixture_hashes_are_unchanged(system) -> None:
    events, checkpoints, _ = system
    by_name = {item.golden_fixture: item for item in events.partitions}
    checkpoint_by_name = {item.golden_fixture: item for item in checkpoints.checkpoints}
    for name, golden in GOLDENS.items():
        partition = by_name[name]
        checkpoint = checkpoint_by_name[name]
        events.validation.verify_partition(partition)
        checkpoints.validation.verify_checkpoint(checkpoint)
        assert partition.from_block == golden["from"]
        assert partition.to_block == golden["to"]
        assert partition.event_count == golden["events"]
        assert partition.sha256 == golden["event_sha"]
        assert checkpoint.block_number == golden["checkpoint"]
        assert checkpoint.sha256 == golden["checkpoint_sha"]
