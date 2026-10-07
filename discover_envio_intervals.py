#!/usr/bin/env python3
"""Fetch one bounded Envio event stream and find local activity windows."""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from bisect import bisect_left, bisect_right
from collections import Counter
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from l3_vertical_slice import DEFAULT_HYPERSYNC_URL
from rebuild_from_chain import EVENT_SPECS, MARKET_ADDRESS, ReconstructionError, decode_log


ORDER_EVENT_NAMES = {
    "OrderCreated",
    "FlipOrderCreated",
    "FlippedOrderCreated",
    "FlipOrderUpdated",
    "OrderCanceled",
    "OrdersCanceled",
    "FlipOrdersCanceled",
}
TRADE_TOPIC = next(spec.topic for spec in EVENT_SPECS if spec.name == "Trade")
TRADE_TOPICS = [TRADE_TOPIC]
ORDER_TOPICS = sorted(
    spec.topic for spec in EVENT_SPECS if spec.name in ORDER_EVENT_NAMES
)
DISCOVERY_EVENT_NAMES = ORDER_EVENT_NAMES | {"Trade"}
DISCOVERY_TOPICS = sorted(ORDER_TOPICS + TRADE_TOPICS)
TOPIC_TO_NAME = {
    spec.topic: spec.name for spec in EVENT_SPECS if spec.name in DISCOVERY_EVENT_NAMES
}
LOG_FIELDS = [
    "address",
    "block_number",
    "transaction_hash",
    "log_index",
    "topic0",
    "data",
]
BLOCK_FIELDS = ["number", "timestamp"]
RATE_LIMIT_HEADER_NAMES = {
    "retry-after",
    "ratelimit-limit",
    "ratelimit-remaining",
    "ratelimit-reset",
    "x-ratelimit-limit",
    "x-ratelimit-remaining",
    "x-ratelimit-reset",
    "x-ratelimit-reset-after",
}
CACHE_SCHEMA_VERSION = "4.0"


class DiscoveryError(RuntimeError):
    """Report a discovery, cache, or HyperSync response error."""


@dataclass(frozen=True)
class DiscoveryEvent:
    block_number: int
    block_timestamp: int
    address: str
    transaction_hash: str
    log_index: int
    name: str
    topic0: str
    data: str
    trade_price: float | None
    trade_size: int | None

    @property
    def identity(self) -> tuple[int, str, int]:
        return (self.block_number, self.transaction_hash, self.log_index)


@dataclass(frozen=True)
class IntervalStats:
    from_block: int
    to_block: int
    inclusive_block_count: int
    duration_seconds: int | None
    duration_basis: str
    first_event_timestamp: str | None
    last_event_timestamp: str | None
    trade_count: int
    executed_volume: int
    order_event_count: int
    first_trade_price: float | None
    last_trade_price: float | None
    approximate_price_movement_percent: float | None


def quantity_int(value: Any) -> int:
    if isinstance(value, str):
        return int(value, 16) if value.startswith("0x") else int(value)
    return int(value)


