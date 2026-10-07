"""Generate a separate validation report for the verified Kuru L3 slice."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from l3_vertical_slice import (
    DEFAULT_FROM_BLOCK,
    DEFAULT_HYPERSYNC_URL,
    DEFAULT_RPC_URL,
    HyperSyncClient,
    HyperSyncError,
    L3Book,
    ReplayEvent,
    apply_event,
    choose_and_run_passive_order,
    count_events,
    decode_events,
    get_contract_midpoint,
    get_l2_book,
    load_checkpoint,
    log_identity,
    normalize_rpc_log,
)
from rebuild_from_chain import (
    MARKET_ADDRESS,
    ReconstructionError,
    RpcClient,
    RpcError,
    market_parameters,
)


DEFAULT_TO_BLOCK = 44_856_100
DEFAULT_OUTPUT_DIR = Path("validation_output")
DEFAULT_INTERVAL_MANIFEST = Path("interval_manifest.json")
SCHEMA_VERSION = "1.0"


@dataclass(frozen=True)
class LevelErrors:
    maximum_price_error_raw: int
    maximum_size_error_raw: int
    missing_local_levels: int
    extra_local_levels: int


@dataclass
class SimulatedOrderState:
    order_id: str
    side: str
    price_raw: int
    size_raw: int
    remaining_size_raw: int
    queue_ahead_orders: list[int]
    queue_ahead_size_raw: int
    placed_block: int
    placed_transaction_index: int
    status: str = "active"


def json_ready(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, dict):
        return {str(key): json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_ready(item) for item in value]
    return value


def stable_json(value: Any, *, pretty: bool = False) -> str:
    return json.dumps(
        json_ready(value),
        sort_keys=True,
        indent=2 if pretty else None,
        separators=None if pretty else (",", ":"),
        allow_nan=False,
    )


def compute_level_errors(
    local: dict[int, int], official: dict[int, int]
) -> LevelErrors:
    local_prices = sorted(local)
    official_prices = sorted(official)
    paired_price_errors = [
        abs(left - right)
        for left, right in zip(local_prices, official_prices, strict=False)
    ]
    maximum_price_error = max(paired_price_errors, default=0)
    maximum_size_error = max(
        (
            abs(local.get(price, 0) - official.get(price, 0))
            for price in set(local) | set(official)
        ),
        default=0,
    )
    return LevelErrors(
        maximum_price_error_raw=maximum_price_error,
        maximum_size_error_raw=maximum_size_error,
        missing_local_levels=len(set(official) - set(local)),
        extra_local_levels=len(set(local) - set(official)),
    )


def combine_errors(*errors: LevelErrors) -> LevelErrors:
    return LevelErrors(
        maximum_price_error_raw=max(
            (item.maximum_price_error_raw for item in errors), default=0
        ),
        maximum_size_error_raw=max(
            (item.maximum_size_error_raw for item in errors), default=0
        ),
        missing_local_levels=sum(item.missing_local_levels for item in errors),
        extra_local_levels=sum(item.extra_local_levels for item in errors),
    )


def explicit_quote(
    book: L3Book, price_precision: int
) -> tuple[float | None, float | None, float | None]:
    bids, asks = book.aggregate()
    best_bid = max(bids) / price_precision if bids else None
    best_ask = min(asks) / price_precision if asks else None
    midpoint = (
        (best_bid + best_ask) / 2
        if best_bid is not None and best_ask is not None
        else None
    )
    return best_bid, best_ask, midpoint


def marked_value(cash: float, inventory: float, midpoint: float | None) -> float:
    return cash if midpoint is None else cash + inventory * midpoint


def active_order_payload(order: SimulatedOrderState | None) -> list[dict[str, Any]]:
    if order is None or order.status != "active":
        return []
    return [
        {
            "order_id": order.order_id,
            "side": order.side,
            "price_raw": order.price_raw,
            "size_raw": order.size_raw,
            "remaining_size_raw": order.remaining_size_raw,
            "queue_ahead_order_ids": list(order.queue_ahead_orders),
            "queue_ahead_size_raw": order.queue_ahead_size_raw,
            "status": order.status,
        }
    ]


def timeline_record(
    sequence: int,
    timestamp: int,
    block: int,
    transaction_index: int,
    log_index: int | None,
    record_type: str,
    market_event: dict[str, Any] | None,
    book: L3Book,
    price_precision: int,
    simulated_order: SimulatedOrderState | None,
    fills: list[dict[str, Any]],
    cancellations: list[dict[str, Any]],
    cash: float,
    inventory: float,
) -> dict[str, Any]:
    best_bid, best_ask, midpoint = explicit_quote(book, price_precision)
    portfolio_value = marked_value(cash, inventory, midpoint)
    return {
        "schema_version": SCHEMA_VERSION,
        "sequence": sequence,
        "timestamp": datetime.fromtimestamp(timestamp, tz=timezone.utc),
        "block_number": block,
        "transaction_index": transaction_index,
        "log_index": log_index,
        "record_type": record_type,
        "market_event": market_event,
        "market": {
            "best_bid": best_bid,
            "best_ask": best_ask,
            "midpoint": midpoint,
            "source": "reconstructed_explicit_l2",
        },
        "active_simulated_orders": active_order_payload(simulated_order),
        "fills": fills,
        "cancellations": cancellations,
        "cash": cash,
        "inventory": inventory,
        "marked_portfolio_value": portfolio_value,
        "cumulative_pnl": portfolio_value,
    }


def event_parity(
    hypersync_logs: Sequence[dict[str, Any]] | None,
    rpc_logs: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    if hypersync_logs is None:
        return {
            "status": "unavailable",
            "envio_event_count": None,
            "rpc_event_count": len(rpc_logs),
            "missing_from_envio": None,
            "missing_from_rpc": None,
        }
    envio = Counter(log_identity(log) for log in hypersync_logs)
    rpc = Counter(log_identity(log) for log in rpc_logs)
    missing_from_envio = sum((rpc - envio).values())
    missing_from_rpc = sum((envio - rpc).values())
    return {
        "status": "pass" if not missing_from_envio and not missing_from_rpc else "fail",
        "envio_event_count": len(hypersync_logs),
        "rpc_event_count": len(rpc_logs),
        "missing_from_envio": missing_from_envio,
        "missing_from_rpc": missing_from_rpc,
    }


def cancellation_summary_audit(events: Sequence[ReplayEvent]) -> dict[str, Any]:
    individual: dict[str, Counter[int]] = defaultdict(Counter)
    summarized: dict[str, Counter[int]] = defaultdict(Counter)
    summary_event_count = 0
    zero_sentinel_count = 0
    for event in events:
        if event.name == "OrderCanceled":
            individual[event.transaction_hash][int(event.args["order_id"])] += 1
        elif event.name in {"OrdersCanceled", "FlipOrdersCanceled"}:
            summary_event_count += 1
            for value in event.args["order_ids"]:
                order_id = int(value)
                if order_id == 0:
                    zero_sentinel_count += 1
                    continue
                summarized[event.transaction_hash][order_id] += 1
    missing_individual = 0
    individual_not_summarized = 0
    duplicate_individual = 0
    for transaction_hash in set(individual) | set(summarized):
        individual_ids = individual[transaction_hash]
        summarized_ids = summarized[transaction_hash]
        missing_individual += sum((summarized_ids - individual_ids).values())
        individual_not_summarized += sum((individual_ids - summarized_ids).values())
        duplicate_individual += sum(
            count - 1 for count in individual_ids.values() if count > 1
        )
    status = (
        "pass"
        if missing_individual == 0
        and individual_not_summarized == 0
        and duplicate_individual == 0
        else "fail"
    )
    return {
        "status": status,
        "summary_event_count": summary_event_count,
        "summary_referenced_nonzero_order_ids": sum(
            sum(ids.values()) for ids in summarized.values()
        ),
        "summary_zero_sentinel_count": zero_sentinel_count,
        "individual_order_cancellation_count": sum(
            sum(ids.values()) for ids in individual.values()
        ),
        "summary_ids_missing_individual_cancellation": missing_individual,
        "individual_cancellations_not_in_summary": individual_not_summarized,
        "duplicate_individual_cancellation_count": duplicate_individual,
        "summary_order_ids_applied_as_cancellations": 0,
        "replay_rule": (
            "OrdersCanceled and FlipOrdersCanceled are summary-only; "
            "only OrderCanceled mutates the L3 book"
        ),
    }


def validation_blocks(
    events: Iterable[ReplayEvent],
    first: int,
    last: int,
    point_count: int = 7,
) -> list[int]:
    del events
    if point_count < 7:
        raise ValueError("validation requires at least seven points")
    if last < first:
        raise ValueError("validation block range is invalid")
    if first == last:
        return [first]
    return sorted(
        {
            first + (last - first) * index // (point_count - 1)
            for index in range(point_count)
        }
    )


def load_named_interval(
    manifest_path: Path, interval_name: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    intervals = payload.get("intervals", {})
    if interval_name not in intervals:
        available = ", ".join(sorted(intervals)) or "none"
        raise ValueError(
            f"interval {interval_name!r} is not in {manifest_path}; available: {available}"
        )
    interval = dict(intervals[interval_name])
    interval["name"] = interval_name
    interval["market"] = str(payload["market"]).lower()
    interval["size_precision"] = int(payload["size_precision"])
    interval["trade_cache"] = str(payload["trade_cache"])
    return payload, interval


def fetch_rpc_logs_chunked(
    rpc: RpcClient,
    market: str,
    first_block: int,
    last_block: int,
    chunk_blocks: int = 500,
) -> list[dict[str, Any]]:
    if chunk_blocks <= 0:
        raise ValueError("RPC log chunk size must be positive")
    logs: list[dict[str, Any]] = []
    for chunk_first in range(first_block, last_block + 1, chunk_blocks):
        chunk_last = min(last_block, chunk_first + chunk_blocks - 1)
        logs.extend(rpc.logs(market, chunk_first, chunk_last))
    return sorted(
        logs,
        key=lambda log: (
            normalize_rpc_log(log)["blockNumber"],
            normalize_rpc_log(log)["transactionIndex"],
            normalize_rpc_log(log)["logIndex"],
        ),
    )


def _cache_path(manifest_path: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else manifest_path.parent / path


def load_cached_envio_logs(
    manifest_path: Path,
    interval: Mapping[str, Any],
    rpc_logs: Sequence[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[int, int]]:
    first_block = int(interval["from_block"])
    last_block = int(interval["to_block"])
    cache_paths = [
        _cache_path(manifest_path, str(interval["trade_cache"])),
        _cache_path(manifest_path, str(interval["order_event_cache"])),
    ]
    cached: dict[tuple[int, str, int], dict[str, Any]] = {}
    timestamps: dict[int, int] = {}
    for path in cache_paths:
        if not path.exists():
            raise ReconstructionError(f"Envio discovery cache is missing: {path}")
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                block_number = int(row["block_number"])
                if not first_block <= block_number <= last_block:
                    continue
                key = (
                    block_number,
                    str(row["transaction_hash"]).lower(),
                    int(row["log_index"]),
                )
                timestamp = int(row["block_timestamp"])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                raise ReconstructionError(
                    f"Malformed Envio cache row {path}:{line_number}: {error}"
                ) from error
            existing = cached.get(key)
            if existing is not None and existing != row:
                raise ReconstructionError(f"Conflicting cached Envio event {key}")
            cached[key] = row
            prior_timestamp = timestamps.get(block_number)
            if prior_timestamp is not None and prior_timestamp != timestamp:
                raise ReconstructionError(
                    f"Conflicting cached timestamps for block {block_number}"
                )
            timestamps[block_number] = timestamp

    rpc_by_key: dict[tuple[int, str, int], dict[str, Any]] = {}
    for log in rpc_logs:
        normalized = normalize_rpc_log(log)
        key = (
            int(normalized["blockNumber"]),
            str(normalized["transactionHash"]),
            int(normalized["logIndex"]),
        )
        if key in rpc_by_key:
            raise ReconstructionError(f"Duplicate RPC event {key}")
        rpc_by_key[key] = normalized
    missing_from_cache = sorted(set(rpc_by_key) - set(cached))
    missing_from_rpc = sorted(set(cached) - set(rpc_by_key))
    if missing_from_cache or missing_from_rpc:
        raise ReconstructionError(
            "Cached Envio/RPC event keys differ: "
            f"missing_from_cache={len(missing_from_cache)}, "
            f"missing_from_rpc={len(missing_from_rpc)}"
        )

    logs: list[dict[str, Any]] = []
    names = Counter()
    market = str(interval["market"]).lower()
    for key in sorted(
        cached,
        key=lambda item: (
            item[0],
            rpc_by_key[item]["transactionIndex"],
            item[2],
        ),
    ):
        row = cached[key]
        rpc_log = rpc_by_key[key]
        if str(row["address"]).lower() != market:
            raise ReconstructionError(f"Cached Envio event {key} has a foreign address")
        if (
            str(row["topic0"]).lower() != rpc_log["topics"][0]
            or str(row["data"]).lower() != rpc_log["data"]
            or str(row["address"]).lower() != rpc_log["address"]
        ):
            raise ReconstructionError(f"Cached Envio payload differs from RPC for {key}")
        names[str(row["name"])] += 1
        logs.append(
            {
                "address": str(row["address"]),
                "block_number": key[0],
                "block_timestamp": int(row["block_timestamp"]),
                "transaction_hash": key[1],
                "transaction_index": int(rpc_log["transactionIndex"]),
                "log_index": key[2],
                "data": str(row["data"]),
                "topic0": str(row["topic0"]),
                "topic1": None,
                "topic2": None,
                "topic3": None,
            }
        )
    if names["Trade"] != int(interval["trade_count"]):
        raise ReconstructionError(
            f"Cached Trade count is {names['Trade']}; expected {interval['trade_count']}"
        )
    order_count = sum(count for name, count in names.items() if name != "Trade")
    if order_count != int(interval["order_event_count"]):
        raise ReconstructionError(
            f"Cached order-event count is {order_count}; "
            f"expected {interval['order_event_count']}"
        )
    return logs, timestamps


def is_worse_trade(
    event: ReplayEvent, simulated_order: SimulatedOrderState, price_precision: int
) -> bool:
    if event.name != "Trade":
        return False
    taker_is_buy = simulated_order.side == "sell"
    if bool(event.args["is_buy"]) != taker_is_buy:
        return False
    trade_price = int(event.args["price"])
    simulated_price = simulated_order.price_raw * 10**18 // price_precision
    return trade_price > simulated_price if taker_is_buy else trade_price < simulated_price


def update_ahead_before_event(
    book: L3Book, event: ReplayEvent, simulated_order: SimulatedOrderState
) -> None:
    if event.name == "OrderCanceled":
        order_id = int(event.args["order_id"])
        if order_id in simulated_order.queue_ahead_orders:
            order = book.orders.get(order_id)
            if order is not None:
                simulated_order.queue_ahead_size_raw -= order.size
            simulated_order.queue_ahead_orders.remove(order_id)
    elif event.name == "Trade" and int(event.args["order_id"]) != 0:
        order_id = int(event.args["order_id"])
        if order_id in simulated_order.queue_ahead_orders:
            simulated_order.queue_ahead_size_raw -= int(event.args["filled_size"])
            if int(event.args["updated_size"]) == 0:
                simulated_order.queue_ahead_orders.remove(order_id)
    if simulated_order.queue_ahead_size_raw < 0:
        raise ReconstructionError("Simulated queue-ahead size became negative.")


def build_report_data(
    rpc: RpcClient,
    market: str,
    first_block: int,
    last_block: int,
    events: Sequence[ReplayEvent],
    rpc_logs: Sequence[dict[str, Any]],
    hypersync_logs: Sequence[dict[str, Any]] | None,
    *,
    interval_name: str | None = None,
    interval_metadata: Mapping[str, Any] | None = None,
    source_timestamps: Mapping[int, int] | None = None,
    validation_point_count: int = 7,
    event_source: str | None = None,
    event_input: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, list[Any]]]:
    params = market_parameters(rpc, market)
    price_precision = int(params["price_precision"])
    size_precision = int(params["size_precision"])
    min_size = int(params["min_size"])
    checkpoint_block = first_block - 1
    checkpoint_book, checkpoint_bids, checkpoint_asks = load_checkpoint(
        rpc, market, checkpoint_block
    )
    checkpoint_local_bids, checkpoint_local_asks = checkpoint_book.aggregate()
    checkpoint_errors = combine_errors(
        compute_level_errors(checkpoint_local_bids, checkpoint_bids),
        compute_level_errors(checkpoint_local_asks, checkpoint_asks),
    )
    checkpoint_book.check_invariants()

    final_mark = get_contract_midpoint(rpc, market, last_block)
    passive = choose_and_run_passive_order(
        checkpoint_book,
        events,
        price_precision,
        size_precision,
        min_size,
        final_mark,
    )

    check_blocks = validation_blocks(
        events, first_block, last_block, validation_point_count
    )
    event_blocks = {event.block_number for event in events}
    required_timestamp_blocks = event_blocks | set(check_blocks) | {checkpoint_block}
    timestamps = {
        int(block): int(timestamp)
        for block, timestamp in (source_timestamps or {}).items()
        if block in required_timestamp_blocks
    }
    missing_timestamp_blocks = required_timestamp_blocks - set(timestamps)
    if missing_timestamp_blocks:
        timestamps.update(rpc.block_timestamps(missing_timestamp_blocks))
    events_by_block: dict[int, list[ReplayEvent]] = defaultdict(list)
    for event in events:
        events_by_block[event.block_number].append(event)

    book = checkpoint_book.copy()
    timeline: list[dict[str, Any]] = []
    plot_rows: list[dict[str, Any]] = []
    validation_rows: list[dict[str, Any]] = []
    trade_rows: list[dict[str, Any]] = []
    invariant_checks = 1
    invariant_failures = 0
    sequence = 0
    cash = 0.0
    inventory = 0.0
    simulated_order: SimulatedOrderState | None = None
    placement_row: dict[str, Any] | None = None
    fill_rows: list[dict[str, Any]] = []

    for block in sorted(event_blocks | set(check_blocks)):
        timestamp = timestamps[block]
        for event in events_by_block.get(block, []):
            if (
                simulated_order is None
                and event.transaction_hash == passive.placed_before_transaction
            ):
                is_buy = passive.side == "buy"
                queue_ids = book.queue(is_buy, passive.price_raw)
                simulated_order = SimulatedOrderState(
                    order_id="sim-passive-1",
                    side=passive.side,
                    price_raw=passive.price_raw,
                    size_raw=passive.size_raw,
                    remaining_size_raw=passive.size_raw,
                    queue_ahead_orders=queue_ids,
                    queue_ahead_size_raw=sum(book.orders[item].size for item in queue_ids),
                    placed_block=event.block_number,
                    placed_transaction_index=event.transaction_index,
                )
                sequence += 1
                timeline.append(
                    timeline_record(
                        sequence,
                        timestamp,
                        event.block_number,
                        event.transaction_index,
                        None,
                        "simulated_order_placement",
                        None,
                        book,
                        price_precision,
                        simulated_order,
                        [],
                        [],
                        cash,
                        inventory,
                    )
                )
                placement_row = {
                    "block_number": block,
                    "timestamp": timestamp,
                    "price": passive.price_raw / price_precision,
                }

            if simulated_order is not None and simulated_order.status == "active":
                if simulated_order.queue_ahead_size_raw == 0 and is_worse_trade(
                    event, simulated_order, price_precision
                ):
                    fill_size = min(
                        simulated_order.remaining_size_raw,
                        int(event.args["filled_size"]),
                    )
                    fill_base = fill_size / size_precision
                    fill_price = simulated_order.price_raw / price_precision
                    if simulated_order.side == "buy":
                        inventory += fill_base
                        cash -= fill_base * fill_price
                    else:
                        inventory -= fill_base
                        cash += fill_base * fill_price
                    simulated_order.remaining_size_raw -= fill_size
                    if simulated_order.remaining_size_raw == 0:
                        simulated_order.status = "filled"
                    fills = [
                        {
                            "order_id": simulated_order.order_id,
                            "side": simulated_order.side,
                            "price": fill_price,
                            "price_raw": simulated_order.price_raw,
                            "size": fill_base,
                            "size_raw": fill_size,
                            "trigger_transaction_hash": event.transaction_hash,
                            "trigger_log_index": event.log_index,
                        }
                    ]
                    sequence += 1
                    timeline.append(
                        timeline_record(
                            sequence,
                            timestamp,
                            event.block_number,
                            event.transaction_index,
                            event.log_index,
                            "simulated_fill",
                            None,
                            book,
                            price_precision,
                            simulated_order,
                            fills,
                            [],
                            cash,
                            inventory,
                        )
                    )
                    fill_rows.append(
                        {
                            "block_number": block,
                            "timestamp": timestamp,
                            "price": fill_price,
                            "size_raw": fill_size,
                        }
                    )
                update_ahead_before_event(book, event, simulated_order)

            apply_event(book, event, price_precision)
            try:
                book.check_invariants()
            except ReconstructionError:
                invariant_failures += 1
                raise
            invariant_checks += 1
            sequence += 1
            record = timeline_record(
                sequence,
                timestamp,
                event.block_number,
                event.transaction_index,
                event.log_index,
                "market_event",
                {"name": event.name, "args": event.args},
                book,
                price_precision,
                simulated_order,
                [],
                [],
                cash,
                inventory,
            )
            timeline.append(record)
            quote = record["market"]
            plot_rows.append(
                {
                    "block_number": block,
                    "timestamp": timestamp,
                    "best_bid": quote["best_bid"],
                    "best_ask": quote["best_ask"],
                    "midpoint": quote["midpoint"],
                }
            )
            if event.name == "Trade":
                trade_rows.append(
                    {
                        "block_number": block,
                        "timestamp": timestamp,
                        "price": int(event.args["price"]) / 10**18,
                        "order_id": int(event.args["order_id"]),
                    }
                )

        if block in check_blocks:
            returned_block, official_bids, official_asks = get_l2_book(rpc, market, block)
            local_bids, local_asks = book.aggregate()
            errors = combine_errors(
                compute_level_errors(local_bids, official_bids),
                compute_level_errors(local_asks, official_asks),
            )
            local_bid = max(local_bids) / price_precision if local_bids else None
            local_ask = min(local_asks) / price_precision if local_asks else None
            official_bid = max(official_bids) / price_precision if official_bids else None
            official_ask = min(official_asks) / price_precision if official_asks else None
            local_midpoint = (
                (local_bid + local_ask) / 2
                if local_bid is not None and local_ask is not None
                else None
            )
            official_midpoint = (
                (official_bid + official_ask) / 2
                if official_bid is not None and official_ask is not None
                else None
            )
            midpoint_error = (
                abs(local_midpoint - official_midpoint)
                if local_midpoint is not None and official_midpoint is not None
                else None
            )
            validation_rows.append(
                {
                    "block_number": block,
                    "timestamp": timestamp,
                    "returned_block": returned_block,
                    "reconstructed_midpoint": local_midpoint,
                    "official_midpoint": official_midpoint,
                    "midpoint_error": midpoint_error,
                    "maximum_price_error_raw": errors.maximum_price_error_raw,
                    "maximum_size_error_raw": errors.maximum_size_error_raw,
                    "missing_local_levels": errors.missing_local_levels,
                    "extra_local_levels": errors.extra_local_levels,
                    "bid_levels": len(official_bids),
                    "ask_levels": len(official_asks),
                }
            )

    total_filled = sum(row["size_raw"] for row in fill_rows)
    if placement_row is None or total_filled != passive.filled_size_raw:
        raise ReconstructionError(
            f"Report overlay filled {total_filled}; verified overlay filled "
            f"{passive.filled_size_raw}."
        )
    final_quote = explicit_quote(book, price_precision)
    final_portfolio = marked_value(cash, inventory, final_quote[2])
    all_price_errors = [row["maximum_price_error_raw"] for row in validation_rows]
    all_size_errors = [row["maximum_size_error_raw"] for row in validation_rows]
    parity = event_parity(hypersync_logs, rpc_logs)
    cancellation_audit = cancellation_summary_audit(events)
    if cancellation_audit["status"] != "pass":
        raise ReconstructionError(
            "Cancellation summary audit failed: "
            f"{stable_json(cancellation_audit)}"
        )
    discovery = dict(interval_metadata or {})
    summary = {
        "schema_version": SCHEMA_VERSION,
        "market": market.lower(),
        "interval": {
            "name": interval_name,
            "from_block": first_block,
            "to_block": last_block,
            "inclusive_block_count": last_block - first_block + 1,
            "discovery_trade_count": discovery.get("trade_count"),
            "executed_volume_raw": discovery.get("executed_volume_raw"),
            "executed_volume_mon": discovery.get("executed_volume_mon"),
            "trade_price_movement_percent": discovery.get(
                "price_movement_percent"
            ),
            "discovery_order_event_count": discovery.get("order_event_count"),
        },
        "event_source": event_source
        or ("envio_hypersync" if hypersync_logs is not None else "rpc_fallback"),
        "event_input": dict(event_input or {}),
        "event_counts": count_events(events),
        "event_parity": parity,
        "cancellation_summary_audit": cancellation_audit,
        "checkpoint": {
            "block_number": checkpoint_block,
            "active_order_count": len(checkpoint_book.orders),
            "bid_level_count": len(checkpoint_bids),
            "ask_level_count": len(checkpoint_asks),
            "matched_level_count": len(checkpoint_bids) + len(checkpoint_asks),
            "missing_local_levels": checkpoint_errors.missing_local_levels,
            "extra_local_levels": checkpoint_errors.extra_local_levels,
            "maximum_price_error_raw": checkpoint_errors.maximum_price_error_raw,
            "maximum_size_error_raw": checkpoint_errors.maximum_size_error_raw,
        },
        "validation": {
            "validated_block_count": len(validation_rows),
            "validated_blocks": [row["block_number"] for row in validation_rows],
            "maximum_midpoint_error": max(
                (row["midpoint_error"] or 0 for row in validation_rows), default=0
            ),
            "maximum_price_level_error_raw": max(all_price_errors, default=0),
            "maximum_price_level_error": max(all_price_errors, default=0)
            / price_precision,
            "maximum_aggregate_size_error_raw": max(all_size_errors, default=0),
            "maximum_aggregate_size_error": max(all_size_errors, default=0)
            / size_precision,
            "missing_local_levels": sum(
                row["missing_local_levels"] for row in validation_rows
            ),
            "extra_local_levels": sum(
                row["extra_local_levels"] for row in validation_rows
            ),
            "queue_invariant_checks": invariant_checks,
            "queue_invariant_failures": invariant_failures,
            "queue_invariant_status": "pass" if invariant_failures == 0 else "fail",
        },
        "passive_order": asdict(passive),
        "final_result": {
            "cash": cash,
            "inventory": inventory,
            "explicit_midpoint": final_quote[2],
            "combined_contract_midpoint": final_mark,
            "marked_portfolio_value": final_portfolio,
            "cumulative_pnl": final_portfolio,
            "gross_pnl_at_combined_contract_midpoint": passive.gross_pnl,
            "fees_included": False,
            "gas_included": False,
        },
        "timeline": {
            "record_count": len(timeline),
            "first_timestamp": timeline[0]["timestamp"],
            "last_timestamp": timeline[-1]["timestamp"],
            "backend_is_complete": True,
            "playback_delay_seconds": 0,
        },
        "ohlc": {
            "generated": False,
            "required_source_if_added": "Trade events only",
        },
    }
    plot_data = {
        "book": plot_rows,
        "trades": trade_rows,
        "validations": validation_rows,
        "placement": [placement_row],
        "fills": fill_rows,
    }
    return summary, timeline, plot_data


def plot_prices(data: dict[str, list[Any]], destination: Path) -> None:
    book = data["book"]
    trades = data["trades"]
    validations = data["validations"]
    placement = data["placement"][0]
    fills = data["fills"]
    figure, axis = plt.subplots(figsize=(13, 7))
    x = [row["block_number"] for row in book]
    axis.step(
        x,
        [row["best_bid"] for row in book],
        where="post",
        label="Reconstructed best bid",
        linewidth=1.2,
    )
    axis.step(
        x,
        [row["best_ask"] for row in book],
        where="post",
        label="Reconstructed best ask",
        linewidth=1.2,
    )
    axis.step(
        x,
        [row["midpoint"] for row in book],
        where="post",
        label="Reconstructed midpoint",
        linewidth=1.5,
    )
    axis.scatter(
        [row["block_number"] for row in trades],
        [row["price"] for row in trades],
        marker="x",
        s=55,
        label="Trade-event price",
        zorder=5,
    )
    axis.scatter(
        [row["block_number"] for row in validations],
        [row["official_midpoint"] for row in validations],
        facecolors="none",
        edgecolors="black",
        s=70,
        label="Official getL2Book midpoint",
        zorder=6,
    )
    axis.scatter(
        [placement["block_number"]],
        [placement["price"]],
        marker="^",
        s=110,
        label="Simulated placement",
        zorder=7,
    )
    axis.scatter(
        [row["block_number"] for row in fills],
        [row["price"] for row in fills],
        marker="*",
        s=160,
        label="Simulated fill",
        zorder=8,
    )
    axis.set_title("Kuru MON-USDC reconstructed book and Trade events")
    axis.set_xlabel("Monad block")
    axis.set_ylabel("USDC per MON")
    axis.grid(alpha=0.25)
    axis.legend(loc="best", ncol=2)
    figure.tight_layout()
    figure.savefig(destination, dpi=160)
    plt.close(figure)


def plot_errors(data: dict[str, list[Any]], destination: Path) -> None:
    validations = data["validations"]
    blocks = [row["block_number"] for row in validations]
    figure, axes = plt.subplots(3, 1, figsize=(12, 8), sharex=True)
    axes[0].plot(
        blocks, [row["midpoint_error"] for row in validations], marker="o"
    )
    axes[0].set_ylabel("Midpoint error")
    axes[1].plot(
        blocks,
        [row["maximum_price_error_raw"] for row in validations],
        marker="o",
    )
    axes[1].set_ylabel("Max price error (raw)")
    axes[2].plot(
        blocks,
        [row["maximum_size_error_raw"] for row in validations],
        marker="o",
    )
    axes[2].set_ylabel("Max size error (raw)")
    axes[2].set_xlabel("Monad block")
    for axis in axes:
        axis.grid(alpha=0.25)
        axis.axhline(0, color="black", linewidth=0.8)
    figure.suptitle("Reconstruction error at official getL2Book checks")
    figure.tight_layout()
    figure.savefig(destination, dpi=160)
    plt.close(figure)


def report_markdown(summary: dict[str, Any], artifacts: dict[str, str]) -> str:
    parity = summary["event_parity"]
    checkpoint = summary["checkpoint"]
    validation = summary["validation"]
    passive = summary["passive_order"]
    final = summary["final_result"]
    cancellation_audit = summary["cancellation_summary_audit"]
    return f"""# Reconstruction Validation Report

