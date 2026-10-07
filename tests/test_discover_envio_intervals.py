"""Tests for resumable, rate-aware Envio interval discovery."""

import json
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from eth_abi import encode as abi_encode

from discover_envio_intervals import (
    BLOCK_FIELDS,
    CACHE_SCHEMA_VERSION,
    LOG_FIELDS,
    ORDER_TOPICS,
    TOPIC_TO_NAME,
    TRADE_TOPICS,
    DiscoveryCache,
    DiscoveryError,
    DiscoveryEvent,
    RateLimitedHyperSyncClient,
    build_interval_stats,
    cache_signature,
    event_breakdown,
    fetch_candidate_order_events,
    fetch_discovery_events,
    fallback_block_payload,
    page_events,
    parse_args,
    query_payload,
    select_intervals,
)
from rebuild_from_chain import EVENT_SPECS, MARKET_ADDRESS


OWNER = "0x0000000000000000000000000000000000000123"
TRADE_SPEC = next(spec for spec in EVENT_SPECS if spec.name == "Trade")
ORDER_TOPIC = next(topic for topic, name in TOPIC_TO_NAME.items() if name == "OrderCreated")


def trade_data(price: float = 1.0, size: int = 10) -> str:
    values = (
        1,
        OWNER,
        True,
        int(price * 10**18),
        0,
        OWNER,
        OWNER,
        size,
    )
    return "0x" + abi_encode(TRADE_SPEC.types, values).hex()


def log_row(
    block: int,
    log_index: int,
    *,
    topic0: str = TRADE_SPEC.topic,
    data: str | None = None,
    transaction_hash: str | None = None,
    address: str = MARKET_ADDRESS,
) -> dict[str, object]:
    return {
        "address": address,
        "block_number": block,
        "transaction_hash": transaction_hash or f"0x{block:062x}{log_index:02x}",
        "log_index": log_index,
        "topic0": topic0,
        "data": data if data is not None else trade_data(),
    }


def page(next_block: int, logs: list[dict[str, object]]) -> dict[str, object]:
    blocks = sorted({int(log["block_number"]) for log in logs})
    return {
        "next_block": next_block,
        "data": [
            {
                "blocks": [
                    {"number": block, "timestamp": 1_700_000_000 + block}
                    for block in blocks
                ],
                "logs": logs,
            }
        ],
    }


def signature(first: int, last: int) -> dict[str, object]:
    return cache_signature(MARKET_ADDRESS, first, last, "https://example.invalid")


class FakePageClient:
    def __init__(self, pages: list[dict[str, object]]) -> None:
        self.pages = list(pages)
        self.cursors: list[int] = []
        self.payloads: list[dict[str, object]] = []

    def query(self, payload, *, persist_cursor, on_rate_limit):  # type: ignore[no-untyped-def]
        self.cursors.append(int(payload["from_block"]))
        self.payloads.append(payload)
        return self.pages.pop(0)


def discovery_event(
    block: int,
    log_index: int,
    name: str,
    price: float | None = None,
    size: int = 10,
) -> DiscoveryEvent:
    return DiscoveryEvent(
        block_number=block,
        block_timestamp=1_700_000_000 + block,
        address=MARKET_ADDRESS,
        transaction_hash=f"0x{block:062x}{log_index:02x}",
        log_index=log_index,
        name=name,
        topic0=TRADE_SPEC.topic if name == "Trade" else ORDER_TOPIC,
        data=trade_data(price or 1.0) if name == "Trade" else "0x",
        trade_price=price,
        trade_size=size if name == "Trade" else None,
    )


