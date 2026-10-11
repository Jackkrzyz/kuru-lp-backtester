"""Validate a compact Kuru L3 replay and run one passive-order simulation."""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from dataclasses import dataclass, replace
from typing import Any, Iterable, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode

from kuru_backtester.engine.rebuild_from_chain import (
    MARKET_ADDRESS,
    ReconstructionError,
    RpcClient,
    decode_log,
    hex_int,
    market_parameters,
    selector,
)


DEFAULT_HYPERSYNC_URL = "https://monad.hypersync.xyz"
DEFAULT_RPC_URL = "https://rpc2.monad.xyz"
DEFAULT_FROM_BLOCK = 44_853_800
DEFAULT_TO_BLOCK = 44_856_100


@dataclass(frozen=True)
class BookOrder:
    order_id: int
    owner: str
    size: int
    prev: int
    next: int
    flipped_id: int
    price: int
    flipped_price: int
    is_buy: bool


@dataclass
class PriceQueue:
    head: int = 0
    tail: int = 0
    total_size: int = 0


@dataclass(frozen=True)
class ReplayEvent:
    block_number: int
    transaction_index: int
    log_index: int
    transaction_hash: str
    name: str
    args: dict[str, Any]
    raw: dict[str, Any]


@dataclass(frozen=True)
class PassiveResult:
    placed_before_transaction: str
    side: str
    price_raw: int
    size_raw: int
    queue_ahead_orders: int
    queue_ahead_size_raw: int
    filled_size_raw: int
    downstream_size_raw: int
    final_inventory: float
    final_cash: float
    final_mark: float
    gross_pnl: float


class ReplayCursor:
    """Apply a sorted event stream once and keep its exact source position."""

    def __init__(self, events: Sequence[ReplayEvent]) -> None:
        self.events = list(events)
        self.index = 0
        self.position: tuple[int, int, int] | None = None

    def replay_through(
        self, book: "L3Book", block: int, price_precision: int
    ) -> int:
        applied = 0
        while self.index < len(self.events):
            event = self.events[self.index]
            if event.block_number > block:
                break
            apply_event(book, event, price_precision)
            self.position = (
                event.block_number,
                event.transaction_index,
                event.log_index,
            )
            self.index += 1
            applied += 1
        return applied

    @property
    def complete(self) -> bool:
        return self.index == len(self.events)


class HyperSyncError(RuntimeError):
    """Report a HyperSync access or response error."""


def canonical_address(value: str) -> str:
    return value.lower()


def encode_call(signature: str, types: Sequence[str] = (), values: Sequence[Any] = ()) -> str:
    return selector(signature) + abi_encode(types, values).hex()


def rpc_contract_call(
    rpc: RpcClient, market: str, data: str, block: int
) -> str:
    return rpc.call("eth_call", [{"to": market, "data": data}, hex(block)])


def rpc_contract_call_many(
    rpc: RpcClient,
    market: str,
    data_values: Sequence[str],
    block: int,
    batch_size: int = 10,
) -> list[str]:
    results: list[str] = []
    for offset in range(0, len(data_values), batch_size):
        calls = [
            ("eth_call", [{"to": market, "data": data}, hex(block)])
            for data in data_values[offset : offset + batch_size]
        ]
        results.extend(rpc.batch(calls))
    return results


def normalize_rpc_log(log: dict[str, Any]) -> dict[str, Any]:
    return {
        "address": canonical_address(str(log["address"])),
        "blockNumber": hex_int(log["blockNumber"]),
        "transactionHash": str(log["transactionHash"]).lower(),
        "transactionIndex": hex_int(log["transactionIndex"]),
        "logIndex": hex_int(log["logIndex"]),
        "data": str(log.get("data", "0x")).lower(),
        "topics": [str(topic).lower() for topic in log.get("topics", [])],
    }


