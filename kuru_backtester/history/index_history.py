#!/usr/bin/env python3
"""Download one bounded canonical market-event partition with resume support.

This command writes a staging partition. It does not add that partition to the
validated L3 manifest. Promotion requires Envio/RPC parity, L2 checks, queue
invariants, a verified checkpoint, and content hashes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

from kuru_backtester.history.discover_envio_intervals import (
    BLOCK_FIELDS,
    DiscoveryError,
    RateLimitedHyperSyncClient,
    fallback_block_payload,
    page_timestamp_map,
    quantity_int,
)
from kuru_backtester.engine import l3_vertical_slice as l3
from kuru_backtester.engine.l3_vertical_slice import DEFAULT_HYPERSYNC_URL
from kuru_backtester.engine.rebuild_from_chain import (
    DEFAULT_RPC_URL, EVENT_SPECS, MARKET_ADDRESS, RpcClient, market_parameters,
)


from kuru_backtester.paths import ROOT
SCHEMA_VERSION = "1.0"
LOG_FIELDS = [
    "address", "block_number", "transaction_hash", "transaction_index",
    "log_index", "topic0", "topic1", "topic2", "topic3", "data",
]
EVENT_TOPICS = sorted(spec.topic for spec in EVENT_SPECS)


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def content_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def index_query(market: str, cursor: int, stop: int) -> dict[str, Any]:
    return {
        "from_block": cursor,
        "to_block": stop,
        "logs": [{"address": [market.lower()], "topics": [EVENT_TOPICS]}],
        "field_selection": {"log": LOG_FIELDS, "block": BLOCK_FIELDS},
        "join_mode": "Default",
    }


class StagingPartition:
    """Store one append-only pagination stream and its durable cursor."""

    def __init__(self, directory: Path, signature: dict[str, Any]) -> None:
        self.directory = directory
        self.events_path = directory / "events.jsonl"
        self.state_path = directory / "state.json"
        self.signature = signature
        directory.mkdir(parents=True, exist_ok=True)
        if self.state_path.exists():
            self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
            if self.state.get("signature") != signature:
                raise DiscoveryError("staging cache identity does not match this request")
        else:
            self.state = {
                "schema_version": SCHEMA_VERSION,
                "signature": signature,
                "next_block": signature["from_block"],
                "event_count": 0,
                "page_count": 0,
                "complete": False,
                "validation_status": "downloaded_unvalidated",
            }
            self.save()
        self.identities = self._read_identities()

    def _read_identities(self) -> set[tuple[int, str, int]]:
        if not self.events_path.exists():
            return set()
        identities: set[tuple[int, str, int]] = set()
        with self.events_path.open(encoding="utf-8") as source:
            for line in source:
                if line.strip():
                    item = json.loads(line)
                    identities.add((
                        int(item["block_number"]),
                        str(item["transaction_hash"]).lower(),
                        int(item["log_index"]),
                    ))
        return identities

    def save(self) -> None:
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(canonical_json(self.state) + "\n", encoding="utf-8")
        temporary.replace(self.state_path)

    def append(self, rows: list[dict[str, Any]]) -> int:
        added = 0
        with self.events_path.open("a", encoding="utf-8") as output:
            for item in rows:
                identity = (
                    int(item["block_number"]),
                    str(item["transaction_hash"]).lower(),
                    int(item["log_index"]),
                )
                if identity in self.identities:
                    continue
                output.write(canonical_json(item) + "\n")
                self.identities.add(identity)
                added += 1
        self.state["event_count"] = len(self.identities)
        return added


def canonical_page_rows(
    result: dict[str, Any], market: str, timestamp_lookup: Any
) -> list[dict[str, Any]]:
    timestamps = page_timestamp_map(result)
    output: list[dict[str, Any]] = []
    for batch in result.get("data", []):
        for log in batch.get("logs", []):
            address = str(log.get("address", "")).lower()
            if address != market.lower():
                raise DiscoveryError(f"HyperSync returned foreign market log {address}")
            block = quantity_int(log["block_number"])
            if block not in timestamps:
                timestamps[block] = timestamp_lookup(block)
            topics = [log.get(f"topic{index}") for index in range(4)]
            if str(topics[0]).lower() not in EVENT_TOPICS:
                raise DiscoveryError("HyperSync returned an event outside the requested topics")
            output.append({
                "address": address,
                "block_number": block,
                "block_timestamp": timestamps[block],
                "transaction_hash": str(log["transaction_hash"]).lower(),
                "transaction_index": quantity_int(log["transaction_index"]),
                "log_index": quantity_int(log["log_index"]),
                "topic0": str(topics[0]).lower(),
                "topic1": None if topics[1] is None else str(topics[1]).lower(),
                "topic2": None if topics[2] is None else str(topics[2]).lower(),
                "topic3": None if topics[3] is None else str(topics[3]).lower(),
                "data": str(log.get("data", "0x")).lower(),
            })
    output.sort(key=lambda item: (
        item["block_number"], item["transaction_index"], item["log_index"]
    ))
    return output


def run_index(
    client: RateLimitedHyperSyncClient,
    partition: StagingPartition,
    market: str,
    stop: int,
    progress_callback: Any = None,
) -> None:
    cursor = int(partition.state["next_block"])
    while cursor < stop:
        partition.state["next_block"] = cursor
        partition.save()
        result = client.query(
            index_query(market, cursor, stop),
            persist_cursor=partition.save,
            on_rate_limit=partition.save,
        )
        if "next_block" not in result:
            raise DiscoveryError("HyperSync page has no next_block cursor; page was not committed")
        next_block = quantity_int(result["next_block"])
        if next_block <= cursor:
            raise DiscoveryError(f"HyperSync cursor did not advance past {cursor}")
        def missing_timestamp(block: int) -> int:
            fallback = client.query(
                fallback_block_payload(block),
                persist_cursor=partition.save,
                on_rate_limit=partition.save,
            )
            timestamps = page_timestamp_map(fallback)
            if block not in timestamps:
                raise DiscoveryError(
                    f"narrow timestamp lookup returned no block {block}"
                )
            return timestamps[block]

        rows = canonical_page_rows(
            result,
            market,
            missing_timestamp,
        )
        page_end = min(next_block, stop)
        if any(not cursor <= int(row["block_number"]) < page_end for row in rows):
            raise DiscoveryError("HyperSync page contains a log outside its cursor range; page was not committed")
        added = partition.append(rows)
        cursor = min(next_block, stop)
        partition.state.update({
            "next_block": cursor,
            "page_count": int(partition.state["page_count"]) + 1,
            "complete": cursor >= stop,
        })
        partition.save()
        if progress_callback is not None:
            progress_callback(dict(partition.state))
        print(
            f"cursor={cursor} page_events={len(rows)} new_events={added} "
            f"stored_events={partition.state['event_count']}",
            flush=True,
        )
    partition.events_path.touch(exist_ok=True)
    partition.state["event_file_sha256"] = content_hash(partition.events_path)
    partition.save()


def capture_checkpoint(
    rpc_url: str, market: str, block_number: int, destination: Path
) -> str:
    """Write one exact L3 checkpoint and return its content hash."""
    rpc = RpcClient(rpc_url, timeout=180)
    book, _, _ = l3.load_checkpoint(rpc, market, block_number)
    parameters = market_parameters(rpc, market, block_number)
    timestamp = rpc.block_timestamps([block_number])[block_number]
    payload = {
        "schema_version": "1.0",
        "market": market.lower(),
        "checkpoint_block": block_number,
        "checkpoint_timestamp": timestamp,
        "parameters": {
            "price_precision": int(parameters["price_precision"]),
            "size_precision": int(parameters["size_precision"]),
            "tick_size": int(parameters["tick_size"]),
            "min_size": int(parameters["min_size"]),
            "base_decimals": int(parameters["base_decimals"]),
            "quote_decimals": int(parameters["quote_decimals"]),
        },
        "book": {
            "orders": [asdict(book.orders[key]) for key in sorted(book.orders)],
            "levels": [
                {
                    "is_buy": is_buy,
                    "price": price,
                    "head": level.head,
                    "tail": level.tail,
                    "total_size": level.total_size,
                }
                for (is_buy, price), level in sorted(
                    book.levels.items(), key=lambda item: (not item[0][0], item[0][1])
                )
            ],
        },
    }
    destination.write_text(canonical_json(payload) + "\n", encoding="utf-8")
    return content_hash(destination)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market", default=MARKET_ADDRESS)
    parser.add_argument("--from-block", type=int, required=True)
    parser.add_argument("--to-block", type=int, required=True)
    parser.add_argument("--hypersync-url", default=DEFAULT_HYPERSYNC_URL)
    parser.add_argument("--rpc-url", default=DEFAULT_RPC_URL)
    parser.add_argument("--output-root", type=Path, default=ROOT / ".cache" / "historical_index_staging")
    parser.add_argument(
        "--partition-dir",
        type=Path,
        help="Use this exact staging directory instead of a hash-named directory.",
    )
    parser.add_argument("--max-blocks", type=int, default=100_000)
    parser.add_argument("--allow-large-range", action="store_true")
    args = parser.parse_args()
    block_count = args.to_block - args.from_block + 1
    if block_count < 1:
        parser.error("to-block must be at least from-block")
    if block_count > args.max_blocks and not args.allow_large_range:
        parser.error(
            f"range has {block_count} blocks; raise --max-blocks or use --allow-large-range"
        )
    token = os.environ.get("ENVIO_API_TOKEN", "").strip()
    if not token:
        parser.error("ENVIO_API_TOKEN is not set")
    signature = {
        "schema_version": SCHEMA_VERSION,
        "market": args.market.lower(),
        "from_block": args.from_block,
        "to_block": args.to_block,
        "topics": EVENT_TOPICS,
        "log_fields": LOG_FIELDS,
        "block_fields": BLOCK_FIELDS,
        "hypersync_url": args.hypersync_url.rstrip("/"),
    }
    identity = hashlib.sha256(canonical_json(signature).encode()).hexdigest()[:20]
    partition_directory = args.partition_dir or (args.output_root / identity)
    partition = StagingPartition(partition_directory, signature)
    client = RateLimitedHyperSyncClient(args.hypersync_url, token)
    run_index(client, partition, args.market, args.to_block + 1)
    checkpoint_path = partition.directory / "checkpoint.json"
    checkpoint_sha256 = capture_checkpoint(
        args.rpc_url, args.market, args.from_block - 1, checkpoint_path
    )
    partition.state.update({
        "checkpoint_block": args.from_block - 1,
        "checkpoint_path": checkpoint_path.name,
        "checkpoint_sha256": checkpoint_sha256,
    })
    partition.save()
    print(f"Staging partition: {partition.directory}")
    print(
        f"Checkpoint: block={args.from_block - 1} sha256={checkpoint_sha256}"
    )
    print("Status: downloaded_unvalidated. It is not available to backtests.")
    print("Next gate: Envio/RPC parity, L2 checks, queue invariants, and checkpoint hash.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