def timestamp_text(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def event_identity(payload: Mapping[str, Any]) -> tuple[int, str, int]:
    return (
        int(payload["block_number"]),
        str(payload["transaction_hash"]).lower(),
        int(payload["log_index"]),
    )


class DiscoveryCache:
    """Append-only event cache with an atomic pagination cursor."""

    def __init__(self, directory: Path, signature: dict[str, Any]) -> None:
        self.directory = directory
        self.events_path = directory / "events.jsonl"
        self.state_path = directory / "state.json"
        self.signature = signature
        self.directory.mkdir(parents=True, exist_ok=True)
        self.state = self._load_or_create_state()
        self.identities = {
            event.identity for event in self.load_events()
        }
        self.state["event_count"] = len(self.identities)
        self.save_state()

    def _load_or_create_state(self) -> dict[str, Any]:
        if self.state_path.exists():
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            if state.get("schema_version") != CACHE_SCHEMA_VERSION:
                self._invalidate_stale_cache(str(state.get("schema_version", "unknown")))
                return self._new_state()
            if state.get("signature") != self.signature:
                raise DiscoveryError(
                    "cache settings do not match this scan; select a different cache directory"
                )
            return state
        return self._new_state()

    def _new_state(self) -> dict[str, Any]:
        return {
            "schema_version": CACHE_SCHEMA_VERSION,
            "signature": self.signature,
            "next_block": self.signature["from_block"],
            "complete": False,
            "event_count": 0,
            "page_count": 0,
            "rate_limit_count": 0,
        }

    def _invalidate_stale_cache(self, old_version: str) -> None:
        suffix = f".stale-v{old_version}-{time.time_ns()}"
        moved: list[str] = []
        for path in (self.events_path, self.state_path):
            if not path.exists():
                continue
            destination = path.with_name(path.name + suffix)
            path.replace(destination)
            moved.append(destination.name)
        print(
            f"Invalidated incompatible discovery cache schema {old_version}; "
            f"preserved files: {', '.join(moved) or 'none'}",
            file=sys.stderr,
        )

    def save_state(self) -> None:
        temporary = self.state_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(self.state, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.state_path)

    def load_events(self) -> list[DiscoveryEvent]:
        if not self.events_path.exists():
            return []
        unique: dict[tuple[int, str, int], DiscoveryEvent] = {}
        for line_number, line in enumerate(
            self.events_path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
                event = DiscoveryEvent(**payload)
            except (json.JSONDecodeError, TypeError, ValueError) as error:
                raise DiscoveryError(
                    f"cache event line {line_number} is malformed: {error}"
                ) from error
            unique.setdefault(event.identity, event)
        return sorted(
            unique.values(),
            key=lambda event: (event.block_number, event.transaction_hash, event.log_index),
        )

    def append_events(self, events: Sequence[DiscoveryEvent]) -> int:
        new_events: list[DiscoveryEvent] = []
        page_identities: set[tuple[int, str, int]] = set()
        for event in events:
            if event.identity in self.identities or event.identity in page_identities:
                continue
            page_identities.add(event.identity)
            new_events.append(event)
        if new_events:
            with self.events_path.open("a", encoding="utf-8", newline="\n") as output:
                for event in new_events:
                    output.write(json.dumps(asdict(event), sort_keys=True) + "\n")
                output.flush()
                os.fsync(output.fileno())
            self.identities.update(page_identities)
        self.state["event_count"] = len(self.identities)
        return len(new_events)


def relevant_rate_limit_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in headers.items()
        if key.lower() in RATE_LIMIT_HEADER_NAMES
    }


def retry_delay_seconds(
    headers: Mapping[str, str],
    attempt: int,
    *,
    now: float,
    base_backoff: float,
    maximum_backoff: float,
    jitter_seconds: float,
    random_value: float,
) -> float:
    lowered = {key.lower(): value for key, value in headers.items()}
    retry_after = lowered.get("retry-after")
    if retry_after:
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            try:
                parsed = parsedate_to_datetime(retry_after)
                return max(0.0, parsed.timestamp() - now)
            except (TypeError, ValueError, OverflowError):
                pass
    reset_after = lowered.get("x-ratelimit-reset-after")
    if reset_after:
        try:
            return max(0.0, float(reset_after))
        except ValueError:
            pass
    for name in ("ratelimit-reset", "x-ratelimit-reset"):
        reset = lowered.get(name)
        if reset:
            try:
                value = float(reset)
                return max(0.0, value - now if value > now else value)
            except ValueError:
                continue
    fallback = base_backoff * (2**attempt) + jitter_seconds * random_value
    return min(maximum_backoff, fallback)


class RateLimitedHyperSyncClient:
    """Minimal HTTP client with bounded 429 retry behavior."""

    def __init__(
        self,
        url: str,
        api_token: str,
        *,
        timeout: int = 180,
        max_retries: int = 6,
        base_backoff: float = 2.0,
        maximum_backoff: float = 60.0,
        jitter_seconds: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.time,
        random_value: Callable[[], float] = random.random,
        http_post: Callable[[dict[str, Any]], tuple[dict[str, Any], dict[str, str]]] | None = None,
    ) -> None:
        if not api_token.strip():
            raise DiscoveryError("ENVIO_API_TOKEN is not set")
        self.url = url.rstrip("/")
        self.api_token = api_token
        self.timeout = timeout
        self.max_retries = max_retries
        self.base_backoff = base_backoff
        self.maximum_backoff = maximum_backoff
        self.jitter_seconds = jitter_seconds
        self.sleep = sleep
        self.now = now
        self.random_value = random_value
        self.http_post = http_post or self._http_post

    def _http_post(self, payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
        request = Request(
            f"{self.url}/query",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_token}",
                "Content-Type": "application/json",
                "User-Agent": "kuru-market-making-backtester/0.5",
            },
        )
        with urlopen(request, timeout=self.timeout) as response:
            result = json.loads(response.read())
            headers = {key: value for key, value in response.headers.items()}
        if not isinstance(result, dict):
            raise DiscoveryError("HyperSync returned a value that is not an object")
        return result, headers

    def query(
        self,
        payload: dict[str, Any],
        *,
        persist_cursor: Callable[[], None],
        on_rate_limit: Callable[[], None],
    ) -> dict[str, Any]:
        for attempt in range(self.max_retries + 1):
            try:
                result, _ = self.http_post(payload)
                return result
            except HTTPError as error:
                if error.code != 429:
                    raise DiscoveryError(f"HyperSync returned HTTP {error.code}") from error
                headers = (
                    {key: value for key, value in error.headers.items()}
                    if error.headers is not None
                    else {}
                )
                persist_cursor()
                on_rate_limit()
                rate_headers = relevant_rate_limit_headers(headers)
                formatted = ", ".join(
                    f"{key}={value}" for key, value in sorted(rate_headers.items())
                ) or "none"
                print(f"HyperSync HTTP 429; rate-limit headers: {formatted}", file=sys.stderr)
                if attempt >= self.max_retries:
                    raise DiscoveryError(
                        f"HyperSync remained rate limited after {self.max_retries} retries"
                    ) from error
                delay = retry_delay_seconds(
                    headers,
                    attempt,
                    now=self.now(),
                    base_backoff=self.base_backoff,
                    maximum_backoff=self.maximum_backoff,
                    jitter_seconds=self.jitter_seconds,
                    random_value=self.random_value(),
                )
                print(f"Retrying cursor {payload['from_block']} in {delay:.2f} seconds.", file=sys.stderr)
                self.sleep(delay)
            except (URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
                persist_cursor()
                raise DiscoveryError(f"HyperSync query failed: {error}") from error
        raise DiscoveryError("HyperSync retry loop ended unexpectedly")


def query_payload(
    market: str,
    cursor: int,
    stop_exclusive: int,
    topics: Sequence[str] = TRADE_TOPICS,
) -> dict[str, Any]:
    normalized_market = market.lower()
    return {
        "from_block": cursor,
        "to_block": stop_exclusive,
        "logs": [
            {
                "address": [normalized_market],
                "topics": [list(topics)],
            }
        ],
        "field_selection": {"log": LOG_FIELDS, "block": BLOCK_FIELDS},
        "join_mode": "Default",
    }


def cache_signature(
    market: str,
    from_block: int,
    to_block_inclusive: int,
    hypersync_url: str,
    topics: Sequence[str] = TRADE_TOPICS,
) -> dict[str, Any]:
    return {
        "schema_version": CACHE_SCHEMA_VERSION,
        "market": market.lower(),
        "from_block": from_block,
        "to_block_inclusive": to_block_inclusive,
        "stop_block_exclusive": to_block_inclusive + 1,
        "hypersync_url": hypersync_url.rstrip("/"),
        "topics": list(topics),
        "log_fields": LOG_FIELDS,
        "block_fields": BLOCK_FIELDS,
    }


def fallback_block_payload(block_number: int) -> dict[str, Any]:
    return {
        "from_block": block_number,
        "to_block": block_number + 1,
        "blocks": [{}],
        "field_selection": {"block": BLOCK_FIELDS},
        "join_mode": "JoinNothing",
    }


def page_timestamp_map(result: Mapping[str, Any]) -> dict[int, int]:
    timestamps: dict[int, int] = {}
    for batch in result.get("data", []):
        for block in batch.get("blocks", []):
            number_value = block.get("number", block.get("block_number"))
            if number_value is None or block.get("timestamp") is None:
                continue
            timestamps[quantity_int(number_value)] = quantity_int(block["timestamp"])
    return timestamps


def page_events(
    result: Mapping[str, Any],
    market: str = MARKET_ADDRESS,
    allowed_topics: Sequence[str] = DISCOVERY_TOPICS,
    timestamp_lookup: Callable[[int], int] | None = None,
) -> list[DiscoveryEvent]:
    output: list[DiscoveryEvent] = []
    normalized_market = market.lower()
    normalized_topics = {topic.lower() for topic in allowed_topics}
    timestamps = page_timestamp_map(result)
    for batch in result.get("data", []):
        for log in batch.get("logs", []):
            returned_address = str(log.get("address", ""))
            normalized_log_address = returned_address.lower()
            if normalized_log_address != normalized_market:
                raise DiscoveryError(
                    "HyperSync returned foreign log address "
                    f"{returned_address or '<missing>'}; expected {normalized_market}"
                )
            block_number = quantity_int(log["block_number"])
            block_timestamp = timestamps.get(block_number)
            if block_timestamp is None:
                print(
                    f"HyperSync response batch keys for missing block {block_number}: "
                    f"{sorted(batch.keys())}",
                    file=sys.stderr,
                )
                if timestamp_lookup is None:
                    raise DiscoveryError(
                        f"HyperSync returned matched log block {block_number} without related block metadata"
                    )
                block_timestamp = timestamp_lookup(block_number)
                timestamps[block_number] = block_timestamp
            topic0 = str(log.get("topic0", "")).lower()
            if topic0 not in normalized_topics:
                raise DiscoveryError(
                    f"HyperSync returned unexpected topic0 {topic0 or '<missing>'}"
                )
            name = TOPIC_TO_NAME.get(topic0)
            if name is None:
                continue
            transaction_hash = str(log["transaction_hash"]).lower()
            log_index = quantity_int(log["log_index"])
            data = str(log.get("data", "0x")).lower()
            trade_price: float | None = None
            trade_size: int | None = None
            if name == "Trade":
                _, decoded = decode_log(
                    {
                        "address": normalized_market,
                        "blockNumber": block_number,
                        "transactionHash": transaction_hash,
                        "transactionIndex": 0,
                        "logIndex": log_index,
                        "topics": [topic0],
                        "data": data,
                    }
                )
                trade_price = int(decoded["price"]) / 10**18
                trade_size = int(decoded["filled_size"])
            output.append(
                DiscoveryEvent(
                    block_number=block_number,
                    block_timestamp=block_timestamp,
                    address=returned_address,
                    transaction_hash=transaction_hash,
                    log_index=log_index,
                    name=name,
                    topic0=topic0,
                    data=data,
                    trade_price=trade_price,
                    trade_size=trade_size,
                )
            )
    return output


def lookup_block_timestamp(
    client: RateLimitedHyperSyncClient,
    cache: DiscoveryCache,
    block_number: int,
) -> int:
    result = client.query(
        fallback_block_payload(block_number),
        persist_cursor=cache.save_state,
        on_rate_limit=lambda: _record_rate_limit(cache),
    )
    timestamps = page_timestamp_map(result)
    if block_number not in timestamps:
        batch_keys = [sorted(batch.keys()) for batch in result.get("data", [])]
        print(
            f"Fallback response batch keys for missing block {block_number}: {batch_keys}",
            file=sys.stderr,
        )
        raise DiscoveryError(
            f"narrow fallback returned no timestamp for block {block_number}"
        )
    return timestamps[block_number]


def fetch_discovery_events(
    client: RateLimitedHyperSyncClient,
    cache: DiscoveryCache,
    market: str,
    stop_exclusive: int,
    topics: Sequence[str] = TRADE_TOPICS,
) -> list[DiscoveryEvent]:
    if cache.state.get("complete"):
        return cache.load_events()
    cursor = int(cache.state["next_block"])
    while cursor < stop_exclusive:
        cache.state["next_block"] = cursor
        cache.save_state()
        result = client.query(
            query_payload(market, cursor, stop_exclusive, topics),
            persist_cursor=cache.save_state,
            on_rate_limit=lambda: _record_rate_limit(cache),
        )
        next_block = quantity_int(result.get("next_block", stop_exclusive))
        if next_block <= cursor:
            cache.save_state()
            raise DiscoveryError(
                f"HyperSync next_block {next_block} did not advance past {cursor}"
            )
        fetched = page_events(
            result,
            market,
            topics,
            timestamp_lookup=lambda block_number: lookup_block_timestamp(
                client, cache, block_number
            ),
        )
        added = cache.append_events(fetched)
        cursor = min(next_block, stop_exclusive)
        cache.state["next_block"] = cursor
        cache.state["page_count"] = int(cache.state.get("page_count", 0)) + 1
        cache.state["complete"] = cursor >= stop_exclusive
        cache.save_state()
        print(
            f"cursor={cursor} page_events={len(fetched)} new_events={added} "
            f"cached_events={cache.state['event_count']}"
        )
    return cache.load_events()


def _record_rate_limit(cache: DiscoveryCache) -> None:
    cache.state["rate_limit_count"] = int(cache.state.get("rate_limit_count", 0)) + 1
    cache.save_state()


def event_breakdown(events: Sequence[DiscoveryEvent]) -> list[dict[str, Any]]:
    counts = Counter((event.name, event.topic0) for event in events)
    return [
        {"event_name": name, "topic0": topic0, "count": count}
        for (name, topic0), count in sorted(counts.items())
    ]


def print_event_breakdown(label: str, events: Sequence[DiscoveryEvent]) -> None:
    rows = event_breakdown(events)
    if not rows:
        print(f"{label} event breakdown: no matched events")
        return
    print(f"{label} event breakdown:")
    for row in rows:
        print(
            f"  event={row['event_name']} topic0={row['topic0']} "
            f"count={row['count']}"
        )


def build_interval_stats(
    events: Sequence[DiscoveryEvent],
    from_block: int,
    to_block: int,
    window_blocks: int,
    step_blocks: int,
) -> list[IntervalStats]:
    unique = {event.identity: event for event in events}
    ordered = sorted(
        unique.values(),
        key=lambda event: (event.block_number, event.transaction_hash, event.log_index),
    )
    block_numbers = [event.block_number for event in ordered]
    rows: list[IntervalStats] = []
    for first in range(from_block, to_block + 1, step_blocks):
        last = min(first + window_blocks - 1, to_block)
        if last - first + 1 < window_blocks:
            break
        left = bisect_left(block_numbers, first)
        right = bisect_right(block_numbers, last)
        interval_events = ordered[left:right]
        trades = [event for event in interval_events if event.name == "Trade"]
        prices = [event.trade_price for event in trades if event.trade_price is not None]
        executed_volume = sum(
            event.trade_size for event in trades if event.trade_size is not None
        )
        movement = None
        if prices and prices[0] != 0:
            movement = (prices[-1] - prices[0]) / prices[0] * 100
        first_timestamp = interval_events[0].block_timestamp if interval_events else None
        last_timestamp = interval_events[-1].block_timestamp if interval_events else None
        rows.append(
            IntervalStats(
                from_block=first,
                to_block=last,
                inclusive_block_count=last - first + 1,
                duration_seconds=(
                    None
                    if first_timestamp is None or last_timestamp is None
                    else last_timestamp - first_timestamp
                ),
                duration_basis="first_to_last_filtered_market_event",
                first_event_timestamp=(
                    None if first_timestamp is None else timestamp_text(first_timestamp)
                ),
                last_event_timestamp=(
                    None if last_timestamp is None else timestamp_text(last_timestamp)
                ),
                trade_count=len(trades),
                executed_volume=executed_volume,
                order_event_count=sum(
                    1 for event in interval_events if event.name in ORDER_EVENT_NAMES
                ),
                first_trade_price=prices[0] if prices else None,
                last_trade_price=prices[-1] if prices else None,
                approximate_price_movement_percent=movement,
            )
        )
    return rows


def intervals_overlap(first: IntervalStats, second: IntervalStats) -> bool:
    return first.from_block <= second.to_block and second.from_block <= first.to_block


def first_non_overlapping(
    rows: Sequence[IntervalStats], selected: Sequence[IntervalStats]
) -> IntervalStats | None:
    return next(
        (row for row in rows if all(not intervals_overlap(row, item) for item in selected)),
        None,
    )


def select_intervals(
    rows: Sequence[IntervalStats], minimum_trades: int
) -> list[tuple[str, IntervalStats]]:
    eligible = [
        row
        for row in rows
        if row.trade_count >= minimum_trades
        and row.approximate_price_movement_percent is not None
    ]
    if len(eligible) < 3:
        raise ValueError(
            f"only {len(eligible)} windows have at least {minimum_trades} Trade events"
        )
    active = sorted(
        eligible,
        key=lambda row: (
            -row.trade_count,
            -row.executed_volume,
            -abs(row.approximate_price_movement_percent or 0),
            row.from_block,
        ),
    )[0]
    directional = first_non_overlapping(
        sorted(
            eligible,
            key=lambda row: (
                -abs(row.approximate_price_movement_percent or 0),
                -row.executed_volume,
                -row.trade_count,
                row.from_block,
            ),
        ),
        [active],
    )
    if directional is None:
        raise ValueError("no directional window is separate from the active window")
    quiet = first_non_overlapping(
        sorted(
            eligible,
            key=lambda row: (
                row.trade_count,
                row.executed_volume,
                abs(row.approximate_price_movement_percent or 0),
                row.from_block,
            ),
        ),
        [active, directional],
    )
    if quiet is None:
        raise ValueError("no quiet window is separate from the other selected windows")
    return [("relatively_quiet", quiet), ("relatively_active", active), ("directional", directional)]


def fetch_candidate_order_events(
    client: RateLimitedHyperSyncClient,
    cache_root: Path,
    market: str,
    hypersync_url: str,
    selected: Sequence[tuple[str, IntervalStats]],
) -> tuple[
    list[tuple[str, IntervalStats]],
    list[DiscoveryEvent],
    list[dict[str, Any]],
]:
    enriched: list[tuple[str, IntervalStats]] = []
    all_order_events: list[DiscoveryEvent] = []
    cache_rows: list[dict[str, Any]] = []
    for label, row in selected:
        directory = (
            cache_root
            / "candidate_order_events"
            / f"{label}_{row.from_block}_{row.to_block}"
        )
        cache = DiscoveryCache(
            directory,
            cache_signature(
                market,
                row.from_block,
                row.to_block,
                hypersync_url,
                ORDER_TOPICS,
            ),
        )
        events = fetch_discovery_events(
            client,
            cache,
            market,
            row.to_block + 1,
            ORDER_TOPICS,
        )
        print_event_breakdown(f"candidate={label}", events)
        all_order_events.extend(events)
        enriched.append((label, replace(row, order_event_count=len(events))))
        cache_rows.append(
            {
                "classification": label,
                "directory": str(directory),
                "from_block": row.from_block,
                "to_block": row.to_block,
                "event_count": int(cache.state["event_count"]),
                "page_count": int(cache.state["page_count"]),
                "rate_limit_count": int(cache.state["rate_limit_count"]),
                "complete": bool(cache.state["complete"]),
            }
        )
    return enriched, all_order_events, cache_rows


def report_payload(
    selected: Sequence[tuple[str, IntervalStats]],
    rows: Sequence[IntervalStats],
    cache: DiscoveryCache,
    scan: dict[str, Any],
    events: Sequence[DiscoveryEvent],
    candidate_order_caches: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    intervals = []
    for label, row in selected:
        payload = asdict(row)
        payload["classification"] = label
        intervals.append(payload)
    return {
        "schema_version": "1.2",
        "event_source": "envio_hypersync",
        "market": scan["market"],
        "scan": scan,
        "cache": {
            "directory": str(cache.directory),
            "complete": bool(cache.state["complete"]),
            "next_block": int(cache.state["next_block"]),
            "event_count": int(cache.state["event_count"]),
            "page_count": int(cache.state["page_count"]),
            "rate_limit_count": int(cache.state["rate_limit_count"]),
        },
        "analysis_window_count": len(rows),
        "analysis_source": "local_resumable_cache",
        "event_breakdown": event_breakdown(events),
        "candidate_order_caches": list(candidate_order_caches),
        "strategy_parameters_changed": False,
        "selected_intervals": intervals,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-block", type=int, required=True)
    parser.add_argument("--to-block", type=int, required=True)
    parser.add_argument("--window-blocks", type=int, default=10_000)
    parser.add_argument("--step-blocks", type=int, default=5_000)
    parser.add_argument("--minimum-trades", type=int, default=20)
    parser.add_argument("--max-scan-blocks", type=int, default=2_000_000)
    parser.add_argument("--hypersync-url", default=DEFAULT_HYPERSYNC_URL)
    parser.add_argument("--market", default=MARKET_ADDRESS)
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--output", type=Path, default=Path("envio_interval_candidates.json"))
    parser.add_argument("--max-retries", type=int, default=6)
    parser.add_argument("--base-backoff-seconds", type=float, default=2.0)
    parser.add_argument("--maximum-backoff-seconds", type=float, default=60.0)
    parser.add_argument("--jitter-seconds", type=float, default=1.0)
    parser.add_argument("--smoke-test", action="store_true")
    parser.add_argument("--smoke-test-blocks", type=int, default=50_000)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    requested_blocks = args.to_block - args.from_block + 1
    if args.from_block < 0 or args.to_block < args.from_block:
        raise SystemExit("Error: a finite valid block range is required.")
    if args.window_blocks <= 0 or args.step_blocks <= 0:
        raise SystemExit("Error: window and step sizes must be positive.")
    if args.minimum_trades <= 0:
        raise SystemExit("Error: minimum Trade count must be positive.")
    if requested_blocks > args.max_scan_blocks:
        raise SystemExit(
            f"Error: scan has {requested_blocks} blocks; safety limit is {args.max_scan_blocks}."
        )
    effective_to_block = args.to_block
    if args.smoke_test:
        if args.smoke_test_blocks <= 0:
            raise SystemExit("Error: smoke-test block count must be positive.")
        effective_to_block = min(
            args.to_block, args.from_block + args.smoke_test_blocks - 1
        )
    stop_exclusive = effective_to_block + 1
    cache_directory = args.cache_dir or (
        Path(".cache")
        / "envio_interval_discovery"
        / f"{args.market.lower()}_{args.from_block}_{effective_to_block}"
    )
    signature = cache_signature(
        args.market,
        args.from_block,
        effective_to_block,
        args.hypersync_url,
    )
    try:
        print(f"market={args.market.lower()}")
        cache = DiscoveryCache(cache_directory, signature)
        token = os.environ.get("ENVIO_API_TOKEN", "").strip()
        client = RateLimitedHyperSyncClient(
            args.hypersync_url,
            token,
            max_retries=args.max_retries,
            base_backoff=args.base_backoff_seconds,
            maximum_backoff=args.maximum_backoff_seconds,
            jitter_seconds=args.jitter_seconds,
        )
        events = fetch_discovery_events(client, cache, args.market, stop_exclusive)
        print_event_breakdown("broad_trade_cache", events)
        rows = build_interval_stats(
            events,
            args.from_block,
            effective_to_block,
            args.window_blocks,
            args.step_blocks,
        )
        try:
            selected = select_intervals(rows, args.minimum_trades)
        except ValueError as error:
            if not args.smoke_test:
                raise
            print(f"Smoke test completed without three selectable windows: {error}")
            selected = []
        candidate_order_events: list[DiscoveryEvent] = []
        candidate_order_caches: list[dict[str, Any]] = []
        if selected:
            selected, candidate_order_events, candidate_order_caches = (
                fetch_candidate_order_events(
                    client,
                    cache_directory,
                    args.market,
                    args.hypersync_url,
                    selected,
                )
            )
        report = report_payload(
            selected,
            rows,
            cache,
            {
                "market": args.market.lower(),
                "requested_from_block": args.from_block,
                "requested_to_block": args.to_block,
                "effective_from_block": args.from_block,
                "effective_to_block": effective_to_block,
                "inclusive_block_count": effective_to_block - args.from_block + 1,
                "window_blocks": args.window_blocks,
                "step_blocks": args.step_blocks,
                "minimum_trades": args.minimum_trades,
                "broad_topics": TRADE_TOPICS,
                "candidate_order_topics": ORDER_TOPICS,
                "bounded": True,
                "smoke_test": args.smoke_test,
            },
            [*events, *candidate_order_events],
            candidate_order_caches,
        )
        args.output.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(json.dumps(report, indent=2, sort_keys=True))
    except (DiscoveryError, ReconstructionError, ValueError) as error:
        raise SystemExit(f"Error: {error}") from error
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