class PaginationCacheTests(unittest.TestCase):
    def test_market_argument_defaults_to_verified_market(self) -> None:
        with patch(
            "sys.argv",
            [
                "discover_envio_intervals.py",
                "--from-block",
                "10",
                "--to-block",
                "29",
            ],
        ):
            args = parse_args()

        self.assertEqual(args.market, MARKET_ADDRESS)

    def test_one_forward_paginated_pass_uses_next_block(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = DiscoveryCache(Path(directory), signature(10, 29))
            client = FakePageClient(
                [page(20, [log_row(12, 0)]), page(30, [log_row(22, 0)])]
            )

            events = fetch_discovery_events(client, cache, MARKET_ADDRESS, 30)  # type: ignore[arg-type]

            self.assertEqual(client.cursors, [10, 20])
            self.assertEqual(len(events), 2)
            self.assertTrue(cache.state["complete"])
            self.assertEqual(cache.state["next_block"], 30)

    def test_duplicate_event_across_pages_is_not_counted_twice(self) -> None:
        duplicate = log_row(12, 0)
        with tempfile.TemporaryDirectory() as directory:
            cache = DiscoveryCache(Path(directory), signature(10, 29))
            client = FakePageClient(
                [
                    page(20, [duplicate]),
                    page(30, [duplicate, log_row(22, 0)]),
                ]
            )

            events = fetch_discovery_events(client, cache, MARKET_ADDRESS, 30)  # type: ignore[arg-type]

            self.assertEqual(len(events), 2)
            self.assertEqual(cache.state["event_count"], 2)
            self.assertEqual(len(cache.events_path.read_text().splitlines()), 2)

    def test_resume_starts_at_persisted_next_block(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            first_cache = DiscoveryCache(path, signature(10, 29))
            first_cache.append_events([discovery_event(12, 0, "Trade", 1.0)])
            first_cache.state["next_block"] = 20
            first_cache.save_state()

            resumed_cache = DiscoveryCache(path, signature(10, 29))
            client = FakePageClient([page(30, [log_row(22, 0)])])
            events = fetch_discovery_events(
                client, resumed_cache, MARKET_ADDRESS, 30  # type: ignore[arg-type]
            )

            self.assertEqual(client.cursors, [20])
            self.assertEqual(len(events), 2)
            self.assertEqual(resumed_cache.state["next_block"], 30)

    def test_query_explicitly_requests_log_and_block_fields(self) -> None:
        payload = query_payload(MARKET_ADDRESS, 10, 30)

        self.assertEqual(payload["from_block"], 10)
        self.assertEqual(payload["to_block"], 30)
        self.assertEqual(
            payload["field_selection"],
            {"log": LOG_FIELDS, "block": BLOCK_FIELDS},
        )
        self.assertEqual(payload["join_mode"], "Default")
        self.assertNotIn("include_all_blocks", payload)
        self.assertEqual(payload["logs"][0]["address"], [MARKET_ADDRESS])
        self.assertEqual(payload["logs"][0]["topics"], [TRADE_TOPICS])

    def test_every_log_filter_contains_market_and_topic_filters(self) -> None:
        payload = query_payload(MARKET_ADDRESS.upper(), 10, 30)

        for log_filter in payload["logs"]:
            self.assertEqual(log_filter["address"], [MARKET_ADDRESS])
            self.assertEqual(log_filter["topics"], [TRADE_TOPICS])

    def test_broad_cache_identity_is_trade_only(self) -> None:
        self.assertEqual(signature(10, 29)["topics"], TRADE_TOPICS)

    def test_address_survives_response_cache_serialization_round_trip(self) -> None:
        returned_address = MARKET_ADDRESS.upper()
        parsed = page_events(
            page(20, [log_row(12, 0, address=returned_address)]),
            MARKET_ADDRESS,
            TRADE_TOPICS,
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            cache = DiscoveryCache(path, signature(10, 19))
            cache.append_events(parsed)
            cache.save_state()
            serialized = json.loads(cache.events_path.read_text(encoding="utf-8"))
            loaded = DiscoveryCache(path, signature(10, 19)).load_events()

        self.assertEqual(parsed[0].address, returned_address)
        self.assertEqual(serialized["address"], returned_address)
        self.assertEqual(loaded[0].address, returned_address)

    def test_final_candidate_fetch_uses_only_dense_order_topics(self) -> None:
        row = build_interval_stats(
            [discovery_event(12, 0, "Trade", 1.0)],
            10,
            19,
            10,
            10,
        )[0]
        client = FakePageClient(
            [page(20, [log_row(12, 1, topic0=ORDER_TOPIC, data="0x")])]
        )

        with tempfile.TemporaryDirectory() as directory:
            selected, events, _ = fetch_candidate_order_events(
                client,  # type: ignore[arg-type]
                Path(directory),
                MARKET_ADDRESS,
                "https://example.invalid",
                [("relatively_active", row)],
            )

        payload = client.payloads[0]
        self.assertEqual(payload["from_block"], 10)
        self.assertEqual(payload["to_block"], 20)
        self.assertEqual(payload["logs"][0]["topics"], [ORDER_TOPICS])
        self.assertEqual(selected[0][1].order_event_count, 1)
        self.assertEqual(events[0].name, "OrderCreated")

    def test_foreign_contract_log_is_rejected(self) -> None:
        result = page(
            20,
            [
                log_row(
                    12,
                    0,
                    address="0x0000000000000000000000000000000000009999",
                )
            ],
        )

        with self.assertRaisesRegex(DiscoveryError, "foreign log address"):
            page_events(result, MARKET_ADDRESS)

    def test_unordered_blocks_are_joined_to_logs_by_block_number(self) -> None:
        result = {
            "next_block": 30,
            "data": [
                {
                    "blocks": [
                        {"number": 22, "timestamp": 1_700_000_022},
                        {"number": 12, "timestamp": 1_700_000_012},
                    ],
                    "logs": [log_row(12, 0), log_row(22, 0)],
                }
            ],
        }

        events = page_events(result)

        self.assertEqual(
            [(event.block_number, event.block_timestamp) for event in events],
            [(12, 1_700_000_012), (22, 1_700_000_022)],
        )

    def test_page_map_reads_later_batches_before_processing_logs(self) -> None:
        result = {
            "next_block": 30,
            "data": [
                {"blocks": [], "logs": [log_row(12, 0)]},
                {
                    "blocks": [{"number": 12, "timestamp": 1_700_000_012}],
                    "logs": [],
                },
            ],
        }

        events = page_events(result)

        self.assertEqual(events[0].block_timestamp, 1_700_000_012)

    def test_timestamp_parser_accepts_integer_decimal_and_hexadecimal(self) -> None:
        result = {
            "next_block": 40,
            "data": [
                {
                    "blocks": [
                        {"number": 12, "timestamp": 1_700_000_012},
                        {"number": "22", "timestamp": "1700000022"},
                        {"number": "0x20", "timestamp": "0x6553f120"},
                    ],
                    "logs": [log_row(12, 0), log_row(22, 0), log_row(32, 0)],
                }
            ],
        }

        events = page_events(result)

        self.assertEqual(
            [event.block_timestamp for event in events],
            [1_700_000_012, 1_700_000_022, int("6553f120", 16)],
        )

    def test_missing_related_block_uses_one_block_fallback(self) -> None:
        requested: list[int] = []

        events = page_events(
            {"next_block": 20, "data": [{"blocks": [], "logs": [log_row(12, 0)]}]},
            timestamp_lookup=lambda block: requested.append(block) or 1_700_000_012,
        )

        self.assertEqual(requested, [12])
        self.assertEqual(events[0].block_timestamp, 1_700_000_012)

    def test_missing_related_block_does_not_commit_page_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = DiscoveryCache(Path(directory), signature(10, 29))
            client = FakePageClient(
                [
                    {
                        "next_block": 20,
                        "data": [{"blocks": [], "logs": [log_row(12, 0)]}],
                    },
                    {"next_block": 13, "data": [{"blocks": [], "logs": []}]},
                ]
            )

            with self.assertRaisesRegex(DiscoveryError, "narrow fallback"):
                fetch_discovery_events(client, cache, MARKET_ADDRESS, 30)  # type: ignore[arg-type]

            self.assertEqual(client.cursors, [10, 12])
            self.assertEqual(cache.state["next_block"], 10)
            self.assertEqual(cache.state["page_count"], 0)
            self.assertEqual(cache.state["event_count"], 0)
            self.assertFalse(cache.state["complete"])

    def test_fallback_query_is_narrow_and_does_not_include_all_blocks(self) -> None:
        payload = fallback_block_payload(12)

        self.assertEqual(payload["from_block"], 12)
        self.assertEqual(payload["to_block"], 13)
        self.assertEqual(payload["blocks"], [{}])
        self.assertEqual(payload["field_selection"], {"block": BLOCK_FIELDS})
        self.assertNotIn("include_all_blocks", payload)

    def test_stale_globally_filtered_cache_is_invalidated_and_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            stale_signature = signature(10, 29)
            stale_signature["schema_version"] = "3.0"
            (path / "state.json").write_text(
                json.dumps(
                    {
                        "schema_version": "3.0",
                        "signature": stale_signature,
                        "next_block": 20,
                        "complete": False,
                        "event_count": 1,
                        "page_count": 1,
                        "rate_limit_count": 0,
                    }
                ),
                encoding="utf-8",
            )
            (path / "events.jsonl").write_text(
                json.dumps(
                    {
                        **discovery_event(12, 0, "Trade", 1.0).__dict__,
                        "block_timestamp": 0,
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            cache = DiscoveryCache(path, signature(10, 29))

            self.assertEqual(cache.state["schema_version"], CACHE_SCHEMA_VERSION)
            self.assertEqual(cache.state["next_block"], 10)
            self.assertEqual(cache.state["event_count"], 0)
            self.assertFalse(cache.events_path.exists())
            self.assertEqual(len(list(path.glob("events.jsonl.stale-v3.0-*"))), 1)
            self.assertEqual(len(list(path.glob("state.json.stale-v3.0-*"))), 1)

    def test_cache_cannot_be_reused_for_a_different_market(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            DiscoveryCache(path, signature(10, 29))
            other = cache_signature(
                "0x0000000000000000000000000000000000009999",
                10,
                29,
                "https://example.invalid",
            )

            with self.assertRaisesRegex(DiscoveryError, "cache settings"):
                DiscoveryCache(path, other)

    def test_cache_cannot_be_reused_for_a_different_topic_set(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            DiscoveryCache(path, signature(10, 29))
            other = signature(10, 29)
            other["topics"] = ORDER_TOPICS

            with self.assertRaisesRegex(DiscoveryError, "cache settings"):
                DiscoveryCache(path, other)


class RateLimitTests(unittest.TestCase):
    def http_429(self, **headers: str) -> HTTPError:
        message = Message()
        for key, value in headers.items():
            message[key.replace("_", "-")] = value
        return HTTPError("https://example.invalid", 429, "rate limited", message, None)

    def test_429_honors_retry_after_and_persists_before_wait(self) -> None:
        calls = 0
        sleeps: list[float] = []
        persisted: list[str] = []

        def post(payload):  # type: ignore[no-untyped-def]
            nonlocal calls
            calls += 1
            if calls == 1:
                raise self.http_429(Retry_After="3", X_RateLimit_Remaining="0")
            return ({"next_block": 2, "data": []}, {})

        client = RateLimitedHyperSyncClient(
            "https://example.invalid",
            "token",
            sleep=sleeps.append,
            http_post=post,
        )
        result = client.query(
            {"from_block": 1},
            persist_cursor=lambda: persisted.append("saved"),
            on_rate_limit=lambda: None,
        )

        self.assertEqual(result["next_block"], 2)
        self.assertEqual(sleeps, [3.0])
        self.assertEqual(persisted, ["saved"])

    def test_429_fallback_uses_bounded_exponential_backoff_with_jitter(self) -> None:
        calls = 0
        sleeps: list[float] = []

        def post(payload):  # type: ignore[no-untyped-def]
            nonlocal calls
            calls += 1
            if calls <= 2:
                raise self.http_429()
            return ({"next_block": 2, "data": []}, {})

        client = RateLimitedHyperSyncClient(
            "https://example.invalid",
            "token",
            max_retries=2,
            base_backoff=2,
            maximum_backoff=4,
            jitter_seconds=1,
            random_value=lambda: 0.5,
            sleep=sleeps.append,
            http_post=post,
        )
        client.query(
            {"from_block": 1},
            persist_cursor=lambda: None,
            on_rate_limit=lambda: None,
        )

        self.assertEqual(sleeps, [2.5, 4])

    def test_429_honors_rate_limit_reset_header(self) -> None:
        calls = 0
        sleeps: list[float] = []

        def post(payload):  # type: ignore[no-untyped-def]
            nonlocal calls
            calls += 1
            if calls == 1:
                raise self.http_429(X_RateLimit_Reset="105")
            return ({"next_block": 2, "data": []}, {})

        client = RateLimitedHyperSyncClient(
            "https://example.invalid",
            "token",
            now=lambda: 100,
            sleep=sleeps.append,
            http_post=post,
        )
        client.query(
            {"from_block": 1},
            persist_cursor=lambda: None,
            on_rate_limit=lambda: None,
        )

        self.assertEqual(sleeps, [5])

    def test_429_persists_cursor_before_final_exit(self) -> None:
        persisted: list[str] = []

        def post(payload):  # type: ignore[no-untyped-def]
            raise self.http_429()

        client = RateLimitedHyperSyncClient(
            "https://example.invalid", "token", max_retries=0, http_post=post
        )

        with self.assertRaisesRegex(DiscoveryError, "remained rate limited"):
            client.query(
                {"from_block": 15},
                persist_cursor=lambda: persisted.append("saved"),
                on_rate_limit=lambda: None,
            )
        self.assertEqual(persisted, ["saved"])


class SlidingWindowTests(unittest.TestCase):
    def test_overlapping_windows_are_computed_locally(self) -> None:
        events = [
            discovery_event(1, 0, "Trade", 1.0),
            discovery_event(6_000, 0, "Trade", 1.1),
            discovery_event(6_100, 0, "OrderCreated"),
            discovery_event(11_000, 0, "Trade", 1.2),
        ]

        rows = build_interval_stats(events, 1, 20_000, 10_000, 5_000)

        self.assertEqual([(row.from_block, row.to_block) for row in rows], [
            (1, 10_000),
            (5_001, 15_000),
            (10_001, 20_000),
        ])
        self.assertEqual([row.trade_count for row in rows], [2, 2, 1])
        self.assertEqual([row.executed_volume for row in rows], [20, 20, 10])
        self.assertEqual([row.order_event_count for row in rows], [1, 1, 0])
        self.assertAlmostEqual(rows[0].approximate_price_movement_percent, 10)

    def test_local_window_aggregation_deduplicates_input(self) -> None:
        trade = discovery_event(10, 0, "Trade", 1.0)

        rows = build_interval_stats([trade, trade], 1, 100, 100, 100)

        self.assertEqual(rows[0].trade_count, 1)

    def test_selects_quiet_active_and_directional_windows(self) -> None:
        events = []
        for log in range(3):
            events.append(discovery_event(1 + log, log, "Trade", 1 + log * 0.001))
        for log in range(9):
            events.append(discovery_event(11 + log, log, "Trade", 1 + log * 0.001))
        for log in range(5):
            events.append(discovery_event(21 + log, log, "Trade", 1 + log * 0.05))
        rows = build_interval_stats(events, 1, 30, 10, 10)

        selected = select_intervals(rows, minimum_trades=2)

        self.assertEqual(
            [label for label, _ in selected],
            ["relatively_quiet", "relatively_active", "directional"],
        )
        self.assertEqual([row.trade_count for _, row in selected], [3, 9, 5])

    def test_active_ranking_uses_executed_volume_after_trade_count(self) -> None:
        events = [
            discovery_event(1, 0, "Trade", 1.0, 10),
            discovery_event(2, 1, "Trade", 1.001, 10),
            discovery_event(3, 2, "Trade", 1.002, 10),
            discovery_event(11, 0, "Trade", 1.0, 100),
            discovery_event(12, 1, "Trade", 1.001, 100),
            discovery_event(13, 2, "Trade", 1.002, 100),
            discovery_event(21, 0, "Trade", 1.0, 20),
            discovery_event(22, 1, "Trade", 1.5, 20),
        ]
        rows = build_interval_stats(events, 1, 30, 10, 10)

        selected = select_intervals(rows, minimum_trades=2)

        self.assertEqual(selected[1][0], "relatively_active")
        self.assertEqual(selected[1][1].from_block, 11)
        self.assertEqual(selected[1][1].executed_volume, 300)

    def test_breakdown_reports_event_name_topic_and_count(self) -> None:
        rows = event_breakdown(
            [
                discovery_event(1, 0, "Trade", 1.0),
                discovery_event(2, 0, "Trade", 1.1),
                discovery_event(3, 0, "OrderCreated"),
            ]
        )

        self.assertEqual(
            rows,
            [
                {
                    "event_name": "OrderCreated",
                    "topic0": ORDER_TOPIC,
                    "count": 1,
                },
                {
                    "event_name": "Trade",
                    "topic0": TRADE_SPEC.topic,
                    "count": 2,
                },
            ],
        )


if __name__ == "__main__":
    unittest.main()
