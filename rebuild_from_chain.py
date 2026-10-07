"""Rebuild the Kuru MON-USDC order book from its on-chain event history."""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Iterable, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pandas as pd
from eth_abi import decode as abi_decode
from eth_utils import keccak


MARKET_ADDRESS = "0x065c9d28e428a0db40191a54d33d5b7c71a9c394"
DEPLOYMENT_BLOCK = 43_660_999
DEFAULT_RPC_URL = "https://rpc2.monad.xyz"
DEFAULT_OUTPUT_DIR = Path("chain_data")
VAULT_PRICE_PRECISION = 10**18
BPS_MULTIPLIER = 10_000
DOUBLE_BPS_MULTIPLIER = 20_000


class RpcError(RuntimeError):
    """Report a JSON-RPC or transport error."""


class ReconstructionError(RuntimeError):
    """Report an event sequence that cannot produce an exact book."""


@dataclass(frozen=True)
class EventSpec:
    name: str
    signature: str
    types: tuple[str, ...]
    names: tuple[str, ...]

    @property
    def topic(self) -> str:
        return "0x" + keccak(text=self.signature).hex()


EVENT_SPECS = (
    EventSpec(
        "MarketStateUpdated",
        "MarketStateUpdated(uint8,uint8)",
        ("uint8", "uint8"),
        ("previous_state", "new_state"),
    ),
    EventSpec(
        "OrderCreated",
        "OrderCreated(uint40,address,uint96,uint32,bool)",
        ("uint40", "address", "uint96", "uint32", "bool"),
        ("order_id", "owner", "size", "price", "is_buy"),
    ),
    EventSpec(
        "FlipOrderCreated",
        "FlipOrderCreated(uint40,uint40,address,uint96,uint32,uint32,bool)",
        ("uint40", "uint40", "address", "uint96", "uint32", "uint32", "bool"),
        ("order_id", "flipped_id", "owner", "size", "price", "flipped_price", "is_buy"),
    ),
    EventSpec(
        "FlippedOrderCreated",
        "FlippedOrderCreated(uint40,uint40,address,uint96,uint32,uint32,bool)",
        ("uint40", "uint40", "address", "uint96", "uint32", "uint32", "bool"),
        ("order_id", "flipped_id", "owner", "size", "price", "flipped_price", "is_buy"),
    ),
    EventSpec(
        "FlipOrderUpdated",
        "FlipOrderUpdated(uint40,uint96)",
        ("uint40", "uint96"),
        ("order_id", "size"),
    ),
    EventSpec(
        "FlipOrdersCanceled",
        "FlipOrdersCanceled(uint40[],address)",
        ("uint40[]", "address"),
        ("order_ids", "owner"),
    ),
    EventSpec(
        "OrdersCanceled",
        "OrdersCanceled(uint40[],address)",
        ("uint40[]", "address"),
        ("order_ids", "owner"),
    ),
    EventSpec(
        "OrderCanceled",
        "OrderCanceled(uint40,address,uint32,uint96,bool)",
        ("uint40", "address", "uint32", "uint96", "bool"),
        ("order_id", "owner", "price", "size", "is_buy"),
    ),
    EventSpec(
        "VaultParamsUpdated",
        "VaultParamsUpdated(uint96,uint96,uint96,uint96,uint256,uint256)",
        ("uint96", "uint96", "uint96", "uint96", "uint256", "uint256"),
        (
            "vault_ask_order_size",
            "ask_partially_filled_size",
            "vault_bid_order_size",
            "bid_partially_filled_size",
            "ask_price",
            "bid_price",
        ),
    ),
    EventSpec(
        "Trade",
        "Trade(uint40,address,bool,uint256,uint96,address,address,uint96)",
        ("uint40", "address", "bool", "uint256", "uint96", "address", "address", "uint96"),
        (
            "order_id",
            "maker_address",
            "is_buy",
            "price",
            "updated_size",
            "taker_address",
            "tx_origin",
            "filled_size",
        ),
    ),
)
EVENTS_BY_TOPIC = {spec.topic: spec for spec in EVENT_SPECS}


def hex_int(value: str | int) -> int:
    """Convert a JSON-RPC quantity to an integer."""
    return int(value, 16) if isinstance(value, str) else int(value)