Market: `{summary['market']}`

Interval: `{summary['interval']['from_block']}` through `{summary['interval']['to_block']}`, inclusive.

Named interval: `{summary['interval']['name']}`

Discovered executed volume: `{summary['interval']['executed_volume_mon']}` MON (`{summary['interval']['executed_volume_raw']}` raw)

## Numerical results

- Event source: `{summary['event_source']}`
- Envio/RPC parity status: `{parity['status']}`
- Envio event count: `{parity['envio_event_count']}`
- RPC event count: `{parity['rpc_event_count']}`
- Events missing from Envio: `{parity['missing_from_envio']}`
- Events missing from RPC: `{parity['missing_from_rpc']}`
- Checkpoint active orders: `{checkpoint['active_order_count']}`
- Checkpoint matched levels: `{checkpoint['matched_level_count']}`
- Checkpoint missing levels: `{checkpoint['missing_local_levels']}`
- Checkpoint extra levels: `{checkpoint['extra_local_levels']}`
- Validated blocks: `{validation['validated_block_count']}`
- Validated block numbers: `{validation['validated_blocks']}`
- Maximum midpoint error: `{validation['maximum_midpoint_error']}`
- Maximum price-level error: `{validation['maximum_price_level_error']}` (`{validation['maximum_price_level_error_raw']}` raw)
- Maximum aggregate-size error: `{validation['maximum_aggregate_size_error']}` (`{validation['maximum_aggregate_size_error_raw']}` raw)
- Queue-invariant checks: `{validation['queue_invariant_checks']}`
- Queue-invariant failures: `{validation['queue_invariant_failures']}`
- Queue-invariant status: `{validation['queue_invariant_status']}`
- Cancellation-summary status: `{cancellation_audit['status']}`
- OrdersCanceled summary events: `{cancellation_audit['summary_event_count']}`
- Summary nonzero order IDs: `{cancellation_audit['summary_referenced_nonzero_order_ids']}`
- Individual OrderCanceled events: `{cancellation_audit['individual_order_cancellation_count']}`
- Summary order IDs applied as cancellations: `{cancellation_audit['summary_order_ids_applied_as_cancellations']}`

