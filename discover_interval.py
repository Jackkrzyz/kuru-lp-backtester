"""Find compact Kuru event intervals with selected event classes."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict

from rebuild_from_chain import EVENT_SPECS, MARKET_ADDRESS, RpcClient, hex_int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scan one block range and report Kuru market event counts."
    )
    parser.add_argument("--from-block", type=int, required=True)
    parser.add_argument("--to-block", type=int, required=True)
    parser.add_argument("--chunk-size", type=int, default=20_000)
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--rpc-url", default="https://rpc2.monad.xyz")
    parser.add_argument("--market", default=MARKET_ADDRESS)
    parser.add_argument(
        "--events",
        nargs="+",
        help="Only scan these event names. The default scans all known events.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.from_block < 0 or args.to_block < args.from_block:
        raise SystemExit("Error: invalid block range.")
    if args.chunk_size <= 0:
        raise SystemExit("Error: chunk size must be greater than zero.")

    rpc = RpcClient(args.rpc_url, timeout=90)
    selected = [
        spec for spec in EVENT_SPECS if not args.events or spec.name in set(args.events)
    ]
    if args.events:
        unknown = sorted(set(args.events) - {spec.name for spec in selected})
        if unknown:
            raise SystemExit(f"Error: unknown event names: {', '.join(unknown)}")
    topic_to_name = {spec.topic: spec.name for spec in selected}
    topics = list(topic_to_name)
    found: dict[str, list[int]] = defaultdict(list)
    total = Counter()

    ranges = [
        (first, min(first + args.chunk_size - 1, args.to_block))
        for first in range(args.from_block, args.to_block + 1, args.chunk_size)
    ]
    for offset in range(0, len(ranges), args.batch_size):
        batch_ranges = ranges[offset : offset + args.batch_size]
        calls = [
            (
                "eth_getLogs",
                [
                    {
                        "address": args.market,
                        "fromBlock": hex(first),
                        "toBlock": hex(last),
                        "topics": [topics],
                    }
                ],
            )
            for first, last in batch_ranges
        ]
        for (first, last), logs in zip(batch_ranges, rpc.batch(calls), strict=True):
            counts = Counter(
                topic_to_name.get(str(log["topics"][0]).lower(), "Unknown")
                for log in logs
            )
            total.update(counts)
            if logs:
                print(f"{first}-{last}: {len(logs)} events {dict(sorted(counts.items()))}")
            for log in logs:
                event_name = topic_to_name.get(
                    str(log["topics"][0]).lower(), "Unknown"
                )
                found[event_name].append(hex_int(log["blockNumber"]))

    print(f"TOTAL: {sum(total.values())} events {dict(sorted(total.items()))}")
    for name in (
        "FlipOrderCreated",
        "FlippedOrderCreated",
        "FlipOrderUpdated",
        "FlipOrdersCanceled",
        "VaultParamsUpdated",
    ):
        blocks = found[name]
        print(
            f"{name}: count={len(blocks)} "
            f"first={blocks[:10]} last={blocks[-3:] if blocks else []}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