def json_value(value: Any) -> Any:
    """Convert ABI values to stable JSON values."""
    if isinstance(value, tuple):
        return [json_value(item) for item in value]
    if isinstance(value, bytes):
        return "0x" + value.hex()
    return value


def selector(signature: str) -> str:
    """Return a four-byte function selector."""
    return "0x" + keccak(text=signature)[:4].hex()


class RpcClient:
    """Small JSON-RPC client with bounded retries."""

    def __init__(self, url: str, timeout: int = 60, retries: int = 5) -> None:
        self.url = url
        self.timeout = timeout
        self.retries = retries
        self._request_id = 0

    def _post(self, payload: dict[str, Any] | list[dict[str, Any]]) -> Any:
        body = json.dumps(payload).encode("utf-8")
        request = Request(
            self.url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "kuru-market-making-backtester/0.2",
            },
        )
        last_error: Exception | None = None
        for attempt in range(self.retries):
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    return json.loads(response.read())
            except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt + 1 < self.retries:
                    time.sleep(min(2**attempt, 8))
        raise RpcError(f"RPC request failed after {self.retries} attempts: {last_error}")

    def call(self, method: str, params: Sequence[Any]) -> Any:
        self._request_id += 1
        response = self._post(
            {
                "jsonrpc": "2.0",
                "id": self._request_id,
                "method": method,
                "params": list(params),
            }
        )
        if not isinstance(response, dict):
            raise RpcError(f"RPC returned an invalid response for {method}.")
        if "error" in response:
            error = response["error"]
            message = error.get("message", str(error)) if isinstance(error, dict) else str(error)
            raise RpcError(f"{method}: {message}")
        if "result" not in response:
            raise RpcError(f"RPC returned no result for {method}.")
        return response["result"]

    def batch(self, calls: Sequence[tuple[str, Sequence[Any]]]) -> list[Any]:
        if not calls:
            return []
        requests: list[dict[str, Any]] = []
        request_ids: list[int] = []
        for method, params in calls:
            self._request_id += 1
            request_ids.append(self._request_id)
            requests.append(
                {
                    "jsonrpc": "2.0",
                    "id": self._request_id,
                    "method": method,
                    "params": list(params),
                }
            )
        response = self._post(requests)
        if not isinstance(response, list):
            raise RpcError("RPC returned an invalid batch response.")
        by_id = {item.get("id"): item for item in response if isinstance(item, dict)}
        results: list[Any] = []
        for request_id in request_ids:
            item = by_id.get(request_id)
            if item is None:
                raise RpcError(f"RPC batch did not return request {request_id}.")
            if "error" in item:
                error = item["error"]
                message = error.get("message", str(error)) if isinstance(error, dict) else str(error)
                raise RpcError(f"RPC batch request {request_id}: {message}")
            results.append(item.get("result"))
        return results

    def block_number(self) -> int:
        return hex_int(self.call("eth_blockNumber", []))

    def logs(self, address: str, first_block: int, last_block: int) -> list[dict[str, Any]]:
        result = self.call(
            "eth_getLogs",
            [
                {
                    "address": address,
                    "fromBlock": hex(first_block),
                    "toBlock": hex(last_block),
                }
            ],
        )
        if not isinstance(result, list):
            raise RpcError("eth_getLogs returned a value that is not a list.")
        return result

    def block_timestamps(self, block_numbers: Iterable[int]) -> dict[int, int]:
        unique = sorted(set(block_numbers))
        timestamps: dict[int, int] = {}
        for offset in range(0, len(unique), 10):
            batch_numbers = unique[offset : offset + 10]
            calls = [("eth_getBlockByNumber", [hex(number), False]) for number in batch_numbers]
            for number, result in zip(batch_numbers, self.batch(calls), strict=True):
                if not isinstance(result, dict) or "timestamp" not in result:
                    raise RpcError(f"No block header is available for block {number:,}.")
                timestamps[number] = hex_int(result["timestamp"])
        return timestamps

    def contract_call(self, address: str, signature: str, block: int) -> str:
        return self.call(
            "eth_call",
            [{"to": address, "data": selector(signature)}, hex(block)],
        )