def normalize_hypersync_log(log: dict[str, Any]) -> dict[str, Any]:
    topics = [
        str(log[key]).lower()
        for key in ("topic0", "topic1", "topic2", "topic3")
        if log.get(key) is not None
    ]
    return {
        "address": canonical_address(str(log["address"])),
        "blockNumber": hex_int(log["block_number"]),
        "transactionHash": str(log["transaction_hash"]).lower(),
        "transactionIndex": hex_int(log["transaction_index"]),
        "logIndex": hex_int(log["log_index"]),
        "data": str(log.get("data", "0x")).lower(),
        "topics": topics,
    }


def log_identity(log: dict[str, Any]) -> tuple[Any, ...]:
    normalized = (
        normalize_rpc_log(log)
        if "blockNumber" in log
        else normalize_hypersync_log(log)
    )
    return (
        normalized["blockNumber"],
        normalized["transactionIndex"],
        normalized["logIndex"],
        normalized["transactionHash"],
        normalized["address"],
        tuple(normalized["topics"]),
        normalized["data"],
    )


class HyperSyncClient:
    """Small Envio HyperSync HTTP client with explicit pagination."""

    def __init__(self, url: str, api_token: str, timeout: int = 180) -> None:
        if not api_token.strip():
            raise HyperSyncError(
                "ENVIO_API_TOKEN is not set. HyperSync event queries require a token."
            )
        self.url = url.rstrip("/")
        self.api_token = api_token
        self.timeout = timeout

    def _query(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = Request(
            f"{self.url}/query",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_token}",
                "Content-Type": "application/json",
                "User-Agent": "kuru-market-making-backtester/0.3",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                result = json.loads(response.read())
        except HTTPError as exc:
            raise HyperSyncError(f"HyperSync returned HTTP {exc.code}.") from exc
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise HyperSyncError(f"HyperSync query failed: {exc}") from exc
        if not isinstance(result, dict):
            raise HyperSyncError("HyperSync returned a value that is not an object.")
        return result

    def logs(self, market: str, first_block: int, last_block: int) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        cursor = first_block
        stop = last_block + 1
        while cursor < stop:
            result = self._query(
                {
                    "from_block": cursor,
                    "to_block": stop,
                    "logs": [{"address": [canonical_address(market)]}],
                    "field_selection": {
                        "log": [
                            "address",
                            "block_number",
                            "transaction_hash",
                            "transaction_index",
                            "log_index",
                            "data",
                            "topic0",
                            "topic1",
                            "topic2",
                            "topic3",
                        ]
                    },
                }
            )
            for batch in result.get("data", []):
                output.extend(batch.get("logs", []))
            next_block = hex_int(result.get("next_block", stop))
            if next_block <= cursor:
                raise HyperSyncError(
                    f"HyperSync pagination did not advance past block {cursor}."
                )
            cursor = next_block
        return output


class L3Book:
    """Exact explicit-order queues for one Kuru market."""

    def __init__(self) -> None:
        self.orders: dict[int, BookOrder] = {}
        self.levels: dict[tuple[bool, int], PriceQueue] = {}
        self._best_bid: int | None = None
        self._best_ask: int | None = None

    def copy(self) -> "L3Book":
        result = L3Book()
        result.orders = dict(self.orders)
        result.levels = {key: replace(level) for key, level in self.levels.items()}
        result._best_bid = self._best_bid
        result._best_ask = self._best_ask
        return result

    def best_prices(self) -> tuple[int | None, int | None]:
        """Return cached explicit best prices without rebuilding aggregate maps."""
        if self._best_bid is None:
            self._best_bid = max((price for is_buy, price in self.levels if is_buy), default=None)
        if self._best_ask is None:
            self._best_ask = min((price for is_buy, price in self.levels if not is_buy), default=None)
        return self._best_bid, self._best_ask

    def queue(self, is_buy: bool, price: int) -> list[int]:
        level = self.levels.get((is_buy, price))
        if level is None:
            return []
        output: list[int] = []
        current = level.head
        seen: set[int] = set()
        while current:
            if current in seen:
                raise ReconstructionError(f"Cycle found at order {current}.")
            seen.add(current)
            output.append(current)
            current = self.orders[current].next
        return output

    def append(
        self,
        order_id: int,
        owner: str,
        size: int,
        price: int,
        is_buy: bool,
        flipped_id: int = 0,
        flipped_price: int = 0,
        expected_prev: int | None = None,
        expected_next: int = 0,
    ) -> None:
        if order_id == 0 or order_id in self.orders:
            raise ReconstructionError(f"Order {order_id} cannot be appended twice.")
        if size <= 0:
            raise ReconstructionError(f"Order {order_id} has invalid size {size}.")
        key = (is_buy, price)
        level = self.levels.setdefault(key, PriceQueue())
        previous = level.tail
        if expected_prev is not None and previous != expected_prev:
            raise ReconstructionError(
                f"Order {order_id} prev mismatch: local {previous}, contract {expected_prev}."
            )
        if expected_next != 0:
            raise ReconstructionError(
                f"Order {order_id} is not a tail append; next is {expected_next}."
            )
        order = BookOrder(
            order_id=order_id,
            owner=owner.lower(),
            size=size,
            prev=previous,
            next=0,
            flipped_id=flipped_id,
            price=price,
            flipped_price=flipped_price,
            is_buy=is_buy,
        )
        if previous:
            self.orders[previous] = replace(self.orders[previous], next=order_id)
        else:
            level.head = order_id
        level.tail = order_id
        level.total_size += size
        self.orders[order_id] = order
        if is_buy and (self._best_bid is None or price > self._best_bid):
            self._best_bid = price
        if not is_buy and (self._best_ask is None or price < self._best_ask):
            self._best_ask = price

    def remove(self, order_id: int) -> BookOrder:
        order = self.orders.get(order_id)
        if order is None:
            raise ReconstructionError(f"Order {order_id} is not active.")
        key = (order.is_buy, order.price)
        level = self.levels[key]
        if order.prev:
            previous = self.orders[order.prev]
            self.orders[order.prev] = replace(previous, next=order.next)
        else:
            level.head = order.next
        if order.next:
            following = self.orders[order.next]
            self.orders[order.next] = replace(following, prev=order.prev)
        else:
            level.tail = order.prev
        level.total_size -= order.size
        del self.orders[order_id]
        if level.total_size == 0:
            if level.head != 0 or level.tail != 0:
                raise ReconstructionError(f"Empty level {key} retains queue links.")
            del self.levels[key]
            if order.is_buy and self._best_bid == order.price:
                self._best_bid = max((price for is_buy, price in self.levels if is_buy), default=None)
            if not order.is_buy and self._best_ask == order.price:
                self._best_ask = min((price for is_buy, price in self.levels if not is_buy), default=None)
        return order

    def set_size(self, order_id: int, new_size: int, require_head: bool = False) -> int:
        order = self.orders.get(order_id)
        if order is None:
            raise ReconstructionError(f"Order {order_id} is not active.")
        level = self.levels[(order.is_buy, order.price)]
        if require_head and level.head != order_id:
            raise ReconstructionError(
                f"Trade order {order_id} is not queue head {level.head}."
            )
        if new_size < 0 or new_size > order.size:
            raise ReconstructionError(
                f"Order {order_id} size changed from {order.size} to invalid {new_size}."
            )
        filled = order.size - new_size
        if new_size == 0:
            self.remove(order_id)
        else:
            level.total_size -= filled
            self.orders[order_id] = replace(order, size=new_size)
        return filled

    def add_size(self, order_id: int, new_size: int) -> None:
        order = self.orders.get(order_id)
        if order is None:
            raise ReconstructionError(f"Flip order {order_id} is not active.")
        if new_size < order.size:
            raise ReconstructionError(
                f"Flip order {order_id} size decreased from {order.size} to {new_size}."
            )
        self.levels[(order.is_buy, order.price)].total_size += new_size - order.size
        self.orders[order_id] = replace(order, size=new_size)

    def aggregate(self) -> tuple[dict[int, int], dict[int, int]]:
        bids = {
            price: level.total_size
            for (is_buy, price), level in self.levels.items()
            if is_buy
        }
        asks = {
            price: level.total_size
            for (is_buy, price), level in self.levels.items()
            if not is_buy
        }
        return bids, asks

    def check_invariants(self) -> None:
        reachable: set[int] = set()
        for (is_buy, price), level in self.levels.items():
            if not level.head or not level.tail or level.total_size <= 0:
                raise ReconstructionError(f"Level {(is_buy, price)} has invalid endpoints.")
            current = level.head
            previous = 0
            total = 0
            while current:
                if current in reachable:
                    raise ReconstructionError(f"Order {current} is linked more than once.")
                reachable.add(current)
                order = self.orders.get(current)
                if order is None:
                    raise ReconstructionError(f"Queue points to missing order {current}.")
                if (order.is_buy, order.price) != (is_buy, price):
                    raise ReconstructionError(f"Order {current} is in the wrong level.")
                if order.prev != previous:
                    raise ReconstructionError(
                        f"Order {current} prev is {order.prev}; expected {previous}."
                    )
                total += order.size
                previous = current
                current = order.next
            if previous != level.tail:
                raise ReconstructionError(
                    f"Level {(is_buy, price)} tail is {level.tail}; reached {previous}."
                )
            if self.orders[level.tail].next != 0:
                raise ReconstructionError(f"Level {(is_buy, price)} tail has a next link.")
            if total != level.total_size:
                raise ReconstructionError(
                    f"Level {(is_buy, price)} size is {level.total_size}; summed {total}."
                )
        if reachable != set(self.orders):
            missing = sorted(set(self.orders) - reachable)
            raise ReconstructionError(f"Unreachable active orders: {missing[:10]}.")


def get_l2_book(
    rpc: RpcClient, market: str, block: int
) -> tuple[int, dict[int, int], dict[int, int]]:
    data = rpc_contract_call(
        rpc,
        market,
        encode_call(
            "getL2Book(uint32,uint32)",
            ("uint32", "uint32"),
            (2**32 - 1, 2**32 - 1),
        ),
        block,
    )
    payload = abi_decode(("bytes",), bytes.fromhex(data[2:]))[0]
    if len(payload) % 32 or len(payload) < 64:
        raise ReconstructionError(f"getL2Book returned malformed data at block {block}.")
    words = [int.from_bytes(payload[offset : offset + 32], "big") for offset in range(0, len(payload), 32)]
    returned_block = words[0]
    bids: dict[int, int] = {}
    asks: dict[int, int] = {}
    index = 1
    while index < len(words) and words[index] != 0:
        if index + 1 >= len(words):
            raise ReconstructionError("getL2Book ended inside a bid pair.")
        bids[words[index]] = words[index + 1]
        index += 2
    if index >= len(words):
        raise ReconstructionError("getL2Book has no bid/ask separator.")
    index += 1
    while index < len(words):
        if index + 1 >= len(words):
            raise ReconstructionError("getL2Book ended inside an ask pair.")
        asks[words[index]] = words[index + 1]
        index += 2
    return returned_block, bids, asks


def get_contract_midpoint(rpc: RpcClient, market: str, block: int) -> float:
    data = rpc_contract_call(rpc, market, encode_call("bestBidAsk()"), block)
    best_bid, best_ask = (
        int(value)
        for value in abi_decode(("uint256", "uint256"), bytes.fromhex(data[2:]))
    )
    if best_bid == 2**256 - 1 or best_ask == 0:
        raise ReconstructionError(
            f"Cannot calculate a combined midpoint at block {block}."
        )
    return (best_bid + best_ask) / 2 / 10**18


def get_price_point(
    rpc: RpcClient, market: str, block: int, is_buy: bool, price: int
) -> tuple[int, int]:
    function = "s_buyPricePoints(uint256)" if is_buy else "s_sellPricePoints(uint256)"
    data = rpc_contract_call(
        rpc, market, encode_call(function, ("uint256",), (price,)), block
    )
    head, tail = abi_decode(("uint40", "uint40"), bytes.fromhex(data[2:]))
    return int(head), int(tail)


def get_order(rpc: RpcClient, market: str, block: int, order_id: int) -> BookOrder:
    data = rpc_contract_call(
        rpc,
        market,
        encode_call("s_orders(uint40)", ("uint40",), (order_id,)),
        block,
    )
    return decode_order_result(order_id, data)


def decode_order_result(order_id: int, data: str) -> BookOrder:
    values = abi_decode(
        ("address", "uint96", "uint40", "uint40", "uint40", "uint32", "uint32", "bool"),
        bytes.fromhex(data[2:]),
    )
    return BookOrder(
        order_id=order_id,
        owner=str(values[0]).lower(),
        size=int(values[1]),
        prev=int(values[2]),
        next=int(values[3]),
        flipped_id=int(values[4]),
        price=int(values[5]),
        flipped_price=int(values[6]),
        is_buy=bool(values[7]),
    )


def load_checkpoint(
    rpc: RpcClient, market: str, block: int
) -> tuple[L3Book, dict[int, int], dict[int, int]]:
    returned_block, bids, asks = get_l2_book(rpc, market, block)
    if returned_block != block:
        raise ReconstructionError(
            f"getL2Book returned block {returned_block}; requested {block}."
        )
    book = L3Book()
    level_rows = [
        (is_buy, price, expected_total)
        for is_buy, levels in ((True, bids), (False, asks))
        for price, expected_total in levels.items()
    ]
    point_calls = [
        encode_call(
            "s_buyPricePoints(uint256)" if is_buy else "s_sellPricePoints(uint256)",
            ("uint256",),
            (price,),
        )
        for is_buy, price, _ in level_rows
    ]
    point_results = rpc_contract_call_many(rpc, market, point_calls, block)
    states: dict[tuple[bool, int], dict[str, Any]] = {}
    for (is_buy, price, expected_total), data in zip(
        level_rows, point_results, strict=True
    ):
        head, tail = (
            int(value)
            for value in abi_decode(("uint40", "uint40"), bytes.fromhex(data[2:]))
        )
        if not head or not tail:
            raise ReconstructionError(
                f"Contract level {(is_buy, price)} has an empty head or tail."
            )
        states[(is_buy, price)] = {
            "expected_total": expected_total,
            "tail": tail,
            "current": head,
            "previous": 0,
            "seen": set(),
        }

    while True:
        active = [(key, state) for key, state in states.items() if state["current"]]
        if not active:
            break
        order_ids = [int(state["current"]) for _, state in active]
        order_calls = [
            encode_call("s_orders(uint40)", ("uint40",), (order_id,))
            for order_id in order_ids
        ]
        order_results = rpc_contract_call_many(rpc, market, order_calls, block)
        for ((is_buy, price), state), order_id, data in zip(
            active, order_ids, order_results, strict=True
        ):
            seen: set[int] = state["seen"]
            if order_id in seen:
                raise ReconstructionError(f"Contract queue cycle at order {order_id}.")
            seen.add(order_id)
            order = decode_order_result(order_id, data)
            if order.prev != state["previous"]:
                raise ReconstructionError(
                    f"Contract order {order_id} prev is {order.prev}; "
                    f"expected {state['previous']}."
                )
            if (order.is_buy, order.price) != (is_buy, price):
                raise ReconstructionError(f"Contract order {order_id} is in the wrong level.")
            book.append(
                order.order_id,
                order.owner,
                order.size,
                order.price,
                order.is_buy,
                order.flipped_id,
                order.flipped_price,
                expected_prev=order.prev,
                expected_next=0,
            )
            state["previous"] = order_id
            state["current"] = order.next

    for key, state in states.items():
        if state["previous"] != state["tail"]:
            raise ReconstructionError(
                f"Contract level {key} tail is {state['tail']}; "
                f"reached {state['previous']}."
            )
        local_total = book.levels[key].total_size
        if local_total != state["expected_total"]:
            raise ReconstructionError(
                f"Checkpoint level {key} is {local_total}; "
                f"L2 is {state['expected_total']}."
            )
    book.check_invariants()
    return book, bids, asks


def decode_event(log: dict[str, Any]) -> ReplayEvent:
    """Decode one RPC or HyperSync log without changing its chain position."""
    normalized = normalize_rpc_log(log) if "blockNumber" in log else normalize_hypersync_log(log)
    name, args = decode_log(normalized)
    return ReplayEvent(
        block_number=normalized["blockNumber"],
        transaction_index=normalized["transactionIndex"],
        log_index=normalized["logIndex"],
        transaction_hash=normalized["transactionHash"],
        name=name,
        args=args,
        raw=normalized,
    )


def decode_events(logs: Iterable[dict[str, Any]]) -> list[ReplayEvent]:
    output = [decode_event(log) for log in logs]
    return sorted(
        output,
        key=lambda event: (event.block_number, event.transaction_index, event.log_index),
    )


def apply_event(book: L3Book, event: ReplayEvent, price_precision: int) -> None:
    args = event.args
    if event.name in {"OrderCreated", "FlipOrderCreated", "FlippedOrderCreated"}:
        book.append(
            int(args["order_id"]),
            str(args["owner"]),
            int(args["size"]),
            int(args["price"]),
            bool(args["is_buy"]),
            int(args.get("flipped_id", 0)),
            int(args.get("flipped_price", 0)),
        )
    elif event.name == "FlipOrderUpdated":
        book.add_size(int(args["order_id"]), int(args["size"]))
    elif event.name == "OrderCanceled":
        order_id = int(args["order_id"])
        order = book.orders.get(order_id)
        if order is None:
            raise ReconstructionError(f"Cancel refers to inactive order {order_id}.")
        expected = (
            str(args["owner"]).lower(),
            int(args["size"]),
            int(args["price"]),
            bool(args["is_buy"]),
        )
        actual = (order.owner, order.size, order.price, order.is_buy)
        if actual != expected:
            raise ReconstructionError(
                f"Cancel fields for order {order_id} do not match local state."
            )
        book.remove(order_id)
    elif event.name == "Trade" and int(args["order_id"]) != 0:
        order_id = int(args["order_id"])
        order = book.orders.get(order_id)
        if order is None:
            raise ReconstructionError(f"Trade refers to inactive order {order_id}.")
        if order.is_buy == bool(args["is_buy"]):
            raise ReconstructionError(f"Trade side for order {order_id} is inconsistent.")
        expected_price = order.price * 10**18 // price_precision
        if int(args["price"]) != expected_price:
            raise ReconstructionError(
                f"Trade price for order {order_id} is {args['price']}; expected {expected_price}."
            )
        filled = book.set_size(order_id, int(args["updated_size"]), require_head=True)
        if filled != int(args["filled_size"]):
            raise ReconstructionError(
                f"Trade fill for order {order_id} is {args['filled_size']}; local delta is {filled}."
            )


def compare_l2(
    book: L3Book, rpc: RpcClient, market: str, block: int
) -> tuple[int, int]:
    returned_block, contract_bids, contract_asks = get_l2_book(rpc, market, block)
    local_bids, local_asks = book.aggregate()
    if returned_block != block or local_bids != contract_bids or local_asks != contract_asks:
        bid_diff = sorted(set(local_bids.items()) ^ set(contract_bids.items()))[:5]
        ask_diff = sorted(set(local_asks.items()) ^ set(contract_asks.items()))[:5]
        raise ReconstructionError(
            f"L2 validation failed at block {block}: bid_diff={bid_diff}, ask_diff={ask_diff}."
        )
    return len(contract_bids), len(contract_asks)


def count_events(events: Iterable[ReplayEvent]) -> dict[str, int]:
    return dict(sorted(Counter(event.name for event in events).items()))


def parity_report(
    hypersync_logs: Sequence[dict[str, Any]], rpc_logs: Sequence[dict[str, Any]]
) -> None:
    hypersync = Counter(log_identity(log) for log in hypersync_logs)
    rpc = Counter(log_identity(log) for log in rpc_logs)
    missing_from_hypersync = list((rpc - hypersync).elements())
    missing_from_rpc = list((hypersync - rpc).elements())
    if missing_from_hypersync or missing_from_rpc:
        raise ReconstructionError(
            "Envio/RPC event parity failed: "
            f"missing_from_envio={len(missing_from_hypersync)}, "
            f"missing_from_rpc={len(missing_from_rpc)}."
        )


def transaction_groups(events: Sequence[ReplayEvent]) -> list[list[ReplayEvent]]:
    groups: list[list[ReplayEvent]] = []
    current_key: tuple[int, int, str] | None = None
    for event in events:
        key = (event.block_number, event.transaction_index, event.transaction_hash)
        if key != current_key:
            groups.append([])
            current_key = key
        groups[-1].append(event)
    return groups


def choose_and_run_passive_order(
    initial_book: L3Book,
    events: Sequence[ReplayEvent],
    price_precision: int,
    size_precision: int,
    min_size: int,
    final_mark: float,
) -> PassiveResult:
    book = initial_book.copy()
    for group in transaction_groups(events):
        trades = [
            event
            for event in group
            if event.name == "Trade" and int(event.args["filled_size"]) > 0
        ]
        for candidate_index, candidate in enumerate(trades):
            if int(candidate.args["order_id"]) == 0:
                continue
            order = book.orders.get(int(candidate.args["order_id"]))
            if order is None:
                continue
            queue_ids = book.queue(order.is_buy, order.price)
            ahead_size = sum(book.orders[order_id].size for order_id in queue_ids)
            same_level_fill = 0
            downstream_size = 0
            cleared = False
            taker_is_buy = bool(candidate.args["is_buy"])
            for later in trades[candidate_index:]:
                if bool(later.args["is_buy"]) != taker_is_buy:
                    continue
                later_price = int(later.args["price"])
                candidate_price = order.price * 10**18 // price_precision
                if later_price == candidate_price and not cleared:
                    same_level_fill += int(later.args["filled_size"])
                    cleared = same_level_fill >= ahead_size
                    continue
                worse = later_price > candidate_price if taker_is_buy else later_price < candidate_price
                if cleared and worse:
                    downstream_size += int(later.args["filled_size"])
            if cleared and downstream_size >= min_size:
                simulated_size = min_size
                filled_size = min(simulated_size, downstream_size)
                side = "buy" if order.is_buy else "sell"
                fill_base = filled_size / size_precision
                fill_price = order.price / price_precision
                inventory = fill_base if order.is_buy else -fill_base
                cash = -fill_base * fill_price if order.is_buy else fill_base * fill_price

                gross_pnl = cash + inventory * final_mark
                return PassiveResult(
                    placed_before_transaction=group[0].transaction_hash,
                    side=side,
                    price_raw=order.price,
                    size_raw=simulated_size,
                    queue_ahead_orders=len(queue_ids),
                    queue_ahead_size_raw=ahead_size,
                    filled_size_raw=filled_size,
                    downstream_size_raw=downstream_size,
                    final_inventory=inventory,
                    final_cash=cash,
                    final_mark=final_mark,
                    gross_pnl=gross_pnl,
                )
        for event in group:
            apply_event(book, event, price_precision)
        book.check_invariants()
    raise ReconstructionError(
        "No transaction in this interval clears a queue and continues to a worse price "
        "with enough size for one minimum-size passive order."
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the Envio-first Kuru L3 hackathon vertical slice."
    )
    parser.add_argument("--from-block", type=int, default=DEFAULT_FROM_BLOCK)
    parser.add_argument("--to-block", type=int, default=DEFAULT_TO_BLOCK)
    parser.add_argument("--market", default=MARKET_ADDRESS)
    parser.add_argument("--hypersync-url", default=DEFAULT_HYPERSYNC_URL)
    parser.add_argument("--rpc-url", default=DEFAULT_RPC_URL)
    parser.add_argument(
        "--rpc-only",
        action="store_true",
        help="Use RPC events only. This skips, and does not prove, Envio parity.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.to_block < args.from_block:
        raise SystemExit("Error: invalid block range.")

    rpc = RpcClient(args.rpc_url, timeout=180)
    if args.rpc_only:
        rpc_logs = rpc.logs(args.market, args.from_block, args.to_block)
        source_logs: Sequence[dict[str, Any]] = rpc_logs
        print("Envio parity: SKIPPED (--rpc-only); this is not an Envio validation.")
    else:
        token = os.environ.get("ENVIO_API_TOKEN", "")
        hypersync = HyperSyncClient(args.hypersync_url, token)
        hypersync_logs = hypersync.logs(args.market, args.from_block, args.to_block)
        rpc_logs = rpc.logs(args.market, args.from_block, args.to_block)
        parity_report(hypersync_logs, rpc_logs)
        source_logs = hypersync_logs
        print(
            f"Envio/RPC parity: PASS ({len(hypersync_logs)} events in each source)."
        )

    events = decode_events(source_logs)
    params = market_parameters(rpc, args.market)
    price_precision = int(params["price_precision"])
    size_precision = int(params["size_precision"])
    min_size = int(params["min_size"])
    print(f"Range: {args.from_block}-{args.to_block}")
    print(f"Event counts: {count_events(events)}")

    checkpoint_block = args.from_block - 1
    book, bids, asks = load_checkpoint(rpc, args.market, checkpoint_block)
    print(
        f"Checkpoint {checkpoint_block}: {len(book.orders)} orders, "
        f"{len(bids)} bid levels, {len(asks)} ask levels; invariants PASS."
    )

    validation_blocks = {
        args.from_block,
        args.to_block,
        *(
            event.block_number
            for event in events
            if event.name in {"Trade", "VaultParamsUpdated", "FlippedOrderCreated"}
        ),
    }
    validations: list[tuple[int, int, int]] = []
    cursor = ReplayCursor(events)
    for block in sorted(validation_blocks):
        cursor.replay_through(book, block, price_precision)
        book.check_invariants()
        bid_levels, ask_levels = compare_l2(book, rpc, args.market, block)
        validations.append((block, bid_levels, ask_levels))
    if not cursor.complete:
        raise ReconstructionError(
            f"Replay cursor stopped at event {cursor.index} of {len(events)}."
        )
    print(f"L2 validations: PASS {validations}")
    print(f"Replay cursor: PASS index={cursor.index} position={cursor.position}")

    initial_book, _, _ = load_checkpoint(rpc, args.market, checkpoint_block)
    result = choose_and_run_passive_order(
        initial_book,
        events,
        price_precision,
        size_precision,
        min_size,
        get_contract_midpoint(rpc, args.market, args.to_block),
    )
    print(
        "Passive order: "
        f"side={result.side} price_raw={result.price_raw} size_raw={result.size_raw} "
        f"ahead_orders={result.queue_ahead_orders} "
        f"ahead_size_raw={result.queue_ahead_size_raw} "
        f"filled_size_raw={result.filled_size_raw} "
        f"tx={result.placed_before_transaction}"
    )
    print(
        "P&L (gross, before fees and gas): "
        f"cash={result.final_cash:.12f} inventory={result.final_inventory:.12f} "
        f"mark={result.final_mark:.12f} pnl={result.gross_pnl:.12f}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (HyperSyncError, ReconstructionError) as exc:
        raise SystemExit(f"Error: {exc}") from exc
