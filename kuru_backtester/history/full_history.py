#!/usr/bin/env python3
"""Resumable, partitioned HyperSync download and verified L3 reconstruction.

No strategy orders or UI event timelines are produced. Every partition uses
the existing L3 mutation rules. Unsupported history stops the run; it is never
labelled validated. The lifetime target is fixed when the job starts.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from decimal import Decimal
import json
import hashlib
import os
from pathlib import Path
import time
from typing import Any

from eth_abi import decode as abi_decode
from eth_utils import keccak

from kuru_backtester.engine import l3_vertical_slice as l3
from kuru_backtester.history.discover_envio_intervals import RateLimitedHyperSyncClient
from kuru_backtester.history.historical_system import deserialize_book
from kuru_backtester.history.index_history import (
    BLOCK_FIELDS, EVENT_TOPICS, LOG_FIELDS, SCHEMA_VERSION, StagingPartition,
    canonical_json, capture_checkpoint, content_hash, run_index,
)
from kuru_backtester.history.promote_historical_partition import promote
from kuru_backtester.engine.rebuild_from_chain import (
    DEFAULT_RPC_URL, DEPLOYMENT_BLOCK, MARKET_ADDRESS, ReconstructionError,
    RpcClient, market_parameters, validate_market,
)
from kuru_backtester.history.reconstruction_validation_report import cancellation_summary_audit, validation_blocks
from kuru_backtester.history import reconstruction_profiles as profiles

from kuru_backtester.paths import ROOT
DEFAULT_JOB = ROOT / ".cache" / "full_history"
DEFAULT_MANIFEST = ROOT / "full_history_manifest.json"
KNOWN_IMPLEMENTATION = profiles.CURRENT_IMPLEMENTATION


class UnsupportedHistoryError(ReconstructionError):
    """Known unsupported history; retain a gap rather than certify it."""


def check_supported_events(events) -> None:
    """Reject unsupported vault activity locally, before expensive RPC checks."""
    for event in events:
        if ((event.name == "Trade" and int(event.args["order_id"]) == 0
             and int(event.args["filled_size"]) > 0)
                or (event.name == "VaultParamsUpdated" and any(int(event.args[key]) for key in
                    ("vault_ask_order_size", "vault_bid_order_size")))):
            raise UnsupportedHistoryError(
                f"Active vault history is not supported: {event.name} at block {event.block_number}, "
                f"transaction {event.transaction_hash}. This partition remains unavailable for LP backtests.")


@contextmanager
def writer_lock(path: Path):
    """Use an OS lock; process exit releases it on Windows and WSL."""
    with path.open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise RuntimeError("Another process owns this indexing job") from error
        try:
            yield
        finally:
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as output:
        output.write(canonical_json(value) + "\n")
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)


def partitions(first: int, last: int, size: int):
    if first < 1 or last < first or not 1 <= size <= 10_000:
        raise ValueError("Use a valid positive range and a partition size of 1–10000 blocks")
    for start in range(first, last + 1, size):
        yield start, min(last, start + size - 1)


class DurablePartition(StagingPartition):
    """Recover an interrupted page without retaining lifetime event identities.

    Only this small partition keeps a deduplication set. The byte boundary is
    committed together with next_block. An incomplete page is replayed.
    """
    def __init__(self, directory: Path, signature: dict[str, Any]):
        state_path = directory / "state.json"
        events_path = directory / "events.jsonl"
        if state_path.exists():
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if state.get("signature") != signature:
                raise ValueError("partition identity differs; use the original settings")
            if state.get("durable_page_schema") != 1:
                raise ValueError("partition lacks durable page metadata; use a new job directory")
            committed = int(state.get("committed_bytes", 0))
            if events_path.exists():
                if events_path.stat().st_size < committed:
                    raise ValueError("committed event file is truncated")
                with events_path.open("r+b") as output:
                    output.truncate(committed)
        super().__init__(directory, signature)

    def save(self) -> None:
        # append() returns only after the page file has been flushed. fsync the
        # data before the cursor, so a crash cannot commit a missing page.
        if self.events_path.exists():
            with self.events_path.open("rb") as source:
                os.fsync(source.fileno())
        self.state["committed_bytes"] = self.events_path.stat().st_size if self.events_path.exists() else 0
        self.state["durable_page_schema"] = 1
        atomic_json(self.state_path, self.state)


class Progress:
    """Write small real telemetry snapshots; no per-event animation records."""
    def __init__(self, path: Path, configuration: dict[str, Any]):
        self.path = path
        self.started = time.monotonic()
        self.last_write = 0.0
        self.value = {
            "schema_version": "1.0", "configuration": configuration,
            "phase": "starting", "requests": 0, "pages": 0,
            "decoded_response_bytes": 0, "rate_limit_waits": 0,
            "rate_limit_wait_seconds": 0, "downloaded_events": 0,
            "reconstructed_events": 0, "validated_events": 0,
            "downloaded_block": configuration["from_block"] - 1,
            "reconstructed_block": configuration["from_block"] - 1,
            "validated_block": configuration["from_block"] - 1,
            "event_counts": {}, "coverage": [], "recent_candles": [],
            "actions": [], "action_sequence": 0,
        }

    def update(self, *, force: bool = False, **values: Any) -> None:
        phase = values.get("phase")
        if phase and phase != self.value.get("phase"):
            self.action("phase", phase=phase, partition=values.get("current_partition", self.value.get("current_partition")))
        if values.get("l2_checks_current_partition", 0) > self.value.get("l2_checks_current_partition", 0):
            self.action("l2_check", block=values.get("reconstructed_block"),
                        checks=values["l2_checks_current_partition"], queue_invariants="pass", price_level_error_raw=0, aggregate_size_error_raw=0)
        self.value.update(values)
        now = time.monotonic()
        if force or now - self.last_write >= 0.5:
            self.value["elapsed_seconds"] = round(now - self.started, 3)
            self.value["updated_at"] = time.time()
            atomic_json(self.path, self.value)
            self.last_write = now

    def action(self, kind: str, **fields: Any) -> None:
        """Bounded observational trace, not a market event or a strategy timeline."""
        self.value["action_sequence"] += 1
        self.value["actions"].append({"sequence": self.value["action_sequence"], "timestamp": time.time(), "kind": kind, **fields})
        self.value["actions"] = self.value["actions"][-60:]

    def observe(self, item: dict[str, Any]) -> None:
        kind = item["kind"]
        if kind == "request":
            self.value["requests"] += 1
            self.action(kind, **{key: value for key, value in item.items() if key != "kind"})
            self.update(force=True, phase="downloading", current_request=item)
        elif kind == "response":
            self.value["decoded_response_bytes"] += item["decoded_response_bytes"]
            self.action(kind, **{key: value for key, value in item.items() if key != "kind"})
            self.update(force=True, rate_limit_headers=item["headers"], last_response=item)
        elif kind == "page_committed":
            self.action(kind, **{key: value for key, value in item.items() if key != "kind"})
            self.update(force=True, last_committed_page=item)
        elif kind == "rate_limit_wait":
            self.value["rate_limit_waits"] += 1
            self.value["rate_limit_wait_seconds"] += item["seconds"]
            self.action(kind, seconds=item["seconds"], headers=item["headers"])
            self.update(force=True, phase="rate_limit_wait", retry_seconds=item["seconds"],
                        rate_limit_headers=item["headers"])

    def observe_rpc(self, item: dict[str, Any]) -> None:
        """RPC telemetry contains method counts, never endpoint credentials."""
        self.action(item["kind"], **{key: value for key, value in item.items() if key != "kind"},
                    partition=self.value.get("current_partition"), phase=self.value.get("phase"))
        if item["kind"] == "rpc_response":
            counts = dict(self.value.get("rpc_method_counts", {}))
            for method, count in item["methods"].items():
                counts[method] = counts.get(method, 0) + count
            self.update(rpc_method_counts=counts, rpc_http_responses=self.value.get("rpc_http_responses", 0) + 1,
                        rpc_response_seconds=self.value.get("rpc_response_seconds", 0) + item["seconds"],
                        rpc_wait_until=None)
        else:
            self.update(force=True, rpc_wait_until=time.time() + item["seconds"],
                        rpc_retry_count=self.value.get("rpc_retry_count", 0) + 1)


def contract_guard(rpc: RpcClient, market: str, block: int) -> dict[str, Any]:
    """Do not apply tested explicit-queue rules to unknown code or live vaults."""
    slot = hex(int.from_bytes(keccak(text="eip1967.proxy.implementation"), "big") - 1)
    implementation = rpc.call("eth_getStorageAt", [market, slot, hex(block)])
    address = "0x" + implementation[-40:]
    code = rpc.call("eth_getCode", [address, hex(block)])
    import hashlib
    code_hash = hashlib.sha256(bytes.fromhex(code[2:])).hexdigest()
    if code_hash not in profiles.SUPPORTED_IMPLEMENTATIONS:
        raise UnsupportedHistoryError(f"Unsupported contract implementation at block {block}: {code_hash}")
    vault = abi_decode(
        ("address", "uint256", "uint96", "uint256", "uint96", "uint96", "uint96", "uint96"),
        bytes.fromhex(rpc.contract_call(market, "getVaultParams()", block)[2:]),
    )
    if int(vault[5]) - int(vault[2]) or int(vault[6]) - int(vault[4]):
        raise UnsupportedHistoryError(f"Active vault liquidity is not verified at block {block}")
    return {"implementation_code_sha256": code_hash, "vault_active": False}


def separate_deployment_logs(logs: list[dict], market: str) -> tuple[list[dict], list[dict]]:
    """Audit the two deployment logs separately from filtered market events.

    Only this market's known deployment transaction is exempt from event parity.
    Unknown logs and later upgrades remain in parity and cause it to fail.
    """
    deployment_tx = "0x85f4b267e1b2a1c0062d5ce1142f3c721a071682dc73d3780b8cccfcd4086a72"
    upgraded = "0x" + keccak(text="Upgraded(address)").hex()
    initialized = "0x" + keccak(text="Initialized(uint64)").hex()
    expected = {
        upgraded: ([upgraded, "0x" + profiles.DEPLOYMENT_IMPLEMENTATION_ADDRESS[2:].zfill(64)], "0x"),
        initialized: ([initialized], "0x" + f"{1:064x}"),
    }
    events, administrative = [], []
    for log in logs:
        normalized = l3.normalize_rpc_log(log)
        topic = normalized["topics"][0] if normalized["topics"] else ""
        if (market == MARKET_ADDRESS and str(log.get("address", "")).lower() == market
                and normalized["blockNumber"] == DEPLOYMENT_BLOCK
                and normalized["transactionHash"] == deployment_tx and topic in expected):
            topics, data = expected[topic]
            if normalized["topics"] != topics or normalized["data"] != data:
                raise ReconstructionError("Unexpected deployment administrative log payload")
            administrative.append(log)
        else:
            events.append(log)
    if administrative and (len(administrative) != 2 or
            [l3.normalize_rpc_log(log)["topics"][0] for log in administrative] != [upgraded, initialized]):
        raise ReconstructionError("Incomplete or duplicate deployment administrative logs")
    return events, administrative


def starting_checkpoint(rpc: RpcClient, market: str, first: int, destination: Path) -> tuple[str, dict]:
    """Bootstrap a proven empty pre-deployment book, not a nonexistent contract call."""
    if market == MARKET_ADDRESS and first == DEPLOYMENT_BLOCK and rpc.call("eth_getCode", [market, hex(first - 1)]) == "0x":
        guard = contract_guard(rpc, market, first)
        parameters = market_parameters(rpc, market, first)
        parameters = {key: int(parameters[key]) for key in ("price_precision", "size_precision", "tick_size", "min_size", "base_decimals", "quote_decimals")}
        timestamp = rpc.block_timestamps([first - 1])[first - 1]
        atomic_json(destination, checkpoint_payload(market, first - 1, timestamp, parameters, l3.L3Book()))
        return content_hash(destination), guard
    guard = contract_guard(rpc, market, first - 1)
    if guard["implementation_code_sha256"] in profiles.RECIPROCAL_LINK_IMPLEMENTATIONS:
        book, _, _ = profiles.load_checkpoint(rpc, market, first - 1, guard["implementation_code_sha256"])
        parameters = market_parameters(rpc, market, first - 1)
        parameters = {key: int(parameters[key]) for key in ("price_precision", "size_precision", "tick_size", "min_size", "base_decimals", "quote_decimals")}
        timestamp = rpc.block_timestamps([first - 1])[first - 1]
        atomic_json(destination, checkpoint_payload(market, first - 1, timestamp, parameters, book))
        return content_hash(destination), guard
    return capture_checkpoint(rpc.url, market, first - 1, destination), guard


def checkpoint_payload(market: str, block: int, timestamp: int, parameters: dict, book: l3.L3Book) -> dict:
    book.check_invariants()
    payload = {
        "schema_version": "1.0", "market": market, "checkpoint_block": block,
        "checkpoint_timestamp": timestamp, "parameters": parameters,
        "book": {
            "orders": [asdict(book.orders[key]) for key in sorted(book.orders)],
            "levels": [{"is_buy": side, "price": price, "head": level.head,
                        "tail": level.tail, "total_size": level.total_size}
                       for (side, price), level in sorted(book.levels.items(), key=lambda item: (not item[0][0], item[0][1]))],
        },
    }
    metadata = profiles.head_metadata(book)
    if metadata:
        profiles.restore_head_metadata(book, sorted(metadata.items()))
        payload["book"]["contract_head_prev"] = [list(item) for item in sorted(metadata.items())]
    return payload


def trade_candles(rows: list[dict], events: list[l3.ReplayEvent], size_precision: int) -> list[dict]:
    """OHLC uses integer Trade prices only. Empty hours have no artificial bars."""
    candles: dict[int, dict] = {}
    for row, event in zip(rows, events, strict=True):
        if event.name != "Trade":
            continue
        hour = int(row["block_timestamp"]) // 3600 * 3600
        price, size = int(event.args["price"]), int(event.args["filled_size"])
        if price <= 0 or size < 0:
            raise ReconstructionError("invalid Trade price or size")
        if hour not in candles:
            candles[hour] = {"timestamp": hour, "open_raw": price, "high_raw": price,
                             "low_raw": price, "close_raw": price, "volume_raw": 0,
                             "trade_count": 0, "from_block": event.block_number, "to_block": event.block_number}
        candle = candles[hour]
        candle.update(high_raw=max(candle["high_raw"], price), low_raw=min(candle["low_raw"], price),
                      close_raw=price, volume_raw=candle["volume_raw"] + size,
                      trade_count=candle["trade_count"] + 1, to_block=event.block_number)
    result = []
    for candle in sorted(candles.values(), key=lambda item: item["timestamp"]):
        item = dict(candle)
        for field in ("open", "high", "low", "close"):
            item[field] = str(Decimal(item[field + "_raw"]) / Decimal(10**18))
            item[field + "_raw"] = str(item[field + "_raw"])
        item["volume_mon"] = str(Decimal(item["volume_raw"]) / Decimal(size_precision))
        item["volume_raw"] = str(item["volume_raw"])
        result.append(item)
    return result


@dataclass(frozen=True)
class VerifiedBoundary:
    """One immutable end checkpoint from a checked partition in this job.

    Call only after validation or the saved-report hash checks. This is a
    bounded handoff between partitions, not a cache of unchecked RPC results.
    """

    checkpoint_json: str
    block_hash: str
    implementation: str

    @classmethod
    def from_result(cls, directory: Path, summary: dict) -> "VerifiedBoundary":
        data = (directory / "end_checkpoint.json").read_bytes()
        if hashlib.sha256(data).hexdigest() != summary["end_checkpoint_sha256"]:
            raise ReconstructionError("end checkpoint handoff hash mismatch")
        checkpoint = json.loads(data)
        block = summary["interval"]["to_block"]
        if checkpoint["market"] != summary["market"] or checkpoint["checkpoint_block"] != block:
            raise ReconstructionError("end checkpoint handoff identity mismatch")
        if not summary["validation"]["final_l3_checkpoint_match"]:
            raise ReconstructionError("end checkpoint handoff has no full L3 match")
        return cls(data.decode("utf-8"), summary["boundary_block_hashes"][str(block)],
                   summary["contract_guards"][-1]["implementation_code_sha256"])

    def write_start(self, rpc: RpcClient, market: str, first: int,
                    header: dict, destination: Path) -> tuple[str, dict] | None:
        checkpoint = json.loads(self.checkpoint_json)
        if checkpoint["market"] != market or checkpoint["checkpoint_block"] != first - 1:
            return None
        # Always check the live boundary and contract. Never reuse boundary
        # headers across the before/after reorg checks of a new partition.
        if header["hash"] != self.block_hash:
            raise ReconstructionError("block hash changed at checkpoint handoff")
        guard = contract_guard(rpc, market, first - 1)
        if guard["implementation_code_sha256"] != self.implementation:
            raise ReconstructionError("contract changed at checkpoint handoff")
        parameters = market_parameters(rpc, market, first - 1)
        if any(int(parameters[key]) != value for key, value in checkpoint["parameters"].items()):
            raise ReconstructionError("market parameters changed at checkpoint handoff")
        deserialize_book(checkpoint["book"]).check_invariants()
        atomic_json(destination, checkpoint)
        return content_hash(destination), guard


def validation_rpc(url: str) -> RpcClient:
    """Pace archive requests; settings affect transport only."""
    return RpcClient(url, timeout=180,
                     timestamp_batch_size=int(os.environ.get("KURU_RPC_TIMESTAMP_BATCH_SIZE", "50")),
                     min_request_interval=float(os.environ.get("KURU_RPC_MIN_REQUEST_INTERVAL", "0.1")))


def validate_partition(directory: Path, market: str, first: int, last: int,
                       rpc: RpcClient, progress: Progress, base_count: int = 0,
                       previous: VerifiedBoundary | None = None) -> dict:
    """Bound memory to one partition; reuse all verified reconstruction rules."""
    event_path = directory / "events.jsonl"
    with event_path.open(encoding="utf-8") as source:
        rows = [json.loads(line) for line in source if line.strip()]
    events = [l3.decode_event(row) for row in rows]
    prior = None
    for row, event in zip(rows, events, strict=True):
        position = (event.block_number, event.transaction_index, event.log_index)
        if str(row["address"]).lower() != market or not first <= event.block_number <= last or (prior is not None and position <= prior):
            raise ReconstructionError("staged events are foreign, out of range, duplicated or unordered")
        prior = position
    check_supported_events(events)
    rpc.observer = progress.observe_rpc
    progress.action("partition_input", from_block=first, to_block=last, events=len(events),
                    event_counts=dict(Counter(event.name for event in events)), market=market)
    progress.update(force=True, phase="checkpoint", current_partition=[first, last])
    boundary_headers = rpc.batch([("eth_getBlockByNumber", [hex(block), False]) for block in (first - 1, last)])
    checkpoint_path = directory / "checkpoint.json"
    reused = previous.write_start(rpc, market, first, boundary_headers[0], checkpoint_path) if previous else None
    checkpoint_hash, initial_guard = reused or starting_checkpoint(rpc, market, first, checkpoint_path)
    progress.update(force=True, checkpoint_reused=reused is not None)
    guards = [initial_guard]
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    book = deserialize_book(checkpoint["book"])
    parameters = checkpoint["parameters"]
    progress.action("checkpoint_loaded", block=first - 1, reused=reused is not None,
                    orders=len(book.orders), levels=len(book.levels), sha256=checkpoint_hash,
                    implementation=initial_guard["implementation_code_sha256"])
    # Capture reciprocal flip-pair evidence during normal replay. A flip batch
    # summary lists requested IDs; cancelling one leg can cancel both legs.
    # Do not apply summary IDs as additional book mutations.
    cancellation_pairs = {}

    # RPC parity is checked in small ranges. Only matched market logs are used;
    # an unknown signature causes a mismatch rather than silent omission.
    progress.update(force=True, phase="rpc_parity")
    rpc_count, index = 0, 0
    administrative_logs = []
    for start in range(first, last + 1, 500):
        end = min(last, start + 499)
        stop = index
        while stop < len(events) and events[stop].block_number <= end:
            stop += 1
        rpc_logs, administrative = separate_deployment_logs(rpc.logs(market, start, end), market)
        administrative_logs.extend(administrative)
        l3.parity_report(rows[index:stop], rpc_logs)
        rpc_count += len(rpc_logs)
        index = stop
        progress.action("rpc_parity_check", from_block=start, to_block=end, rpc_events=len(rpc_logs),
                        status="pass", missing_from_either_source=0)
        progress.update(rpc_checked_block=end, rpc_events_current_partition=rpc_count)

    if market == MARKET_ADDRESS and first == DEPLOYMENT_BLOCK and len(administrative_logs) != 2:
        raise ReconstructionError("Missing deployment administrative logs")

    timestamp_blocks = sorted({event.block_number for event in events})
    progress.update(force=True, phase="rpc_timestamps", timestamp_blocks_checked=0,
                    timestamp_blocks_total=len(timestamp_blocks))
    timestamps = {}
    for offset in range(0, len(timestamp_blocks), 500):
        timestamps.update(rpc.block_timestamps(timestamp_blocks[offset:offset + 500]))
        progress.action("timestamp_check", checked=len(timestamps), total=len(timestamp_blocks))
        progress.update(timestamp_blocks_checked=len(timestamps))
    if any(int(row["block_timestamp"]) != timestamps[event.block_number] for row, event in zip(rows, events, strict=True)):
        raise ReconstructionError("HyperSync/RPC block timestamps differ")

    checks = validation_blocks([], first, last, 7)
    event_index, invariant_checks = 0, 0
    counts = Counter()
    for block in checks:
        while event_index < len(events) and events[event_index].block_number <= block:
            event = events[event_index]
            if event.name == "OrderCanceled":
                order = book.orders.get(int(event.args["order_id"]))
                pair = book.orders.get(order.flipped_id) if order else None
                if (pair and pair.flipped_id == order.order_id and pair.owner == order.owner
                        and pair.is_buy != order.is_buy and pair.price == order.flipped_price
                        and pair.flipped_price == order.price):
                    cancellation_pairs[(event.transaction_hash, order.order_id)] = pair.order_id
                    cancellation_pairs[(event.transaction_hash, pair.order_id)] = order.order_id
            profiles.apply_event(book, event, parameters["price_precision"], initial_guard["implementation_code_sha256"])
            counts[event.name] += 1
            event_index += 1
            if event_index % 1000 == 0:
                progress.update(phase="reconstructing", reconstructed_events=base_count + event_index,
                                reconstructed_block=event.block_number, open_orders=len(book.orders),
                                price_levels=len(book.levels), current_event_counts=dict(counts))
        book.check_invariants()
        invariant_checks += 1
        l3.compare_l2(book, rpc, market, block)
        guards.append(contract_guard(rpc, market, block))
        if guards[-1]["implementation_code_sha256"] != initial_guard["implementation_code_sha256"]:
            raise ReconstructionError("Contract implementation changed inside this partition")
        progress.update(force=True, phase="validating", reconstructed_events=base_count + event_index,
                        reconstructed_block=block, l2_checks_current_partition=invariant_checks,
                        open_orders=len(book.orders), price_levels=len(book.levels),
                        current_event_counts=dict(counts))
    audit = cancellation_summary_audit(events, flip_pairs=cancellation_pairs)
    if audit["status"] != "pass":
        raise ReconstructionError("individual/summary cancellation audit failed: " + json.dumps(audit, sort_keys=True))
    progress.action("cancellation_audit", **audit)
    progress.update(force=True, phase="final_l3_checkpoint")
    final_parameters = market_parameters(rpc, market, last)
    if any(int(final_parameters[key]) != int(value) for key, value in parameters.items()):
        raise ReconstructionError("market parameters changed inside this partition")
    official, _, _ = profiles.load_checkpoint(rpc, market, last, initial_guard["implementation_code_sha256"])
    if checkpoint_payload(market, last, 0, parameters, book)["book"] != checkpoint_payload(market, last, 0, parameters, official)["book"]:
        raise ReconstructionError("final L3 queue links differ from RPC checkpoint")
    progress.action("final_l3_match", block=last, orders=len(book.orders), levels=len(book.levels),
                    status="pass", queue_links="exact")
    timestamp = rpc.block_timestamps([last])[last]
    final_headers = rpc.batch([("eth_getBlockByNumber", [hex(block), False]) for block in (first - 1, last)])
    if any(before["hash"] != after["hash"] for before, after in zip(boundary_headers, final_headers, strict=True)):
        raise ReconstructionError("block hashes changed during validation")
    atomic_json(directory / "end_checkpoint.json", checkpoint_payload(market, last, timestamp, parameters, book))
    candles = trade_candles(rows, events, parameters["size_precision"])
    atomic_json(directory / "candles.json", candles)
    summary = {
        "schema_version": "1.0", "market": market,
        "interval": {"from_block": first, "to_block": last},
        "event_source": "envio_hypersync_staged_verified_by_rpc",
        "event_input": {"record_count": len(rows), "source_sha256": content_hash(event_path)},
        "event_counts": dict(sorted(counts.items())),
        "event_parity": {"status": "pass", "envio_event_count": len(rows), "rpc_event_count": rpc_count,
                         "missing_from_envio": 0, "missing_from_rpc": 0},
        "validation": {"blocks": checks, "maximum_price_level_error_raw": 0,
                       "maximum_aggregate_size_error_raw": 0, "maximum_midpoint_error": 0,
                       "queue_invariant_status": "pass", "queue_invariant_failures": 0,
                       "queue_invariant_checks": invariant_checks, "final_l3_checkpoint_match": True},
        "cancellation_summary_audit": audit, "contract_guards": guards,
        "checkpoint_sha256": checkpoint_hash,
        "end_checkpoint_sha256": content_hash(directory / "end_checkpoint.json"),
        "candles_sha256": content_hash(directory / "candles.json"),
        "boundary_block_hashes": {str(block): header["hash"] for block, header in zip((first - 1, last), final_headers, strict=True)},
        "timestamp_parity_status": "pass",
        "final_open_orders": len(book.orders), "final_price_levels": len(book.levels),
    }
    if administrative_logs:
        atomic_json(directory / "deployment_logs.json", administrative_logs)
        summary["administrative_log_audit"] = {
            "status": "pass", "rpc_log_count": len(administrative_logs),
            "events": {"Upgraded": 1, "Initialized": 1},
            "source_sha256": content_hash(directory / "deployment_logs.json"),
            "scope": "known deployment only; excluded from canonical market-event parity",
        }
    atomic_json(directory / "summary.json", summary)
    progress.action("verified_partition", from_block=first, to_block=last, events=len(events),
                    trades=counts.get("Trade", 0), candles=len(candles),
                    source_sha256=summary["event_input"]["source_sha256"],
                    l2_checks=invariant_checks, price_error_raw=0, size_error_raw=0)
    progress.update(force=True, phase="partition_validated", recent_candles=candles[-120:])
    return summary


def ensure_manifest(path: Path, market: str, deployment: int) -> None:
    if path.resolve().parent != ROOT:
        raise ValueError("manifest must be in the project root for indexed relative paths")
    if not path.exists():
        atomic_json(path, {"schema_version": "1.0", "market": market, "deployment_block": deployment,
                           "event_partitions": [], "checkpoints": []})
    if json.loads(path.read_text(encoding="utf-8"))["market"].lower() != market:
        raise ValueError("manifest belongs to another market")


def run_pipeline(configuration: dict, job_root: Path, manifest: Path,
                 client: RateLimitedHyperSyncClient, rpc: RpcClient, progress: Progress) -> None:
    job_root.mkdir(parents=True, exist_ok=True)
    if not configuration.get("download_only"):
        ensure_manifest(manifest, configuration["market"], configuration["deployment_block"])
        progress.update(force=True, phase="contract_preflight")
        start = configuration["from_block"]
        contract_guard(rpc, configuration["market"], start if start == DEPLOYMENT_BLOCK else start - 1)
    base_count = 0
    base_pages = 0
    base_bytes = 0
    counts = Counter()
    previous = None
    for first, last in partitions(configuration["from_block"], configuration["to_block"], configuration["partition_blocks"]):
        import shutil
        if shutil.disk_usage(job_root).free < 256 * 1024 * 1024:
            raise OSError("Less than 256 MiB free disk space; stopped before the next partition")
        progress.update(force=True, current_partition=[first, last], current_event_counts={},
                        l2_checks_current_partition=0, rpc_events_current_partition=0)
        directory = job_root / f"{first}-{last}"
        signature = {"schema_version": SCHEMA_VERSION, "market": configuration["market"],
                     "from_block": first, "to_block": last, "topics": EVENT_TOPICS,
                     "log_fields": LOG_FIELDS, "block_fields": BLOCK_FIELDS,
                     "hypersync_url": client.url}
        partition = DurablePartition(directory, signature)
        saved = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else {"event_partitions": []}
        overlapping = [item for item in saved["event_partitions"] if not (last < item["from_block"] or first > item["to_block"])]
        if configuration.get("download_only"):
            def downloaded_page(state):
                progress.update(force=True, phase="downloading", current_partition=[first, last],
                                downloaded_block=state["next_block"] - 1,
                                downloaded_events=base_count + state["event_count"],
                                pages=base_pages + state["page_count"],
                                stored_bytes=base_bytes + partition.events_path.stat().st_size)
            run_index(client, partition, configuration["market"], last + 1, downloaded_page)
            base_count += partition.state["event_count"]
            base_pages += partition.state["page_count"]
            base_bytes += partition.events_path.stat().st_size
            progress.update(force=True, downloaded_events=base_count, downloaded_block=last,
                            pages=base_pages, stored_bytes=base_bytes)
            continue
        if overlapping:
            if len(overlapping) != 1 or (overlapping[0]["from_block"], overlapping[0]["to_block"]) != (first, last):
                raise ValueError("partition overlaps different manifest bounds")
            from kuru_backtester.history.historical_system import HistoricalEventStore
            store = HistoricalEventStore(manifest)
            summary = store.validation.verify_partition(store.selected_partitions(first, last)[0], configuration["market"])
            if content_hash(directory / "checkpoint.json") != summary["checkpoint_sha256"]:
                raise ReconstructionError("resumed checkpoint hash mismatch")
            if content_hash(directory / "candles.json") != summary["candles_sha256"]:
                raise ReconstructionError("resumed candle hash mismatch")
        else:
            def page(state):
                progress.update(force=True, phase="downloading", current_partition=[first, last],
                                downloaded_block=state["next_block"] - 1,
                                downloaded_events=base_count + state["event_count"],
                                pages=base_pages + state["page_count"], stored_bytes=base_bytes + partition.events_path.stat().st_size)
            if configuration.get("reconstruct_only") and not partition.state["complete"]:
                raise ValueError(f"partition {first}-{last} is not downloaded; reconstruct-only will not query HyperSync")
            run_index(client, partition, configuration["market"], last + 1, page)
            summary = validate_partition(directory, configuration["market"], first, last, rpc, progress, base_count, previous)
            promote(manifest, partition.events_path, directory / "summary.json", directory / "checkpoint.json")
        base_count += summary["event_input"]["record_count"]
        base_pages += partition.state["page_count"]
        base_bytes += partition.events_path.stat().st_size
        counts.update(summary["event_counts"])
        final_checkpoint = json.loads((directory / "end_checkpoint.json").read_text())
        if content_hash(directory / "end_checkpoint.json") != summary["end_checkpoint_sha256"]:
            raise ReconstructionError("resumed end checkpoint hash mismatch")
        previous = VerifiedBoundary.from_result(directory, summary)
        partition.state.update(validation_status="validated", complete=True)
        partition.save()
        coverage = json.loads(manifest.read_text(encoding="utf-8"))["event_partitions"]
        merged_coverage = []
        for item in coverage:
            if merged_coverage and item["from_block"] == merged_coverage[-1]["to_block"] + 1:
                merged_coverage[-1]["to_block"] = item["to_block"]
            else:
                merged_coverage.append({"from_block": item["from_block"], "to_block": item["to_block"]})
        # The screen needs bounds, not file paths or all source events.
        progress.update(force=True, downloaded_block=last, reconstructed_block=last, validated_block=last,
                        downloaded_events=base_count, reconstructed_events=base_count, validated_events=base_count,
                        event_counts=dict(sorted(counts.items())),
                        pages=base_pages, stored_bytes=base_bytes,
                        open_orders=len(final_checkpoint["book"]["orders"]), price_levels=len(final_checkpoint["book"]["levels"]),
                        current_event_counts={},
                        queue_invariant_failures=0, event_parity_status="pass",
                        maximum_price_level_error_raw=0, maximum_aggregate_size_error_raw=0, maximum_midpoint_error=0,
                        coverage=merged_coverage,
                        recent_candles=json.loads((directory / "candles.json").read_text())[-120:])
    if not configuration.get("download_only"):
        export_candles(job_root)
    progress.update(force=True, phase="complete", error=None)


def export_candles(job_root: Path) -> None:
    """Merge partition contributions in chronological order, one hour at a time."""
    import sqlite3
    database = sqlite3.connect(job_root / "candles.sqlite")
    database.execute("CREATE TABLE IF NOT EXISTS contributions (partition TEXT, hour INTEGER, payload TEXT, PRIMARY KEY(partition,hour))")
    for directory in sorted(job_root.glob("*-*"), key=lambda path: int(path.name.split("-")[0])):
        if not (directory / "summary.json").exists():
            continue
        for candle in json.loads((directory / "candles.json").read_text()):
            database.execute("INSERT OR REPLACE INTO contributions VALUES (?,?,?)", (directory.name, candle["timestamp"], canonical_json(candle)))
    database.commit()
    temporary = job_root / "candles.jsonl.tmp"
    current = None
    with temporary.open("w", encoding="utf-8") as output:
        for (payload,) in database.execute("SELECT payload FROM contributions ORDER BY hour, CAST(substr(partition,1,instr(partition,'-')-1) AS INTEGER)"):
            candle = json.loads(payload)
            if current is None or candle["timestamp"] != current["timestamp"]:
                if current is not None:
                    output.write(canonical_json(current) + "\n")
                current = candle
            else:
                for field, operation in (("high_raw", max), ("low_raw", min)):
                    current[field] = str(operation(int(current[field]), int(candle[field])))
                current.update(close_raw=candle["close_raw"], close=candle["close"], to_block=candle["to_block"],
                               volume_raw=str(int(current["volume_raw"]) + int(candle["volume_raw"])),
                               volume_mon=str(Decimal(current["volume_mon"]) + Decimal(candle["volume_mon"])),
                               trade_count=current["trade_count"] + candle["trade_count"])
                current["high"] = str(Decimal(current["high_raw"]) / Decimal(10**18))
                current["low"] = str(Decimal(current["low_raw"]) / Decimal(10**18))
        if current is not None:
            output.write(canonical_json(current) + "\n")
    database.close()
    temporary.replace(job_root / "candles.jsonl")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market", default=MARKET_ADDRESS, type=validate_market)
    parser.add_argument("--from-block", type=int, default=DEPLOYMENT_BLOCK)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--to-block", type=int)
    target.add_argument("--to-finalized", action="store_true", help="Freeze the RPC finalized block, not the moving head")
    parser.add_argument("--partition-blocks", type=int, default=5000)
    parser.add_argument("--job-root", type=Path, default=DEFAULT_JOB)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--hypersync-url", default=l3.DEFAULT_HYPERSYNC_URL)
    parser.add_argument("--rpc-url", default=DEFAULT_RPC_URL)
    parser.add_argument("--execute", action="store_true", help="Explicit permission to start network indexing")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--download-only", action="store_true", help="Archive all filtered events without claiming reconstructed or validated coverage")
    modes.add_argument("--reconstruct-only", action="store_true", help="Validate downloaded partitions with RPC; do not query HyperSync")
    parser.add_argument("--local-events", type=Path, help="Use one bounded existing canonical event file; no HyperSync download")
    args = parser.parse_args()
    if not 1 <= args.partition_blocks <= 10000 or args.from_block < 1:
        parser.error("use positive blocks and partition-blocks <= 10000")
    if not args.execute:
        print("Plan only. No network calls or downloads. Add --execute to start.")
        print(f"market={args.market} from={args.from_block} to={args.to_block or 'finalized'} partition_blocks={args.partition_blocks}")
        return 0
    token = os.environ.get("ENVIO_API_TOKEN", "").strip()
    if not token and args.local_events is None and not args.reconstruct_only:
        parser.error("ENVIO_API_TOKEN is not set")
    if args.local_events and (args.to_finalized or args.to_block - args.from_block + 1 > args.partition_blocks):
        parser.error("local-events requires one explicit range no larger than partition-blocks")
    if ROOT not in args.job_root.resolve().parents:
        parser.error("job-root must stay inside the project")
    args.job_root.mkdir(parents=True, exist_ok=True)
    with writer_lock(args.job_root / "writer.lock"):
        rpc = validation_rpc(args.rpc_url)
        if int(rpc.call("eth_chainId", []), 16) != 143:
            raise ValueError("RPC must use Monad Mainnet (chain ID 143)")
        saved_path = args.job_root / "job.json"
        saved = json.loads(saved_path.read_text()) if saved_path.exists() else None
        finalized = rpc.call("eth_getBlockByNumber", ["finalized", False])
        if not finalized:
            raise ValueError("RPC does not provide a finalized block")
        last = args.to_block
        if args.to_finalized:
            last = saved["to_block"] if saved else int(finalized["number"], 16)
        if last > int(finalized["number"], 16):
            raise ValueError("requested end exceeds the RPC finalized block")
        configuration = {"market": args.market, "from_block": args.from_block, "to_block": last,
                         "partition_blocks": args.partition_blocks, "deployment_block": DEPLOYMENT_BLOCK,
                         "hypersync_url": args.hypersync_url.rstrip("/"), "rpc_url": args.rpc_url,
                         "manifest": str(args.manifest.resolve()),
                         "local_source_sha256": content_hash(args.local_events) if args.local_events else None}
        if last < args.from_block:
            raise ValueError("to-block must not be below from-block")
        if saved and saved != configuration:
            raise ValueError("job identity changed; use the original settings or a new job-root")
        atomic_json(saved_path, configuration)
        configuration["download_only"] = args.download_only
        configuration["reconstruct_only"] = args.reconstruct_only
        progress = Progress(args.job_root / "progress.json", configuration)
        client = RateLimitedHyperSyncClient(args.hypersync_url, token or "local-validation-only", observer=progress.observe)
        try:
            if args.local_events:
                signature = {"schema_version": SCHEMA_VERSION, "market": args.market,
                             "from_block": args.from_block, "to_block": last, "topics": EVENT_TOPICS,
                             "log_fields": LOG_FIELDS, "block_fields": BLOCK_FIELDS, "hypersync_url": client.url}
                partition = DurablePartition(args.job_root / f"{args.from_block}-{last}", signature)
                if not partition.state["complete"]:
                    with args.local_events.open(encoding="utf-8") as source:
                        batch = []
                        for line in source:
                            if not line.strip():
                                continue
                            row = json.loads(line)
                            if args.from_block <= int(row["block_number"]) <= last:
                                batch.append(row)
                            if len(batch) >= 1000:
                                partition.append(batch)
                                batch = []
                        partition.append(batch)
                    partition.state.update(next_block=last + 1, complete=True)
                    partition.save()
            run_pipeline(configuration, args.job_root, args.manifest, client, rpc, progress)
        except BaseException as error:
            progress.update(force=True, phase="stopped" if isinstance(error, KeyboardInterrupt) else "failed", error=str(error))
            raise
    if args.download_only:
        print(f"Download complete: {args.job_root}. L3 validation and candle export have not run.")
    else:
        print(f"Complete. Manifest: {args.manifest}. Candles: {args.job_root / 'candles.jsonl'}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError) as error:
        raise SystemExit(f"Error: {error}") from error