def decode_log(log: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Decode one known Kuru event. Preserve unknown events without guessing."""
    topics = log.get("topics")
    if not isinstance(topics, list) or not topics:
        return "Unknown", {}
    spec = EVENTS_BY_TOPIC.get(str(topics[0]).lower())
    if spec is None:
        return "Unknown", {}
    if len(topics) != 1:
        raise ReconstructionError(
            f"{spec.name} at transaction {log.get('transactionHash')} has unexpected indexed fields."
        )
    data = log.get("data")
    if not isinstance(data, str) or not data.startswith("0x"):
        raise ReconstructionError(f"{spec.name} has malformed ABI data.")
    try:
        values = abi_decode(spec.types, bytes.fromhex(data[2:]))
    except Exception as exc:
        raise ReconstructionError(f"Could not decode {spec.name}: {exc}") from exc
    return spec.name, {
        name: json_value(value) for name, value in zip(spec.names, values, strict=True)
    }


def _metadata(connection: sqlite3.Connection, key: str) -> str | None:
    row = connection.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row[0])


def _set_metadata(connection: sqlite3.Connection, key: str, value: Any) -> None:
    connection.execute(
        "INSERT INTO metadata(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def open_database(path: Path) -> sqlite3.Connection:
    """Open the durable state database and create its schema."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS orders (
            order_id INTEGER PRIMARY KEY,
            owner TEXT NOT NULL,
            size TEXT NOT NULL,
            price INTEGER NOT NULL,
            is_buy INTEGER NOT NULL,
            flipped_id INTEGER NOT NULL,
            flipped_price INTEGER NOT NULL,
            created_block INTEGER NOT NULL,
            created_transaction_index INTEGER NOT NULL,
            created_log_index INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS price_levels (
            is_buy INTEGER NOT NULL,
            price INTEGER NOT NULL,
            total_size TEXT NOT NULL,
            PRIMARY KEY (is_buy, price)
        );
        CREATE TABLE IF NOT EXISTS vault_state (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            best_bid TEXT NOT NULL,
            bid_partially_filled_size TEXT NOT NULL,
            best_ask TEXT NOT NULL,
            ask_partially_filled_size TEXT NOT NULL,
            bid_order_size TEXT NOT NULL,
            ask_order_size TEXT NOT NULL,
            spread TEXT NOT NULL
        );
        """
    )
    connection.commit()
    return connection


def _level_total(connection: sqlite3.Connection, is_buy: bool, price: int) -> int:
    row = connection.execute(
        "SELECT total_size FROM price_levels WHERE is_buy = ? AND price = ?",
        (int(is_buy), price),
    ).fetchone()
    return 0 if row is None else int(row[0])


def _change_level(connection: sqlite3.Connection, is_buy: bool, price: int, delta: int) -> None:
    new_total = _level_total(connection, is_buy, price) + delta
    if new_total < 0:
        raise ReconstructionError(
            f"Price level {price} has a negative size after applying {delta}."
        )
    if new_total == 0:
        connection.execute(
            "DELETE FROM price_levels WHERE is_buy = ? AND price = ?",
            (int(is_buy), price),
        )
    else:
        connection.execute(
            "INSERT INTO price_levels(is_buy, price, total_size) VALUES(?, ?, ?) "
            "ON CONFLICT(is_buy, price) DO UPDATE SET total_size = excluded.total_size",
            (int(is_buy), price, str(new_total)),
        )


def add_order(
    connection: sqlite3.Connection,
    args: dict[str, Any],
    block: int,
    transaction_index: int,
    log_index: int,
) -> None:
    """Add one active L3 order and update its aggregate price level."""
    order_id = int(args["order_id"])
    if connection.execute("SELECT 1 FROM orders WHERE order_id = ?", (order_id,)).fetchone():
        raise ReconstructionError(f"Order {order_id} was created more than once.")
    size = int(args["size"])
    price = int(args["price"])
    is_buy = bool(args["is_buy"])
    connection.execute(
        "INSERT INTO orders VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            order_id,
            str(args["owner"]).lower(),
            str(size),
            price,
            int(is_buy),
            int(args.get("flipped_id", 0)),
            int(args.get("flipped_price", 0)),
            block,
            transaction_index,
            log_index,
        ),
    )
    _change_level(connection, is_buy, price, size)


def set_order_size(connection: sqlite3.Connection, order_id: int, new_size: int) -> None:
    """Set an order size from an authoritative event value."""
    row = connection.execute(
        "SELECT size, price, is_buy FROM orders WHERE order_id = ?", (order_id,)
    ).fetchone()
    if row is None:
        raise ReconstructionError(f"Event updates unknown order {order_id}.")
    old_size = int(row["size"])
    _change_level(connection, bool(row["is_buy"]), int(row["price"]), new_size - old_size)
    if new_size == 0:
        connection.execute("DELETE FROM orders WHERE order_id = ?", (order_id,))
    else:
        connection.execute(
            "UPDATE orders SET size = ? WHERE order_id = ?", (str(new_size), order_id)
        )


def cancel_order(connection: sqlite3.Connection, args: dict[str, Any]) -> None:
    """Remove an order after checking the redundant event fields."""
    order_id = int(args["order_id"])
    row = connection.execute(
        "SELECT owner, size, price, is_buy FROM orders WHERE order_id = ?", (order_id,)
    ).fetchone()
    if row is None:
        raise ReconstructionError(f"Cancellation refers to unknown order {order_id}.")
    expected = (
        str(args["owner"]).lower(),
        int(args["size"]),
        int(args["price"]),
        int(bool(args["is_buy"])),
    )
    actual = (str(row["owner"]), int(row["size"]), int(row["price"]), int(row["is_buy"]))
    if actual != expected:
        raise ReconstructionError(
            f"Cancellation fields do not match reconstructed order {order_id}: "
            f"expected {actual}, received {expected}."
        )
    set_order_size(connection, order_id, 0)


def mul_div_round(value: int, multiplier: int, divisor: int) -> int:
    """Match Kuru's integer rounding: (value * multiplier + divisor / 2) / divisor."""
    return (value * multiplier + divisor // 2) // divisor


def vault_state(connection: sqlite3.Connection) -> dict[str, int]:
    row = connection.execute("SELECT * FROM vault_state WHERE singleton = 1").fetchone()
    if row is None:
        raise ReconstructionError("The vault state is not initialized.")
    return {
        "best_bid": int(row["best_bid"]),
        "bid_partially_filled_size": int(row["bid_partially_filled_size"]),
        "best_ask": int(row["best_ask"]),
        "ask_partially_filled_size": int(row["ask_partially_filled_size"]),
        "bid_order_size": int(row["bid_order_size"]),
        "ask_order_size": int(row["ask_order_size"]),
        "spread": int(row["spread"]),
    }


def save_vault_state(connection: sqlite3.Connection, state: dict[str, int]) -> None:
    connection.execute(
        """
        INSERT INTO vault_state VALUES(1, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(singleton) DO UPDATE SET
            best_bid = excluded.best_bid,
            bid_partially_filled_size = excluded.bid_partially_filled_size,
            best_ask = excluded.best_ask,
            ask_partially_filled_size = excluded.ask_partially_filled_size,
            bid_order_size = excluded.bid_order_size,
            ask_order_size = excluded.ask_order_size,
            spread = excluded.spread
        """,
        (
            str(state["best_bid"]),
            str(state["bid_partially_filled_size"]),
            str(state["best_ask"]),
            str(state["ask_partially_filled_size"]),
            str(state["bid_order_size"]),
            str(state["ask_order_size"]),
            str(state["spread"]),
        ),
    )


def update_vault_from_params(connection: sqlite3.Connection, args: dict[str, Any]) -> None:
    current = vault_state(connection)
    current.update(
        {
            "ask_order_size": int(args["vault_ask_order_size"]),
            "ask_partially_filled_size": int(args["ask_partially_filled_size"]),
            "bid_order_size": int(args["vault_bid_order_size"]),
            "bid_partially_filled_size": int(args["bid_partially_filled_size"]),
            "best_ask": int(args["ask_price"]),
            "best_bid": int(args["bid_price"]),
        }
    )
    save_vault_state(connection, current)


def update_vault_from_trade(connection: sqlite3.Connection, args: dict[str, Any]) -> None:
    """Apply one order-id-zero AMM trade with the contract's integer formulas."""
    state = vault_state(connection)
    is_buy = bool(args["is_buy"])
    event_price = int(args["price"])
    updated_size = int(args["updated_size"])
    spread = state["spread"]

    expected_price = state["best_ask"] if is_buy else state["best_bid"]
    if event_price != expected_price:
        raise ReconstructionError(
            f"Vault trade price {event_price} does not match reconstructed price {expected_price}."
        )

    if is_buy:
        if updated_size > 0:
            if updated_size > state["ask_order_size"]:
                raise ReconstructionError("Vault ask trade leaves more than the full ask size.")
            state["ask_partially_filled_size"] = state["ask_order_size"] - updated_size
        else:
            state["ask_partially_filled_size"] = 0
            state["ask_order_size"] = (
                state["ask_order_size"] * DOUBLE_BPS_MULTIPLIER
            ) // (DOUBLE_BPS_MULTIPLIER + spread)
            state["best_ask"] = mul_div_round(
                state["best_ask"], BPS_MULTIPLIER + spread, BPS_MULTIPLIER
            )
            state["best_bid"] = mul_div_round(
                state["best_ask"], BPS_MULTIPLIER, BPS_MULTIPLIER + spread
            )
            state["bid_order_size"] = (
                state["ask_order_size"] * (DOUBLE_BPS_MULTIPLIER + spread)
            ) // DOUBLE_BPS_MULTIPLIER
            state["bid_partially_filled_size"] = (
                state["bid_partially_filled_size"] * BPS_MULTIPLIER
            ) // (BPS_MULTIPLIER + spread)
    else:
        if updated_size > 0:
            if updated_size > state["bid_order_size"]:
                raise ReconstructionError("Vault bid trade leaves more than the full bid size.")
            state["bid_partially_filled_size"] = state["bid_order_size"] - updated_size
        else:
            state["bid_partially_filled_size"] = 0
            state["bid_order_size"] = (
                state["bid_order_size"] * (DOUBLE_BPS_MULTIPLIER + spread)
            ) // DOUBLE_BPS_MULTIPLIER
            state["best_bid"] = mul_div_round(
                state["best_bid"], BPS_MULTIPLIER, BPS_MULTIPLIER + spread
            )
            state["best_ask"] = mul_div_round(
                state["best_bid"], BPS_MULTIPLIER + spread, BPS_MULTIPLIER
            )
            state["ask_order_size"] = (
                state["bid_order_size"] * DOUBLE_BPS_MULTIPLIER
            ) // (DOUBLE_BPS_MULTIPLIER + spread)
    save_vault_state(connection, state)


def apply_event(
    connection: sqlite3.Connection,
    event_name: str,
    args: dict[str, Any],
    block: int,
    transaction_index: int,
    log_index: int,
) -> None:
    """Apply one decoded event to the durable book state."""
    if event_name in {"OrderCreated", "FlipOrderCreated", "FlippedOrderCreated"}:
        add_order(connection, args, block, transaction_index, log_index)
    elif event_name == "FlipOrderUpdated":
        set_order_size(connection, int(args["order_id"]), int(args["size"]))
    elif event_name == "OrderCanceled":
        cancel_order(connection, args)
    elif event_name == "Trade":
        if int(args["order_id"]) == 0:
            update_vault_from_trade(connection, args)
        else:
            set_order_size(connection, int(args["order_id"]), int(args["updated_size"]))
    elif event_name == "VaultParamsUpdated":
        update_vault_from_params(connection, args)
    # Batch cancellation events repeat individual OrderCanceled events. Unknown,
    # proxy, and market-state events do not change the reconstructed book.


def best_explicit_price(connection: sqlite3.Connection, is_buy: bool) -> int | None:
    direction = "DESC" if is_buy else "ASC"
    row = connection.execute(
        f"SELECT price FROM price_levels WHERE is_buy = ? ORDER BY price {direction} LIMIT 1",
        (int(is_buy),),
    ).fetchone()
    return None if row is None else int(row[0])


def _valid_vault_price(value: int, size: int, side: str) -> int | None:
    if size == 0:
        return None
    if side == "bid" and value == 0:
        return None
    if side == "ask" and value == 2**256 - 1:
        return None
    return value if value > 0 else None


def make_snapshot(
    connection: sqlite3.Connection, block: int, timestamp: int, price_precision: int
) -> dict[str, Any]:
    """Build one top-of-book row after all events in a block."""
    explicit_bid_raw = best_explicit_price(connection, True)
    explicit_ask_raw = best_explicit_price(connection, False)
    vault = vault_state(connection)
    vault_bid_raw = _valid_vault_price(vault["best_bid"], vault["bid_order_size"], "bid")
    vault_ask_raw = _valid_vault_price(vault["best_ask"], vault["ask_order_size"], "ask")

    explicit_bid = None if explicit_bid_raw is None else explicit_bid_raw / price_precision
    explicit_ask = None if explicit_ask_raw is None else explicit_ask_raw / price_precision
    vault_bid = None if vault_bid_raw is None else vault_bid_raw / VAULT_PRICE_PRECISION
    vault_ask = None if vault_ask_raw is None else vault_ask_raw / VAULT_PRICE_PRECISION
    bids = [value for value in (explicit_bid, vault_bid) if value is not None]
    asks = [value for value in (explicit_ask, vault_ask) if value is not None]
    best_bid = max(bids) if bids else math.nan
    best_ask = min(asks) if asks else math.nan
    midpoint = (best_bid + best_ask) / 2 if not (math.isnan(best_bid) or math.isnan(best_ask)) else math.nan
    spread = best_ask - best_bid if not math.isnan(midpoint) else math.nan
    spread_percent = (spread / midpoint) * 100 if midpoint > 0 else math.nan
    active_orders = int(connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0])

    return {
        "block_number": block,
        "timestamp": datetime.fromtimestamp(timestamp, tz=timezone.utc),
        "explicit_best_bid_raw": None if explicit_bid_raw is None else str(explicit_bid_raw),
        "explicit_best_ask_raw": None if explicit_ask_raw is None else str(explicit_ask_raw),
        "vault_best_bid_raw": None if vault_bid_raw is None else str(vault_bid_raw),
        "vault_best_ask_raw": None if vault_ask_raw is None else str(vault_ask_raw),
        "explicit_best_bid": explicit_bid,
        "explicit_best_ask": explicit_ask,
        "vault_best_bid": vault_bid,
        "vault_best_ask": vault_ask,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "midpoint": midpoint,
        "spread": spread,
        "spread_percent": spread_percent,
        "active_orders": active_orders,
    }


def reconstructed_best_bid_ask(
    connection: sqlite3.Connection, price_precision: int
) -> tuple[int, int]:
    """Return best prices in the 1e18 precision used by bestBidAsk()."""
    explicit_bid = best_explicit_price(connection, True)
    explicit_ask = best_explicit_price(connection, False)
    explicit_bid_18 = (
        None
        if explicit_bid is None
        else explicit_bid * VAULT_PRICE_PRECISION // price_precision
    )
    explicit_ask_18 = (
        None
        if explicit_ask is None
        else explicit_ask * VAULT_PRICE_PRECISION // price_precision
    )
    vault = vault_state(connection)
    vault_bid = _valid_vault_price(
        vault["best_bid"], vault["bid_order_size"], "bid"
    )
    vault_ask = _valid_vault_price(
        vault["best_ask"], vault["ask_order_size"], "ask"
    )
    bids = [value for value in (explicit_bid_18, vault_bid) if value is not None]
    asks = [value for value in (explicit_ask_18, vault_ask) if value is not None]
    return (max(bids) if bids else 2**256 - 1, min(asks) if asks else 0)


def validate_against_contract(
    connection: sqlite3.Connection,
    rpc: RpcClient,
    market: str,
    block: int,
    price_precision: int,
) -> None:
    """Stop when reconstructed best prices differ from historical contract state."""
    data = rpc.contract_call(market, "bestBidAsk()", block)
    contract_prices = tuple(
        int(value)
        for value in abi_decode(("uint256", "uint256"), bytes.fromhex(data[2:]))
    )
    reconstructed = reconstructed_best_bid_ask(connection, price_precision)
    if reconstructed != contract_prices:
        raise ReconstructionError(
            f"Top-of-book validation failed at block {block:,}: reconstructed "
            f"{reconstructed}, contract {contract_prices}. The checkpoint was not advanced."
        )


def atomic_parquet(rows: list[dict[str, Any]], destination: Path) -> None:
    """Write a Parquet part atomically. Do nothing when there are no rows."""
    if not rows:
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(
            dir=destination.parent, prefix=destination.name + ".", suffix=".tmp", delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
        pd.DataFrame(rows).to_parquet(temporary_path, index=False)
        temporary_path.replace(destination)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def event_row(
    log: dict[str, Any], event_name: str, args: dict[str, Any], timestamp: int
) -> dict[str, Any]:
    return {
        "block_number": hex_int(log["blockNumber"]),
        "timestamp": datetime.fromtimestamp(timestamp, tz=timezone.utc),
        "block_hash": log.get("blockHash"),
        "transaction_index": hex_int(log["transactionIndex"]),
        "log_index": hex_int(log["logIndex"]),
        "transaction_hash": log.get("transactionHash"),
        "event_name": event_name,
        "args_json": json.dumps(args, sort_keys=True, separators=(",", ":")),
        "raw_topics_json": json.dumps(log.get("topics", []), separators=(",", ":")),
        "raw_data": log.get("data"),
    }


def market_parameters(rpc: RpcClient, market: str) -> dict[str, Any]:
    data = rpc.contract_call(market, "getMarketParams()", DEPLOYMENT_BLOCK)
    values = abi_decode(
        (
            "uint32",
            "uint96",
            "address",
            "uint256",
            "address",
            "uint256",
            "uint32",
            "uint96",
            "uint96",
            "uint256",
            "uint256",
        ),
        bytes.fromhex(data[2:]),
    )
    keys = (
        "price_precision",
        "size_precision",
        "base_asset",
        "base_decimals",
        "quote_asset",
        "quote_decimals",
        "tick_size",
        "min_size",
        "max_size",
        "taker_fee_bps",
        "maker_fee_bps",
    )
    return dict(zip(keys, (json_value(value) for value in values), strict=True))


def initial_vault_parameters(rpc: RpcClient, market: str) -> dict[str, int]:
    data = rpc.contract_call(market, "getVaultParams()", DEPLOYMENT_BLOCK)
    values = abi_decode(
        ("address", "uint256", "uint96", "uint256", "uint96", "uint96", "uint96", "uint96"),
        bytes.fromhex(data[2:]),
    )
    return {
        "best_bid": int(values[1]),
        "bid_partially_filled_size": int(values[2]),
        "best_ask": int(values[3]),
        "ask_partially_filled_size": int(values[4]),
        "bid_order_size": int(values[5]),
        "ask_order_size": int(values[6]),
        "spread": int(values[7]),
    }


def initialize_reconstruction(
    connection: sqlite3.Connection, rpc: RpcClient, market: str
) -> dict[str, Any]:
    stored_market = _metadata(connection, "market")
    if stored_market is not None:
        if stored_market != market:
            raise ReconstructionError(
                f"The database belongs to market {stored_market}, not {market}."
            )
        return {
            "price_precision": int(_metadata(connection, "price_precision") or 0),
            "size_precision": int(_metadata(connection, "size_precision") or 0),
        }

    params = market_parameters(rpc, market)
    vault = initial_vault_parameters(rpc, market)
    connection.execute("BEGIN IMMEDIATE")
    try:
        _set_metadata(connection, "market", market)
        _set_metadata(connection, "deployment_block", DEPLOYMENT_BLOCK)
        _set_metadata(connection, "next_block", DEPLOYMENT_BLOCK)
        for key, value in params.items():
            _set_metadata(connection, key, value)
        save_vault_state(connection, vault)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return params


def fetch_logs_adaptive(
    rpc: RpcClient, market: str, first_block: int, target_last: int, chunk_size: int
) -> tuple[list[dict[str, Any]], int, int]:
    """Fetch a range and reduce it when the provider rejects the result."""
    attempted_size = min(chunk_size, target_last - first_block + 1)
    while True:
        last_block = first_block + attempted_size - 1
        try:
            logs = rpc.logs(market, first_block, last_block)
            logs.sort(
                key=lambda item: (
                    hex_int(item["blockNumber"]),
                    hex_int(item["transactionIndex"]),
                    hex_int(item["logIndex"]),
                )
            )
            return logs, last_block, attempted_size
        except RpcError:
            if attempted_size <= 1:
                raise
            attempted_size = max(1, attempted_size // 2)


def process_chunk(
    connection: sqlite3.Connection,
    rpc: RpcClient,
    market: str,
    output_dir: Path,
    first_block: int,
    last_block: int,
    logs: list[dict[str, Any]],
    price_precision: int,
) -> tuple[int, int]:
    """Apply one fetched block range and save its event and snapshot parts."""
    if any(log.get("removed") for log in logs):
        raise ReconstructionError("RPC returned a removed log for a finalized historical range.")
    block_numbers = [hex_int(log["blockNumber"]) for log in logs]
    timestamps = rpc.block_timestamps(block_numbers)
    by_block: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for log in logs:
        by_block[hex_int(log["blockNumber"])].append(log)

    event_rows: list[dict[str, Any]] = []
    snapshot_rows: list[dict[str, Any]] = []
    connection.execute("BEGIN IMMEDIATE")
    try:
        for block in sorted(by_block):
            for log in by_block[block]:
                event_name, args = decode_log(log)
                transaction_index = hex_int(log["transactionIndex"])
                log_index = hex_int(log["logIndex"])
                event_rows.append(event_row(log, event_name, args, timestamps[block]))
                apply_event(
                    connection,
                    event_name,
                    args,
                    block,
                    transaction_index,
                    log_index,
                )
            snapshot_rows.append(make_snapshot(connection, block, timestamps[block], price_precision))

        validate_against_contract(
            connection, rpc, market, last_block, price_precision
        )

        events_path = output_dir / "events" / f"part-{first_block:09d}-{last_block:09d}.parquet"
        snapshots_path = output_dir / "snapshots" / f"part-{first_block:09d}-{last_block:09d}.parquet"
        atomic_parquet(event_rows, events_path)
        atomic_parquet(snapshot_rows, snapshots_path)
        _set_metadata(connection, "next_block", last_block + 1)
        _set_metadata(connection, "last_completed_at", datetime.now(timezone.utc).isoformat())
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return len(event_rows), len(snapshot_rows)


def validate_market(value: str) -> str:
    normalized = value.lower()
    if len(normalized) != 42 or not normalized.startswith("0x"):
        raise argparse.ArgumentTypeError("market must be a 20-byte hex address")
    try:
        int(normalized[2:], 16)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("market must be a 20-byte hex address") from exc
    return normalized


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return number


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rebuild Kuru L3 orders and top-of-book history from Monad events."
    )
    parser.add_argument("--market", type=validate_market, default=MARKET_ADDRESS)
    parser.add_argument("--rpc-url", default=os.environ.get("MONAD_RPC_URL", DEFAULT_RPC_URL))
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--to-block",
        default="latest",
        help="Last block to process. The default resolves 'latest' once at startup.",
    )
    parser.add_argument(
        "--max-blocks",
        type=positive_int,
        help="Process at most this many blocks in this run.",
    )
    parser.add_argument(
        "--chunk-size",
        type=positive_int,
        default=20_000,
        help="Initial eth_getLogs range. It is reduced when the RPC rejects a range.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    rpc = RpcClient(args.rpc_url)
    database_path = args.output_dir / "state.sqlite3"
    connection = open_database(database_path)
    try:
        params = initialize_reconstruction(connection, rpc, args.market)
        next_block = int(_metadata(connection, "next_block") or DEPLOYMENT_BLOCK)
        if args.to_block == "latest":
            target = rpc.block_number()
        else:
            try:
                target = int(args.to_block)
            except ValueError as exc:
                raise ReconstructionError("--to-block must be an integer or 'latest'.") from exc
        if args.max_blocks is not None:
            target = min(target, next_block + args.max_blocks - 1)
        if target < next_block:
            print(
                f"Nothing to do. The next block is {next_block:,}, and the target is {target:,}."
            )
            return 0

        print(f"Market: {args.market}")
        print(f"RPC: {args.rpc_url}")
        print(f"Resume block: {next_block:,}")
        print(f"Target block: {target:,}")
        print(f"State database: {database_path}")

        chunk_size = args.chunk_size
        total_events = 0
        total_snapshots = 0
        while next_block <= target:
            logs, last_block, used_size = fetch_logs_adaptive(
                rpc, args.market, next_block, target, chunk_size
            )
            events, snapshots = process_chunk(
                connection,
                rpc,
                args.market,
                args.output_dir,
                next_block,
                last_block,
                logs,
                int(params["price_precision"]),
            )
            total_events += events
            total_snapshots += snapshots
            print(
                f"Saved blocks {next_block:,}-{last_block:,}: "
                f"{events:,} events, {snapshots:,} event-block snapshots."
            )
            next_block = last_block + 1
            if used_size == chunk_size and chunk_size < args.chunk_size:
                chunk_size = min(args.chunk_size, chunk_size * 2)
            else:
                chunk_size = used_size

        active_orders = int(connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0])
        print(
            f"Complete through block {target:,}. Saved {total_events:,} events and "
            f"{total_snapshots:,} snapshots in this run. Active L3 orders: {active_orders:,}."
        )
        return 0
    except (RpcError, ReconstructionError, ValueError, sqlite3.Error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