Envio parity is `unavailable` when `ENVIO_API_TOKEN` is not set. This state is not a parity pass.

## Simulated passive order

- Side: `{passive['side']}`
- Raw price: `{passive['price_raw']}`
- Raw size: `{passive['size_raw']}`
- Orders ahead: `{passive['queue_ahead_orders']}`
- Raw size ahead: `{passive['queue_ahead_size_raw']}`
- Raw filled size: `{passive['filled_size_raw']}`
- Placement transaction: `{passive['placed_before_transaction']}`
- Final cash: `{final['cash']}`
- Final inventory: `{final['inventory']}`
- Final marked portfolio value: `{final['marked_portfolio_value']}`
- Final cumulative P&L: `{final['cumulative_pnl']}`
- Gross P&L at the combined contract midpoint: `{final['gross_pnl_at_combined_contract_midpoint']}`

Fees and gas are not included.

## Artifacts

- Price validation chart: `{artifacts['price_chart']}`
- Error chart: `{artifacts['error_chart']}`
- Final summary: `{artifacts['summary']}`
- Deterministic replay timeline: `{artifacts['timeline']}`
- Validation rows: `{artifacts['validation_rows']}`
- Artifact manifest: `{artifacts['manifest']}`

No OHLC candles were generated. If candles are added later, their open, high, low, and close values must come only from `Trade` events.
"""


def write_outputs(
    output_dir: Path,
    summary: dict[str, Any],
    timeline: list[dict[str, Any]],
    plot_data: dict[str, list[Any]],
    source_logs: Sequence[dict[str, Any]],
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "price_chart": output_dir / "reconstructed_prices.png",
        "error_chart": output_dir / "reconstruction_error.png",
        "summary": output_dir / "summary.json",
        "timeline": output_dir / "timeline.jsonl",
        "source_events": output_dir / "source_events.jsonl",
        "validation_rows": output_dir / "validation_rows.json",
        "report": output_dir / "report.md",
        "manifest": output_dir / "manifest.json",
    }
    plot_prices(plot_data, paths["price_chart"])
    plot_errors(plot_data, paths["error_chart"])
    paths["summary"].write_text(
        stable_json(summary, pretty=True) + "\n", encoding="utf-8"
    )
    paths["timeline"].write_text(
        "".join(stable_json(record) + "\n" for record in timeline),
        encoding="utf-8",
    )
    paths["source_events"].write_text(
        "".join(stable_json(log) + "\n" for log in source_logs),
        encoding="utf-8",
    )
    paths["validation_rows"].write_text(
        stable_json(plot_data["validations"], pretty=True) + "\n",
        encoding="utf-8",
    )
    relative = {key: path.name for key, path in paths.items()}
    paths["report"].write_text(
        report_markdown(summary, relative) + "\n", encoding="utf-8"
    )
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "files": {
            key: {
                "path": path.name,
                "size_bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            for key, path in paths.items()
            if key != "manifest"
        },
    }
    paths["manifest"].write_text(
        stable_json(manifest, pretty=True) + "\n", encoding="utf-8"
    )
    return paths


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a separate validation report for the verified L3 slice."
    )
    parser.add_argument("--from-block", type=int, default=DEFAULT_FROM_BLOCK)
    parser.add_argument("--to-block", type=int, default=DEFAULT_TO_BLOCK)
    parser.add_argument("--interval")
    parser.add_argument(
        "--interval-manifest", type=Path, default=DEFAULT_INTERVAL_MANIFEST
    )
    parser.add_argument("--validation-points", type=int, default=7)
    parser.add_argument("--rpc-log-chunk-blocks", type=int, default=500)
    parser.add_argument("--market", default=MARKET_ADDRESS)
    parser.add_argument("--rpc-url", default=DEFAULT_RPC_URL)
    parser.add_argument("--hypersync-url", default=DEFAULT_HYPERSYNC_URL)
    parser.add_argument("--output-dir", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    interval_metadata: dict[str, Any] | None = None
    interval_name: str | None = None
    if args.interval:
        _, interval_metadata = load_named_interval(
            args.interval_manifest, args.interval
        )
        interval_name = args.interval
        args.market = str(interval_metadata["market"])
        args.from_block = int(interval_metadata["from_block"])
        args.to_block = int(interval_metadata["to_block"])
    if args.to_block < args.from_block:
        raise SystemExit("Error: invalid block range.")
    if args.validation_points < 7:
        raise SystemExit("Error: validation requires at least seven points.")
    output_dir = args.output_dir or (
        DEFAULT_OUTPUT_DIR / interval_name if interval_name else DEFAULT_OUTPUT_DIR
    )
    print(
        f"interval={interval_name or 'custom'} market={args.market.lower()} "
        f"blocks={args.from_block}-{args.to_block} "
        f"output_dir={output_dir}"
    )
    rpc = RpcClient(args.rpc_url, timeout=180)
    rpc_logs = fetch_rpc_logs_chunked(
        rpc,
        args.market,
        args.from_block,
        args.to_block,
        args.rpc_log_chunk_blocks,
    )
    hypersync_logs: list[dict[str, Any]] | None = None
    source_timestamps: dict[int, int] = {}
    event_source = "rpc_fallback"
    if interval_metadata is not None:
        hypersync_logs, source_timestamps = load_cached_envio_logs(
            args.interval_manifest,
            interval_metadata,
            rpc_logs,
        )
        event_source = "envio_hypersync_cache_verified_by_rpc"
    else:
        token = os.environ.get("ENVIO_API_TOKEN", "").strip()
        if token:
            hypersync_logs = HyperSyncClient(args.hypersync_url, token).logs(
                args.market, args.from_block, args.to_block
            )
            event_source = "envio_hypersync"
    source_logs: Sequence[dict[str, Any]] = (
        hypersync_logs if hypersync_logs is not None else rpc_logs
    )
    parity = event_parity(hypersync_logs, rpc_logs)
    if interval_metadata is not None and parity["status"] != "pass":
        raise ReconstructionError(
            f"Named interval requires passing Envio/RPC parity: {stable_json(parity)}"
        )
    events = decode_events(source_logs)
    summary, timeline, plot_data = build_report_data(
        rpc,
        args.market,
        args.from_block,
        args.to_block,
        events,
        rpc_logs,
        hypersync_logs,
        interval_name=interval_name,
        interval_metadata=interval_metadata,
        source_timestamps=source_timestamps,
        validation_point_count=args.validation_points,
        event_source=event_source,
        event_input={
            "path": "source_events.jsonl",
            "record_count": len(source_logs),
            "ordering": "block_number,transaction_index,log_index",
            "payload_source": event_source,
        },
    )
    paths = write_outputs(output_dir, summary, timeline, plot_data, source_logs)
    print(
        stable_json(
            {
                "summary": summary,
                "artifacts": {key: str(value) for key, value in paths.items()},
            },
            pretty=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (HyperSyncError, ReconstructionError, RpcError, OSError, ValueError) as exc:
        raise SystemExit(f"Error: {exc}") from exc
