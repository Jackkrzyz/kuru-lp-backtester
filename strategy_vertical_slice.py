#!/usr/bin/env python3
"""Small, deterministic, queue-aware market-making strategy slice.

The verified reconstruction in l3_vertical_slice.py is an input to this module.
This module does not change reconstruction behavior.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
import sys
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

import l3_vertical_slice as l3
from rebuild_from_chain import MARKET_ADDRESS, ReconstructionError, RpcClient, market_parameters


BPS = Decimal(10_000)
PERCENT = Decimal(100)
ZERO = Decimal(0)
DECODED_EVENT_CACHE_SCHEMA_VERSION = "1"


@dataclass(frozen=True)
class StrategyConfig:
    starting_cash: Decimal
    starting_inventory: Decimal
    bid_offset_bps: Decimal
    ask_offset_bps: Decimal
    order_size: Decimal
    minimum_inventory: Decimal
    maximum_inventory: Decimal
    cancel_replace: bool
    replace_threshold_bps: Decimal
    maker_fee_bps: Decimal
    fixed_action_fee: Decimal
    inclusion_delay_blocks: int = 1
    material_impact_threshold_percent: Decimal = Decimal("25")
    max_transaction_participation_percent: Optional[Decimal] = None


@dataclass
class TransactionFillBudget:
    """A replay-time fill budget for one atomic historical transaction."""

    transaction_hash: str
    total_historical_raw: int
    maximum_simulated_raw: int
    remaining_raw: int


@dataclass
class StrategyOrder:
    strategy_order_id: str
    side: str
    price_raw: int
    size_raw: int
    remaining_raw: int
    status: str
    eligible_block: int
    decision_book_block: int
    queue_ahead_ids: list[int]
    queue_ahead_raw: int
    placed_block: int
    placed_transaction_index: int
    reserved_cash: Decimal = ZERO
    reserved_inventory: Decimal = ZERO


@dataclass
class StrategyState:
    cash: Decimal
    inventory: Decimal
    active_orders: dict[str, StrategyOrder] = field(default_factory=dict)
    next_order_number: int = 1
    placements: int = 0
    cancellations: int = 0
    fill_records: int = 0
    filled_size: Decimal = ZERO
    turnover: Decimal = ZERO
    maker_fees: Decimal = ZERO
    action_fees: Decimal = ZERO
    reserved_cash: Decimal = ZERO
    reserved_inventory: Decimal = ZERO


def decimal_argument(value: str) -> Decimal:
    try:
        return Decimal(value)
    except InvalidOperation as error:
        raise argparse.ArgumentTypeError(f"invalid decimal value: {value}") from error


def decimal_to_number(value: Decimal) -> int | float:
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def decimal_text(value: Decimal) -> str:
    return format(value, "f")


def to_raw_size(size: Decimal, size_precision: int) -> int:
    scaled = (size * Decimal(size_precision)).to_integral_value(rounding=ROUND_FLOOR)
    return int(scaled)


def quote_raw(midpoint_raw: int, offset_bps: Decimal, side: str, tick_size: int = 1) -> int:
    multiplier = BPS - offset_bps if side == "buy" else BPS + offset_bps
    unrounded = Decimal(midpoint_raw) * multiplier / BPS
    if side == "buy":
        ticks = int((unrounded / tick_size).to_integral_value(rounding=ROUND_FLOOR))
    else:
        floor_ticks = int((unrounded / tick_size).to_integral_value(rounding=ROUND_FLOOR))
        ticks = floor_ticks if unrounded == floor_ticks * tick_size else floor_ticks + 1
    return max(tick_size, ticks * tick_size)


def best_prices_raw(book: l3.L3Book) -> tuple[Optional[int], Optional[int]]:
    return book.best_prices()


def midpoint_raw(book: l3.L3Book) -> Optional[int]:
    best_bid, best_ask = best_prices_raw(book)
    if best_bid is None or best_ask is None:
        return None
    return (best_bid + best_ask) // 2


def target_quote_prices(
    book: l3.L3Book, config: StrategyConfig, tick_size: int = 1
) -> dict[str, int]:
    midpoint = midpoint_raw(book)
    if midpoint is None:
        return {}
    return {
        "buy": quote_raw(midpoint, config.bid_offset_bps, "buy", tick_size),
        "sell": quote_raw(midpoint, config.ask_offset_bps, "sell", tick_size),
    }


def positive_maker_fee_rate(config: StrategyConfig) -> Decimal:
    return max(config.maker_fee_bps, ZERO) / BPS


def buy_reservation(size: Decimal, price: Decimal, config: StrategyConfig) -> Decimal:
    notional = size * price
    return notional + notional * positive_maker_fee_rate(config)


def available_cash(state: StrategyState) -> Decimal:
    return state.cash - state.reserved_cash


def available_inventory(state: StrategyState) -> Decimal:
    return state.inventory - state.reserved_inventory


def committed_buy_inventory(state: StrategyState, size_precision: int) -> Decimal:
    return sum(
        (Decimal(order.remaining_raw) / Decimal(size_precision) for order in state.active_orders.values() if order.side == "buy"),
        ZERO,
    )


def assert_capital_invariants(state: StrategyState, config: StrategyConfig, size_precision: int) -> None:
    expected_cash = sum((order.reserved_cash for order in state.active_orders.values()), ZERO)
    expected_inventory = sum((order.reserved_inventory for order in state.active_orders.values()), ZERO)
    if state.reserved_cash != expected_cash:
        raise RuntimeError("reserved cash does not equal the active-order reservations")
    if state.reserved_inventory != expected_inventory:
        raise RuntimeError("reserved inventory does not equal the active-order reservations")
    if state.reserved_cash < ZERO or state.reserved_inventory < ZERO:
        raise RuntimeError("a reservation is negative")
    if state.cash < state.reserved_cash:
        raise RuntimeError("active bid reservations exceed cash")
    if state.inventory < config.minimum_inventory or state.inventory > config.maximum_inventory:
        raise RuntimeError("inventory violates a configured limit")
    if state.inventory - state.reserved_inventory < config.minimum_inventory:
        raise RuntimeError("active asks can reduce inventory below its minimum")
    if state.inventory + committed_buy_inventory(state, size_precision) > config.maximum_inventory:
        raise RuntimeError("active bids can increase inventory above its maximum")


def should_replace(existing: StrategyOrder, target_price_raw: int, threshold_bps: Decimal) -> bool:
    if existing.price_raw == target_price_raw:
        return False
    difference_bps = abs(Decimal(target_price_raw - existing.price_raw)) * BPS / Decimal(existing.price_raw)
    return difference_bps >= threshold_bps


def maker_fill_accounting(
    side: str,
    size: Decimal,
    price: Decimal,
    maker_fee_bps: Decimal,
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """Return cash delta, inventory delta, signed quote fee, and notional.

    A positive fee is a quote-denominated cost. A negative fee is a rebate.
    """
    notional = size * price
    fee = notional * maker_fee_bps / BPS
    if side == "buy":
        return -(notional + fee), size, fee, notional
    return notional - fee, -size, fee, notional


def active_order_payload(order: StrategyOrder, size_precision: int, price_precision: int) -> dict[str, Any]:
    return {
        "order_id": order.strategy_order_id,
        "side": order.side,
        "status": order.status,
        "price_raw": order.price_raw,
        "price": order.price_raw / price_precision,
        "size_raw": order.size_raw,
        "size": order.size_raw / size_precision,
        "remaining_size_raw": order.remaining_raw,
        "remaining_size": order.remaining_raw / size_precision,
        "queue_ahead_order_ids": list(order.queue_ahead_ids),
        "queue_ahead_size_raw": order.queue_ahead_raw,
        "queue_ahead_size": order.queue_ahead_raw / size_precision,
        "queue_ahead_order_count": len(order.queue_ahead_ids),
        "decision_book_block": order.decision_book_block,
        "eligible_block": order.eligible_block,
        "reserved_cash": decimal_to_number(order.reserved_cash),
        "reserved_inventory": decimal_to_number(order.reserved_inventory),
    }


def market_payload(book: l3.L3Book, price_precision: int) -> dict[str, Any]:
    best_bid, best_ask = best_prices_raw(book)
    midpoint = midpoint_raw(book)
    return {
        "best_bid_raw": best_bid,
        "best_ask_raw": best_ask,
        "best_bid": None if best_bid is None else best_bid / price_precision,
        "best_ask": None if best_ask is None else best_ask / price_precision,
        "midpoint": None if midpoint is None else midpoint / price_precision,
        "source": "reconstructed_explicit_l2",
    }


def marked_value(state: StrategyState, book: l3.L3Book, price_precision: int) -> Optional[Decimal]:
    midpoint = midpoint_raw(book)
    if midpoint is None:
        return None
    mark = Decimal(midpoint) / Decimal(price_precision)
    return state.cash + state.inventory * mark


def strategy_timeline_record(
    *,
    sequence: int,
    record_type: str,
    block_number: int,
    transaction_index: int,
    log_index: int,
    timestamp: int,
    book: l3.L3Book,
    price_precision: int,
    size_precision: int,
    state: StrategyState,
    initial_value: Decimal,
    market_event: Optional[dict[str, Any]] = None,
    fills: Optional[list[dict[str, Any]]] = None,
    cancellations: Optional[list[dict[str, Any]]] = None,
    placements: Optional[list[dict[str, Any]]] = None,
    activations: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    value = marked_value(state, book, price_precision)
    timestamp_text = datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "schema_version": "1.1",
        "sequence": sequence,
        "record_type": record_type,
        "block_number": block_number,
        "transaction_index": transaction_index,
        "log_index": log_index,
        "timestamp": timestamp_text,
        "market_event": market_event,
        "market": market_payload(book, price_precision),
        "mark_source": "reconstructed_midpoint",
        "active_simulated_orders": [
            active_order_payload(order, size_precision, price_precision)
            for order in sorted(state.active_orders.values(), key=lambda item: item.strategy_order_id)
        ],
        "activations": activations or [],
        "fills": fills or [],
        "cancellations": cancellations or [],
        "placements": placements or [],
        "cash": decimal_to_number(state.cash),
        "inventory": decimal_to_number(state.inventory),
        "reserved_cash": decimal_to_number(state.reserved_cash),
        "reserved_inventory": decimal_to_number(state.reserved_inventory),
        "available_cash": decimal_to_number(available_cash(state)),
        "available_inventory": decimal_to_number(available_inventory(state)),
        "marked_portfolio_value": None if value is None else decimal_to_number(value),
        "cumulative_pnl": None if value is None else decimal_to_number(value - initial_value),
        "cumulative_fees": decimal_to_number(state.maker_fees + state.action_fees),
    }


def cross_reaches_order(event: l3.ReplayEvent, order: StrategyOrder, price_precision: int) -> bool:
    if event.name != "Trade":
        return False
    order_trade_price = order.price_raw * 10**18 // price_precision
    if event.args["is_buy"]:
        return order.side == "sell" and int(event.args["price"]) >= order_trade_price
    return order.side == "buy" and int(event.args["price"]) <= order_trade_price


def activate_orders(
    book: l3.L3Book,
    state: StrategyState,
    block_number: int,
    size_precision: int,
) -> list[dict[str, Any]]:
    activations: list[dict[str, Any]] = []
    for order in sorted(state.active_orders.values(), key=lambda item: item.strategy_order_id):
        if order.status != "pending" or order.eligible_block > block_number:
            continue
        order.status = "active"
        order.queue_ahead_ids = list(book.queue(order.side == "buy", order.price_raw))
        order.queue_ahead_raw = sum(book.orders[order_id].size for order_id in order.queue_ahead_ids)
        activations.append(
            {
                "order_id": order.strategy_order_id,
                "side": order.side,
                "eligible_block": order.eligible_block,
                "activation_block": block_number,
                "queue_ahead_size": order.queue_ahead_raw / size_precision,
                "queue_ahead_order_count": len(order.queue_ahead_ids),
            }
        )
    return activations


def update_queue_ahead(event: l3.ReplayEvent, state: StrategyState) -> None:
    if event.name in {"OrderCreated", "FlipOrderCreated", "FlippedOrderCreated"}:
        return
    if "order_id" not in event.args:
        return
    event_order_id = int(event.args["order_id"])
    for order in state.active_orders.values():
        if order.status != "active" or event_order_id not in order.queue_ahead_ids:
            continue
        if event.name == "OrderCanceled":
            order.queue_ahead_raw -= int(event.args["size"])
            order.queue_ahead_ids.remove(event_order_id)
        elif event.name == "Trade":
            order.queue_ahead_raw -= int(event.args["filled_size"])
            if int(event.args["updated_size"]) == 0:
                order.queue_ahead_ids.remove(event_order_id)
        elif event.name == "FlipOrderUpdated":
            # A flip-order size increase is behind the order's prior queue claim.
            continue
        if order.queue_ahead_raw < 0:
            raise RuntimeError("simulated queue-ahead size became negative")


def release_order_reservation(state: StrategyState, order: StrategyOrder) -> None:
    state.reserved_cash -= order.reserved_cash
    state.reserved_inventory -= order.reserved_inventory
    order.reserved_cash = ZERO
    order.reserved_inventory = ZERO


def release_fill_reservation(
    state: StrategyState,
    order: StrategyOrder,
    fill_size: Decimal,
    fill_price: Decimal,
    config: StrategyConfig,
) -> None:
    if order.side == "buy":
        released = buy_reservation(fill_size, fill_price, config)
        released = min(released, order.reserved_cash)
        order.reserved_cash -= released
        state.reserved_cash -= released
    else:
        released = min(fill_size, order.reserved_inventory)
        order.reserved_inventory -= released
        state.reserved_inventory -= released


def apply_strategy_fills(
    event: l3.ReplayEvent,
    state: StrategyState,
    config: StrategyConfig,
    price_precision: int,
    size_precision: int,
    transaction_budget: Optional[TransactionFillBudget] = None,
    reference_midpoint_raw: Optional[int] = None,
) -> list[dict[str, Any]]:
    if event.name != "Trade":
        return []
    fills: list[dict[str, Any]] = []
    historical_depth_raw = int(event.args["filled_size"])
    for side in ("buy", "sell"):
        order = state.active_orders.get(side)
        if order is None or order.status != "active":
            continue
        if event.block_number < order.eligible_block or order.queue_ahead_raw != 0:
            continue
        if not cross_reaches_order(event, order, price_precision):
            continue
        fill_raw = min(order.remaining_raw, historical_depth_raw)
        if transaction_budget is not None:
            if transaction_budget.transaction_hash != event.transaction_hash:
                raise RuntimeError("transaction fill budget does not match the Trade event")
            fill_raw = min(fill_raw, transaction_budget.remaining_raw)
        if fill_raw <= 0:
            continue
        size = Decimal(fill_raw) / Decimal(size_precision)
        price = Decimal(order.price_raw) / Decimal(price_precision)
        cash_delta, inventory_delta, fee, notional = maker_fill_accounting(
            order.side, size, price, config.maker_fee_bps
        )
        release_fill_reservation(state, order, size, price, config)
        state.cash += cash_delta
        state.inventory += inventory_delta
        state.fill_records += 1
        state.filled_size += size
        state.turnover += notional
        state.maker_fees += fee
        order.remaining_raw -= fill_raw
        if transaction_budget is not None:
            transaction_budget.remaining_raw -= fill_raw
        consumed_percent = Decimal(fill_raw) * PERCENT / Decimal(historical_depth_raw)
        fills.append(
            {
                "strategy_order_id": order.strategy_order_id,
                "side": order.side,
                "price": decimal_to_number(price),
                "price_raw": order.price_raw,
                "size": decimal_to_number(size),
                "notional": decimal_to_number(notional),
                "maker_fee": decimal_to_number(fee),
                "simulated_fill_size_raw": fill_raw,
                "transaction_hash": event.transaction_hash,
                "historical_event_block_number": event.block_number,
                "historical_event_transaction_index": event.transaction_index,
                "historical_event_log_index": event.log_index,
                "historical_trade_order_id": int(event.args["order_id"]),
                "historical_taker_is_buy": bool(event.args["is_buy"]),
                "historical_event_fill_size_raw": historical_depth_raw,
                "historical_depth_size": historical_depth_raw / size_precision,
                "historical_depth_consumed_percent": decimal_to_number(consumed_percent),
                "reference_midpoint_raw": reference_midpoint_raw,
                "reference_midpoint": (
                    None
                    if reference_midpoint_raw is None
                    else reference_midpoint_raw / price_precision
                ),
                "transaction_participation_cap_percent": (
                    None
                    if config.max_transaction_participation_percent is None
                    else decimal_to_number(config.max_transaction_participation_percent)
                ),
                "transaction_fill_budget_raw": (
                    None
                    if transaction_budget is None
                    else transaction_budget.maximum_simulated_raw
                ),
                "transaction_fill_budget_remaining_raw": (
                    None if transaction_budget is None else transaction_budget.remaining_raw
                ),
                "event_material_impact_diagnostic": (
                    consumed_percent >= config.material_impact_threshold_percent
                ),
            }
        )
        if order.remaining_raw == 0:
            release_order_reservation(state, order)
            del state.active_orders[side]
    return fills


def build_transaction_impacts(
    events: Sequence[l3.ReplayEvent],
    fills: Sequence[dict[str, Any]],
    size_precision: int,
    material_threshold_percent: Decimal,
) -> list[dict[str, Any]]:
    """Allocate simulated fills once across Trade volume in each transaction."""
    trades_by_transaction: dict[str, list[l3.ReplayEvent]] = {}
    for event in events:
        if event.name != "Trade" or int(event.args.get("filled_size", 0)) <= 0:
            continue
        trades_by_transaction.setdefault(event.transaction_hash, []).append(event)
    fills_by_transaction: dict[str, list[dict[str, Any]]] = {}
    for fill in fills:
        fills_by_transaction.setdefault(str(fill["transaction_hash"]), []).append(fill)

    impacts: list[dict[str, Any]] = []
    transaction_order = sorted(
        fills_by_transaction,
        key=lambda transaction_hash: min(
            (
                int(fill["historical_event_block_number"]),
                int(fill["historical_event_transaction_index"]),
                int(fill["historical_event_log_index"]),
            )
            for fill in fills_by_transaction[transaction_hash]
        ),
    )
    for transaction_hash in transaction_order:
        trades = sorted(
            trades_by_transaction.get(transaction_hash, []),
            key=lambda event: (event.block_number, event.transaction_index, event.log_index),
        )
        if not trades:
            raise RuntimeError(
                f"simulated fills refer to transaction {transaction_hash} without Trade events"
            )
        event_positions = {
            (event.block_number, event.transaction_index, event.log_index): index
            for index, event in enumerate(trades)
        }
        remaining_capacity = [int(event.args["filled_size"]) for event in trades]
        total_historical_raw = sum(remaining_capacity)
        transaction_fills = sorted(
            fills_by_transaction[transaction_hash],
            key=lambda fill: (
                int(fill["historical_event_block_number"]),
                int(fill["historical_event_transaction_index"]),
                int(fill["historical_event_log_index"]),
                str(fill["strategy_order_id"]),
            ),
        )
        total_simulated_raw = sum(
            int(fill["simulated_fill_size_raw"]) for fill in transaction_fills
        )
        if total_simulated_raw > total_historical_raw:
            raise RuntimeError(
                f"simulated fills in transaction {transaction_hash} exceed total Trade volume"
            )

        displaced_events: list[dict[str, Any]] = []
        total_displaced_raw = 0
        for fill in transaction_fills:
            key = (
                int(fill["historical_event_block_number"]),
                int(fill["historical_event_transaction_index"]),
                int(fill["historical_event_log_index"]),
            )
            if key not in event_positions:
                raise RuntimeError(
                    f"simulated fill has no matching Trade event in transaction {transaction_hash}"
                )
            remaining_fill = int(fill["simulated_fill_size_raw"])
            taker_is_buy = bool(fill["historical_taker_is_buy"])
            for index in range(event_positions[key], len(trades)):
                trade = trades[index]
                if bool(trade.args["is_buy"]) != taker_is_buy:
                    continue
                allocated = min(remaining_fill, remaining_capacity[index])
                if allocated == 0:
                    continue
                remaining_capacity[index] -= allocated
                remaining_fill -= allocated
                total_displaced_raw += allocated
                displaced_events.append(
                    {
                        "historical_trade_order_id": int(trade.args["order_id"]),
                        "block_number": trade.block_number,
                        "transaction_index": trade.transaction_index,
                        "log_index": trade.log_index,
                        "displaced_size_raw": allocated,
                        "displaced_size": allocated / size_precision,
                    }
                )
                if remaining_fill == 0:
                    break
            if remaining_fill:
                raise RuntimeError(
                    f"simulated fills reuse historical aggressive volume in transaction {transaction_hash}"
                )

        impact_percent = Decimal(total_simulated_raw) * PERCENT / Decimal(total_historical_raw)
        impacts.append(
            {
                "transaction_hash": transaction_hash,
                "block_number": trades[0].block_number,
                "transaction_index": trades[0].transaction_index,
                "historical_trade_event_count": len(trades),
                "total_historical_aggressor_size_raw": total_historical_raw,
                "total_historical_aggressor_size": total_historical_raw / size_precision,
                "total_simulated_fill_size_raw": total_simulated_raw,
                "total_simulated_fill_size": total_simulated_raw / size_precision,
                "simulated_to_historical_percent": decimal_to_number(impact_percent),
                "historical_downstream_maker_volume_displaced_raw": total_displaced_raw,
                "historical_downstream_maker_volume_displaced": (
                    total_displaced_raw / size_precision
                ),
                "displaced_historical_trade_events": displaced_events,
                "material_impact": impact_percent >= material_threshold_percent,
            }
        )
    return impacts


def order_is_allowed(
    side: str,
    state: StrategyState,
    config: StrategyConfig,
    price_raw: int,
    price_precision: int,
    size_precision: int,
) -> bool:
    order_size_raw = to_raw_size(config.order_size, size_precision)
    size = Decimal(order_size_raw) / Decimal(size_precision)
    price = Decimal(price_raw) / Decimal(price_precision)
    if side == "buy":
        required_cash = buy_reservation(size, price, config) + config.fixed_action_fee
        if available_cash(state) < required_cash:
            return False
        return state.inventory + committed_buy_inventory(state, size_precision) + size <= config.maximum_inventory
    if available_cash(state) < config.fixed_action_fee:
        return False
    if available_inventory(state) < size:
        return False
    return state.inventory - state.reserved_inventory - size >= config.minimum_inventory


def charge_action_fee(state: StrategyState, config: StrategyConfig) -> None:
    if available_cash(state) < config.fixed_action_fee:
        raise RuntimeError("available cash cannot pay the configured action fee")
    state.cash -= config.fixed_action_fee
    state.action_fees += config.fixed_action_fee


def cancel_and_place_quotes(
    *,
    block_number: int,
    transaction_index: int,
    book: l3.L3Book,
    price_precision: int,
    size_precision: int,
    tick_size: int = 1,
    state: StrategyState,
    config: StrategyConfig,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    targets = target_quote_prices(book, config, tick_size)
    cancellations: list[dict[str, Any]] = []
    placements: list[dict[str, Any]] = []
    if not targets:
        return cancellations, placements

    for side in ("buy", "sell"):
        existing = state.active_orders.get(side)
        target_price = targets[side]
        replace = existing is not None and config.cancel_replace and should_replace(
            existing, target_price, config.replace_threshold_bps
        )
        if replace and existing is not None:
            released_cash = existing.reserved_cash
            released_inventory = existing.reserved_inventory
            release_order_reservation(state, existing)
            del state.active_orders[side]
            charge_action_fee(state, config)
            state.cancellations += 1
            cancellations.append(
                {
                    "order_id": existing.strategy_order_id,
                    "side": side,
                    "reason": "replace",
                    "status_before_cancel": existing.status,
                    "released_cash": decimal_to_number(released_cash),
                    "released_inventory": decimal_to_number(released_inventory),
                    "action_fee": decimal_to_number(config.fixed_action_fee),
                }
            )
            existing = None
        if existing is not None:
            continue
        if not order_is_allowed(
            side, state, config, target_price, price_precision, size_precision
        ):
            continue
        charge_action_fee(state, config)
        order_number = state.next_order_number
        state.next_order_number += 1
        order_size_raw = to_raw_size(config.order_size, size_precision)
        order_size = Decimal(order_size_raw) / Decimal(size_precision)
        price = Decimal(target_price) / Decimal(price_precision)
        reservation_cash = buy_reservation(order_size, price, config) if side == "buy" else ZERO
        reservation_inventory = order_size if side == "sell" else ZERO
        order = StrategyOrder(
            strategy_order_id=f"strategy-{order_number}",
            side=side,
            price_raw=target_price,
            size_raw=order_size_raw,
            remaining_raw=order_size_raw,
            status="pending",
            eligible_block=block_number + config.inclusion_delay_blocks,
            decision_book_block=block_number,
            queue_ahead_ids=[],
            queue_ahead_raw=0,
            placed_block=block_number,
            placed_transaction_index=transaction_index,
            reserved_cash=reservation_cash,
            reserved_inventory=reservation_inventory,
        )
        state.active_orders[side] = order
        state.reserved_cash += reservation_cash
        state.reserved_inventory += reservation_inventory
        state.placements += 1
        placements.append(
            {
                "order_id": order.strategy_order_id,
                "side": side,
                "status": order.status,
                "price_raw": target_price,
                "price": target_price / price_precision,
                "size_raw": order_size_raw,
                "size": order_size_raw / size_precision,
                "queue_ahead_order_count": 0,
                "queue_ahead_size_raw": 0,
                "decision_book_block": block_number,
                "eligible_block": order.eligible_block,
                "reserved_cash": decimal_to_number(reservation_cash),
                "reserved_inventory": decimal_to_number(reservation_inventory),
                "action_fee": decimal_to_number(config.fixed_action_fee),
            }
        )
    assert_capital_invariants(state, config, size_precision)
    return cancellations, placements


def maximum_drawdown(values: Iterable[Decimal]) -> Decimal:
    high_water: Optional[Decimal] = None
    maximum = ZERO
    for value in values:
        high_water = value if high_water is None else max(high_water, value)
        maximum = max(maximum, high_water - value)
    return maximum


def pnl_breakdown(
    initial_value: Decimal,
    final_value: Decimal,
    maker_fees: Decimal,
    action_fees: Decimal,
) -> dict[str, Decimal]:
    costs = maker_fees + action_fees
    net = final_value - initial_value
    gross_before_costs = net + costs
    if gross_before_costs - costs != net:
        raise RuntimeError("P&L accounting identity failed")
    return {
        "gross_pnl_before_costs": gross_before_costs,
        "applicable_costs": costs,
        "net_pnl": net,
    }


def transaction_fill_budgets(
    events: Sequence[l3.ReplayEvent],
    maximum_participation_percent: Optional[Decimal],
) -> dict[str, TransactionFillBudget]:
    """Create fixed budgets for atomic transactions before event replay."""
    if maximum_participation_percent is None:
        return {}
    totals: dict[str, int] = {}
    for event in events:
        if event.name == "Trade":
            totals[event.transaction_hash] = totals.get(event.transaction_hash, 0) + int(
                event.args["filled_size"]
            )
    budgets: dict[str, TransactionFillBudget] = {}
    for transaction_hash, total_raw in totals.items():
        maximum_raw = int(
            (Decimal(total_raw) * maximum_participation_percent / PERCENT).to_integral_value(
                rounding=ROUND_FLOOR
            )
        )
        budgets[transaction_hash] = TransactionFillBudget(
            transaction_hash=transaction_hash,
            total_historical_raw=total_raw,
            maximum_simulated_raw=maximum_raw,
            remaining_raw=maximum_raw,
        )
    return budgets


def strategy_result_decomposition(
    fills: Sequence[dict[str, Any]],
    *,
    initial_inventory: Decimal,
    final_inventory: Decimal,
    final_mark_raw: int,
    price_precision: int,
    size_precision: int,
    maker_fees: Decimal,
    action_fees: Decimal,
) -> dict[str, Decimal | int]:
    """Decompose excess P&L with fill-time midpoint marks only."""
    buy_count = 0
    sell_count = 0
    buy_size = ZERO
    sell_size = ZERO
    buy_notional = ZERO
    sell_notional = ZERO
    spread_capture = ZERO
    inventory_mark_to_market = ZERO
    for fill in fills:
        size_raw = int(fill["simulated_fill_size_raw"])
        price_raw = int(fill["price_raw"])
        reference_raw_value = fill.get("reference_midpoint_raw")
        if reference_raw_value is None:
            raise RuntimeError("fill does not contain a causal reference midpoint")
        reference_raw = int(reference_raw_value)
        size = Decimal(size_raw) / Decimal(size_precision)
        price = Decimal(price_raw) / Decimal(price_precision)
        signed_size = size if fill["side"] == "buy" else -size
        if fill["side"] == "buy":
            buy_count += 1
            buy_size += size
            buy_notional += size * price
        else:
            sell_count += 1
            sell_size += size
            sell_notional += size * price
        spread_capture += signed_size * Decimal(reference_raw - price_raw) / Decimal(
            price_precision
        )
        inventory_mark_to_market += signed_size * Decimal(
            final_mark_raw - reference_raw
        ) / Decimal(price_precision)
    inventory_change = final_inventory - initial_inventory
    excess_before_costs = spread_capture + inventory_mark_to_market
    excess_after_costs = excess_before_costs - maker_fees - action_fees
    return {
        "buy_fill_count": buy_count,
        "sell_fill_count": sell_count,
        "buy_fill_size": buy_size,
        "sell_fill_size": sell_size,
        "buy_notional": buy_notional,
        "sell_notional": sell_notional,
        "ending_inventory_change": inventory_change,
        "realized_spread_capture": spread_capture,
        "inventory_mark_to_market_contribution": inventory_mark_to_market,
        "maker_fees": maker_fees,
        "action_fees": action_fees,
        "strategy_excess_pnl_before_costs": excess_before_costs,
        "strategy_excess_pnl": excess_after_costs,
    }


def validate_config(config: StrategyConfig) -> None:
    if config.starting_cash < ZERO:
        raise ValueError("starting cash must be non-negative")
    if config.order_size <= ZERO:
        raise ValueError("order size must be positive")
    if config.minimum_inventory > config.maximum_inventory:
        raise ValueError("minimum inventory must not exceed maximum inventory")
    if not config.minimum_inventory <= config.starting_inventory <= config.maximum_inventory:
        raise ValueError("starting inventory must be inside the configured limits")
    if config.bid_offset_bps < ZERO or config.ask_offset_bps < ZERO:
        raise ValueError("quote offsets must be non-negative")
    if config.replace_threshold_bps < ZERO:
        raise ValueError("replace threshold must be non-negative")
    if config.fixed_action_fee < ZERO:
        raise ValueError("fixed action fee must be non-negative")
    if config.inclusion_delay_blocks < 1:
        raise ValueError("block inclusion delay must be at least one block")
    if config.material_impact_threshold_percent <= ZERO:
        raise ValueError("material-impact threshold must be positive")
    if config.max_transaction_participation_percent is not None and not (
        ZERO < config.max_transaction_participation_percent <= PERCENT
    ):
        raise ValueError("transaction participation cap must be above zero and at most 100")


def verify_validation_summary(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("event_parity", {}).get("status") != "pass":
        raise RuntimeError("validation summary does not contain passing Envio/RPC parity")
    validation = payload.get("validation", {})
    if validation.get("maximum_price_level_error_raw") != 0:
        raise RuntimeError("validation summary has a non-zero price-level error")
    if validation.get("maximum_aggregate_size_error_raw") != 0:
        raise RuntimeError("validation summary has a non-zero aggregate-size error")
    if validation.get("queue_invariant_status") != "pass":
        raise RuntimeError("validation summary does not contain passing queue invariants")
    if validation.get("maximum_midpoint_error") != 0:
        raise RuntimeError("validation summary has a non-zero or missing midpoint error")
    if payload.get("cancellation_summary_audit", {}).get("status") != "pass":
        raise RuntimeError("validation summary does not contain a passing cancellation audit")
    return payload


def _read_decoded_event_cache(
    cache_path: Path,
    source_sha256: str,
) -> tuple[list[l3.ReplayEvent], dict[int, int]]:
    """Read a safe SQLite event cache that is bound to the source file hash."""
    events: list[l3.ReplayEvent] = []
    timestamps: dict[int, int] = {}
    prior_position: tuple[int, int, int] | None = None
    connection = sqlite3.connect(f"file:{cache_path}?mode=ro", uri=True)
    try:
        metadata = dict(connection.execute("SELECT key, value FROM metadata"))
        if metadata.get("schema_version") != DECODED_EVENT_CACHE_SCHEMA_VERSION:
            raise RuntimeError("decoded event cache schema does not match")
        if metadata.get("source_sha256") != source_sha256:
            raise RuntimeError("decoded event cache source hash does not match")
        query = (
            "SELECT block_number, block_timestamp, transaction_index, log_index, "
            "transaction_hash, name, args_json, address, data, topic0, topic1, topic2, topic3 "
            "FROM events ORDER BY sequence"
        )
        for row in connection.execute(query):
            block_number = int(row[0])
            block_timestamp = int(row[1])
            transaction_index = int(row[2])
            log_index = int(row[3])
            position = (block_number, transaction_index, log_index)
            if prior_position is not None and position < prior_position:
                raise RuntimeError("decoded event cache is not in deterministic replay order")
            prior_position = position
            prior_timestamp = timestamps.get(block_number)
            if prior_timestamp is not None and prior_timestamp != block_timestamp:
                raise RuntimeError(
                    f"decoded event cache has conflicting timestamps for block {block_number}"
                )
            timestamps[block_number] = block_timestamp
            transaction_hash = str(row[4])
            topics = [str(value) for value in row[9:13] if value is not None]
            events.append(l3.ReplayEvent(
                block_number=block_number,
                transaction_index=transaction_index,
                log_index=log_index,
                transaction_hash=transaction_hash,
                name=str(row[5]),
                args=json.loads(str(row[6])),
                raw={
                    "address": str(row[7]),
                    "blockNumber": block_number,
                    "transactionHash": transaction_hash,
                    "transactionIndex": transaction_index,
                    "logIndex": log_index,
                    "data": str(row[8]),
                    "topics": topics,
                },
            ))
    finally:
        connection.close()
    return events, timestamps


def _write_decoded_event_cache(
    cache_path: Path,
    source_sha256: str,
    events: Sequence[l3.ReplayEvent],
    timestamps: dict[int, int],
) -> None:
    """Write decoded events as an atomic SQLite file without executable data."""
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = cache_path.with_suffix(f"{cache_path.suffix}.{os.getpid()}.tmp")
    temporary.unlink(missing_ok=True)
    connection = sqlite3.connect(temporary)
    try:
        connection.execute("PRAGMA journal_mode = OFF")
        connection.execute("PRAGMA synchronous = OFF")
        connection.executescript(
            """
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE events (
                sequence INTEGER PRIMARY KEY,
                block_number INTEGER NOT NULL,
                block_timestamp INTEGER NOT NULL,
                transaction_index INTEGER NOT NULL,
                log_index INTEGER NOT NULL,
                transaction_hash TEXT NOT NULL,
                name TEXT NOT NULL,
                args_json TEXT NOT NULL,
                address TEXT NOT NULL,
                data TEXT NOT NULL,
                topic0 TEXT,
                topic1 TEXT,
                topic2 TEXT,
                topic3 TEXT
            );
            """
        )
        connection.executemany(
            "INSERT INTO metadata(key, value) VALUES(?, ?)",
            (
                ("schema_version", DECODED_EVENT_CACHE_SCHEMA_VERSION),
                ("source_sha256", source_sha256),
                ("record_count", str(len(events))),
            ),
        )

        def cache_rows() -> Iterable[tuple[Any, ...]]:
            for sequence, event in enumerate(events):
                topics = list(event.raw.get("topics", []))[:4]
                topics.extend([None] * (4 - len(topics)))
                yield (
                    sequence,
                    event.block_number,
                    timestamps[event.block_number],
                    event.transaction_index,
                    event.log_index,
                    event.transaction_hash,
                    event.name,
                    json.dumps(
                        event.args,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ),
                    str(event.raw.get("address", "")),
                    str(event.raw.get("data", "0x")),
                    *topics,
                )

        connection.executemany(
            "INSERT INTO events VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            cache_rows(),
        )
        connection.commit()
    finally:
        connection.close()
    temporary.replace(cache_path)


def load_verified_source_events(
    validation_summary_path: Path,
    validation: dict[str, Any],
    metrics: Optional[dict[str, Any]] = None,
    *,
    source_sha256: str | None = None,
    decoded_cache_path: Path | None = None,
) -> tuple[list[l3.ReplayEvent], dict[int, int]] | None:
    event_input = validation.get("event_input", {})
    relative_path = event_input.get("path")
    if not relative_path:
        return None
    validation_root = validation_summary_path.parent.resolve()
    candidate = Path(str(relative_path))
    if candidate.is_absolute():
        raise RuntimeError("verified event input path must be relative")
    path = (validation_root / candidate).resolve()
    if path.parent != validation_root:
        raise RuntimeError("verified event input path must stay inside its validation directory")
    expected_count = int(event_input.get("record_count", 0))
    if decoded_cache_path is not None and source_sha256 and decoded_cache_path.is_file():
        cache_start = time.perf_counter()
        try:
            cached_events, cached_timestamps = _read_decoded_event_cache(
                decoded_cache_path, source_sha256
            )
            if expected_count and len(cached_events) != expected_count:
                raise RuntimeError(
                    f"decoded event cache has {len(cached_events)} records; expected {expected_count}"
                )
            if metrics is not None:
                metrics["decoded_event_cache_hit"] = True
                metrics["decoded_event_cache_load_seconds"] = time.perf_counter() - cache_start
            return cached_events, cached_timestamps
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError, sqlite3.Error):
            # The source JSONL remains authoritative. A stopped or old cache
            # build must never prevent a verified replay.
            if metrics is not None:
                metrics["decoded_event_cache_rebuilt"] = True
    events: list[l3.ReplayEvent] = []
    timestamps: dict[int, int] = {}
    prior_position: tuple[int, int, int] | None = None
    parse_start = time.perf_counter()
    with path.open("r", encoding="utf-8") as input_file:
        for line_number, line in enumerate(input_file, start=1):
            if not line.strip():
                continue
            try:
                log = json.loads(line)
                block_number = int(log["block_number"])
                block_timestamp = int(log["block_timestamp"])
                event = l3.decode_event(log)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                raise RuntimeError(
                    f"invalid verified event row {path}:{line_number}: {error}"
                ) from error
            prior = timestamps.get(block_number)
            if prior is not None and prior != block_timestamp:
                raise RuntimeError(
                    f"verified event input has conflicting timestamps for block {block_number}"
                )
            timestamps[block_number] = block_timestamp
            position = (block_number, int(log["transaction_index"]), int(log["log_index"]))
            if prior_position is not None and position < prior_position:
                raise RuntimeError("verified event input is not in deterministic replay order")
            prior_position = position
            events.append(event)
    if metrics is not None:
        metrics["event_stream_parse_and_decode_seconds"] = time.perf_counter() - parse_start
    expected_count = expected_count or len(events)
    if len(events) != expected_count:
        raise RuntimeError(
            f"verified event input has {len(events)} records; expected {expected_count}"
        )
    if decoded_cache_path is not None and source_sha256:
        cache_write_start = time.perf_counter()
        try:
            _write_decoded_event_cache(
                decoded_cache_path, source_sha256, events, timestamps
            )
            if metrics is not None:
                metrics["decoded_event_cache_write_seconds"] = (
                    time.perf_counter() - cache_write_start
                )
                metrics["decoded_event_cache_hit"] = False
        except (OSError, ValueError, sqlite3.Error):
            if metrics is not None:
                metrics["decoded_event_cache_write_failed"] = True
    return events, timestamps


def run_strategy(
    initial_book: l3.L3Book,
    events: list[l3.ReplayEvent],
    start_block: int,
    end_block: int,
    config: StrategyConfig,
    price_precision: int,
    size_precision: int,
    tick_size: int,
    timestamps: dict[int, int],
    verified_input: Optional[dict[str, Any]] = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    validate_config(config)
    interval_events = sorted(
        (event for event in events if start_block <= event.block_number <= end_block),
        key=lambda item: (item.block_number, item.transaction_index, item.log_index),
    )
    if not interval_events:
        raise RuntimeError("verified interval does not contain strategy events")

    checkpoint_block = start_block - 1
    book = initial_book.copy()
    book.check_invariants()
    initial_mark_raw = midpoint_raw(book)
    if initial_mark_raw is None:
        raise RuntimeError("strategy checkpoint has an empty side")
    initial_mark = Decimal(initial_mark_raw) / Decimal(price_precision)
    initial_value = config.starting_cash + config.starting_inventory * initial_mark
    state = StrategyState(cash=config.starting_cash, inventory=config.starting_inventory)
    timeline: list[dict[str, Any]] = []
    sequence = 0
    invariant_checks = 1

    initial_cancellations, initial_placements = cancel_and_place_quotes(
        block_number=checkpoint_block,
        transaction_index=-1,
        book=book,
        price_precision=price_precision,
        size_precision=size_precision,
        tick_size=tick_size,
        state=state,
        config=config,
    )
    sequence += 1
    timeline.append(
        strategy_timeline_record(
            sequence=sequence,
            record_type="strategy_decision",
            block_number=checkpoint_block,
            transaction_index=-1,
            log_index=-1,
            timestamp=timestamps[checkpoint_block],
            book=book,
            price_precision=price_precision,
            size_precision=size_precision,
            state=state,
            initial_value=initial_value,
            cancellations=initial_cancellations,
            placements=initial_placements,
        )
    )

    event_blocks: dict[int, list[l3.ReplayEvent]] = {}
    for event in interval_events:
        event_blocks.setdefault(event.block_number, []).append(event)
    fill_budgets = transaction_fill_budgets(
        interval_events, config.max_transaction_participation_percent
    )

    for block_number in sorted(event_blocks):
        activations = activate_orders(book, state, block_number, size_precision)
        if activations:
            assert_capital_invariants(state, config, size_precision)
        last_transaction_index = -1
        for event_index, event in enumerate(event_blocks[block_number]):
            causal_midpoint_raw = midpoint_raw(book)
            fills = apply_strategy_fills(
                event,
                state,
                config,
                price_precision,
                size_precision,
                fill_budgets.get(event.transaction_hash),
                causal_midpoint_raw,
            )
            update_queue_ahead(event, state)
            l3.apply_event(book, event, price_precision)
            book.check_invariants()
            invariant_checks += 1
            assert_capital_invariants(state, config, size_precision)
            last_transaction_index = event.transaction_index
            sequence += 1
            timeline.append(
                strategy_timeline_record(
                    sequence=sequence,
                    record_type="market_event",
                    block_number=event.block_number,
                    transaction_index=event.transaction_index,
                    log_index=event.log_index,
                    timestamp=timestamps[event.block_number],
                    book=book,
                    price_precision=price_precision,
                    size_precision=size_precision,
                    state=state,
                    initial_value=initial_value,
                    market_event={"name": event.name, "args": event.args},
                    activations=activations if event_index == 0 else [],
                )
            )
            for fill in fills:
                sequence += 1
                timeline.append(
                    strategy_timeline_record(
                        sequence=sequence,
                        record_type="strategy_fill",
                        block_number=event.block_number,
                        transaction_index=event.transaction_index,
                        log_index=event.log_index,
                        timestamp=timestamps[event.block_number],
                        book=book,
                        price_precision=price_precision,
                        size_precision=size_precision,
                        state=state,
                        initial_value=initial_value,
                        market_event={"name": event.name, "args": event.args},
                        fills=[fill],
                    )
                )
        cancellations, placements = cancel_and_place_quotes(
            block_number=block_number,
            transaction_index=last_transaction_index,
            book=book,
            price_precision=price_precision,
            size_precision=size_precision,
            tick_size=tick_size,
            state=state,
            config=config,
        )
        sequence += 1
        timeline.append(
            strategy_timeline_record(
                sequence=sequence,
                record_type="strategy_decision",
                block_number=block_number,
                transaction_index=last_transaction_index,
                log_index=10**9,
                timestamp=timestamps[block_number],
                book=book,
                price_precision=price_precision,
                size_precision=size_precision,
                state=state,
                initial_value=initial_value,
                cancellations=cancellations,
                placements=placements,
            )
        )

    final_mark_raw = midpoint_raw(book)
    if final_mark_raw is None:
        raise RuntimeError("final book has an empty side")
    final_mark = Decimal(final_mark_raw) / Decimal(price_precision)
    final_value = state.cash + state.inventory * final_mark
    pnl = pnl_breakdown(initial_value, final_value, state.maker_fees, state.action_fees)
    equity_values = [
        Decimal(str(record["marked_portfolio_value"]))
        for record in timeline
        if record["marked_portfolio_value"] is not None
    ]
    max_drawdown = maximum_drawdown(equity_values)
    fill_records = [fill for record in timeline for fill in record["fills"]]
    hold_final_value = config.starting_cash + config.starting_inventory * final_mark
    hold_pnl = hold_final_value - initial_value
    strategy_excess_pnl = final_value - hold_final_value
    decomposition = strategy_result_decomposition(
        fill_records,
        initial_inventory=config.starting_inventory,
        final_inventory=state.inventory,
        final_mark_raw=final_mark_raw,
        price_precision=price_precision,
        size_precision=size_precision,
        maker_fees=state.maker_fees,
        action_fees=state.action_fees,
    )
    if decomposition["strategy_excess_pnl"] != strategy_excess_pnl:
        raise RuntimeError("strategy excess P&L decomposition identity failed")
    total_order_actions = state.placements + state.cancellations
    gross_pnl_per_action = (
        pnl["gross_pnl_before_costs"] / Decimal(total_order_actions)
        if total_order_actions
        else ZERO
    )
    excess_pnl_per_action = (
        strategy_excess_pnl / Decimal(total_order_actions)
        if total_order_actions
        else ZERO
    )
    break_even_fixed_cost_per_action = (
        (strategy_excess_pnl + state.action_fees) / Decimal(total_order_actions)
        if total_order_actions
        else ZERO
    )
    transaction_impacts = build_transaction_impacts(
        interval_events,
        fill_records,
        size_precision,
        config.material_impact_threshold_percent,
    )
    max_transaction_impact_percent = max(
        (
            Decimal(str(impact["simulated_to_historical_percent"]))
            for impact in transaction_impacts
        ),
        default=ZERO,
    )
    material_transactions = sum(
        1 for impact in transaction_impacts if impact["material_impact"]
    )
    summary = {
        "schema_version": "1.2",
        "status": "pass",
        "market": MARKET_ADDRESS,
        "interval": {
            "checkpoint_block": checkpoint_block,
            "from_block": start_block,
            "to_block": end_block,
        },
        "verified_input": verified_input or {},
        "causality": {
            "decision_time": "after all reconstructed events in the decision block",
            "inclusion_delay_blocks": config.inclusion_delay_blocks,
            "first_eligible_rule": "decision block plus inclusion_delay_blocks",
            "same_block_fill_allowed": False,
            "decision_book_rule": "decisions use only reconstructed events already processed",
            "transaction_cap_rule": (
                "A transaction is atomic. Its cap is fixed from its historical total Trade volume, "
                "then consumed in deterministic log order during replay. Unfilled order size stays active."
            ),
        },
        "config": {
            key: (decimal_to_number(value) if isinstance(value, Decimal) else value)
            for key, value in asdict(config).items()
        },
        "mark_source": "reconstructed_midpoint",
        "market_parameters": {
            "price_precision": price_precision,
            "size_precision": size_precision,
            "tick_size": tick_size,
        },
        "metrics": {
            "initial_cash": decimal_to_number(config.starting_cash),
            "initial_inventory": decimal_to_number(config.starting_inventory),
            "initial_midpoint": decimal_to_number(initial_mark),
            "initial_portfolio_value": decimal_to_number(initial_value),
            "final_cash": decimal_to_number(state.cash),
            "final_inventory": decimal_to_number(state.inventory),
            "final_midpoint": decimal_to_number(final_mark),
            "final_portfolio_value": decimal_to_number(final_value),
            "ending_reserved_cash": decimal_to_number(state.reserved_cash),
            "ending_reserved_inventory": decimal_to_number(state.reserved_inventory),
            "placement_count": state.placements,
            "cancellation_count": state.cancellations,
            "fill_count": state.fill_records,
            "filled_size": decimal_to_number(state.filled_size),
            "turnover": decimal_to_number(state.turnover),
            "historical_executed_volume_raw": verified_input.get(
                "historical_executed_volume_raw"
            )
            if verified_input
            else None,
            "historical_executed_volume_mon": verified_input.get(
                "historical_executed_volume_mon"
            )
            if verified_input
            else None,
            "maker_fees": decimal_to_number(state.maker_fees),
            "action_fees": decimal_to_number(state.action_fees),
            "gross_pnl_before_costs": decimal_to_number(pnl["gross_pnl_before_costs"]),
            "applicable_costs": decimal_to_number(pnl["applicable_costs"]),
            "net_pnl": decimal_to_number(pnl["net_pnl"]),
            "maximum_drawdown": decimal_to_number(max_drawdown),
            "queue_invariant_checks": invariant_checks,
            "ending_active_order_count": len(state.active_orders),
        },
        "hold_baseline": {
            "starting_cash": decimal_to_number(config.starting_cash),
            "starting_inventory": decimal_to_number(config.starting_inventory),
            "initial_midpoint": decimal_to_number(initial_mark),
            "initial_portfolio_value": decimal_to_number(initial_value),
            "final_midpoint": decimal_to_number(final_mark),
            "final_portfolio_value": decimal_to_number(hold_final_value),
            "hold_pnl": decimal_to_number(hold_pnl),
            "strategy_excess_pnl": decimal_to_number(strategy_excess_pnl),
        },
        "decomposition": {
            key: decimal_to_number(value) if isinstance(value, Decimal) else value
            for key, value in decomposition.items()
        },
        "operational_costs": {
            "placements": state.placements,
            "cancellations": state.cancellations,
            "total_order_actions": total_order_actions,
            "gross_pnl_per_action": decimal_to_number(gross_pnl_per_action),
            "strategy_excess_pnl_per_action": decimal_to_number(excess_pnl_per_action),
            "break_even_fixed_cost_per_action": decimal_to_number(
                break_even_fixed_cost_per_action
            ),
        },
        "counterfactual": {
            "assumption": (
                "Simulated orders do not change historical taker behavior, historical order actions, "
                "or subsequent market events."
            ),
            "headline_unit": "transaction",
            "transaction_denominator": (
                "the sum of filled_size across all historical Trade events in the transaction"
            ),
            "per_event_percentages_are_diagnostic_only": True,
            "material_impact_threshold_percent": decimal_to_number(
                config.material_impact_threshold_percent
            ),
            "maximum_transaction_participation_percent": (
                None
                if config.max_transaction_participation_percent is None
                else decimal_to_number(config.max_transaction_participation_percent)
            ),
            "max_transaction_impact_percent": decimal_to_number(
                max_transaction_impact_percent
            ),
            "material_impact_transaction_count": material_transactions,
            "run_flagged": material_transactions > 0,
            "transaction_impacts": transaction_impacts,
        },
        "fills": fill_records,
        "timeline": {
            "backend_is_complete": True,
            "playback_delay_seconds": 0,
            "record_count": len(timeline),
            "first_timestamp": timeline[0]["timestamp"],
            "last_timestamp": timeline[-1]["timestamp"],
        },
    }
    return summary, timeline


def canonical_json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, indent=2, sort_keys=True).encode("utf-8") + b"\n"


def write_outputs(output_dir: Path, summary: dict[str, Any], timeline: list[dict[str, Any]]) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_bytes = canonical_json_bytes(summary)
    timeline_bytes = b"".join(
        json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
        for record in timeline
    )
    summary_path = output_dir / "summary.json"
    timeline_path = output_dir / "timeline.jsonl"
    summary_path.write_bytes(summary_bytes)
    timeline_path.write_bytes(timeline_bytes)
    manifest = {
        "summary_sha256": hashlib.sha256(summary_bytes).hexdigest(),
        "timeline_sha256": hashlib.sha256(timeline_bytes).hexdigest(),
    }
    (output_dir / "manifest.json").write_bytes(canonical_json_bytes(manifest))
    return manifest


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--validation-summary",
        type=Path,
        default=Path("validation_output/summary.json"),
        help="passing reconstruction validation summary",
    )
    parser.add_argument("--output-dir", type=Path, default=Path("strategy_output"))
    parser.add_argument("--starting-cash", type=decimal_argument, default=Decimal("1000"))
    parser.add_argument("--starting-inventory", type=decimal_argument, default=Decimal("1000"))
    parser.add_argument("--bid-offset-bps", type=decimal_argument, default=Decimal("10"))
    parser.add_argument("--ask-offset-bps", type=decimal_argument, default=Decimal("10"))
    parser.add_argument("--order-size", type=decimal_argument, default=Decimal("200"))
    parser.add_argument("--minimum-inventory", type=decimal_argument, default=Decimal("0"))
    parser.add_argument("--maximum-inventory", type=decimal_argument, default=Decimal("2000"))
    parser.add_argument("--rpc-url", default=l3.DEFAULT_RPC_URL)
    parser.add_argument("--replace-threshold-bps", type=decimal_argument, default=Decimal("1"))
    parser.add_argument("--maker-fee-bps", type=decimal_argument, default=Decimal("0"))
    parser.add_argument("--fixed-action-fee", type=decimal_argument, default=Decimal("0"))
    parser.add_argument("--inclusion-delay-blocks", type=int, default=1)
    parser.add_argument(
        "--material-impact-threshold-percent", type=decimal_argument, default=Decimal("25")
    )
    parser.add_argument(
        "--max-transaction-participation-percent",
        type=decimal_argument,
        help="optional causal cap as a percent of total historical Trade volume per transaction",
    )
    parser.add_argument("--no-cancel-replace", action="store_true")
    args = parser.parse_args(argv)
    try:
        validation = verify_validation_summary(args.validation_summary)
        interval = validation["interval"]
        start_block = int(interval["from_block"])
        end_block = int(interval["to_block"])
        expected_rpc_count = int(validation["event_parity"]["rpc_event_count"])
        rpc = RpcClient(args.rpc_url, timeout=180)
        verified_source = load_verified_source_events(
            args.validation_summary, validation
        )
        source_timestamps: dict[int, int] = {}
        if verified_source is None:
            rpc_logs = rpc.logs(MARKET_ADDRESS, start_block, end_block)
            events = l3.decode_events(rpc_logs)
        else:
            events, source_timestamps = verified_source
        if len(events) != expected_rpc_count:
            raise RuntimeError(
                f"RPC event count changed: expected {expected_rpc_count}, received {len(events)}"
            )
        config = StrategyConfig(
            starting_cash=args.starting_cash,
            starting_inventory=args.starting_inventory,
            bid_offset_bps=args.bid_offset_bps,
            ask_offset_bps=args.ask_offset_bps,
            order_size=args.order_size,
            minimum_inventory=args.minimum_inventory,
            maximum_inventory=args.maximum_inventory,
            cancel_replace=not args.no_cancel_replace,
            replace_threshold_bps=args.replace_threshold_bps,
            maker_fee_bps=args.maker_fee_bps,
            fixed_action_fee=args.fixed_action_fee,
            inclusion_delay_blocks=args.inclusion_delay_blocks,
            material_impact_threshold_percent=args.material_impact_threshold_percent,
            max_transaction_participation_percent=args.max_transaction_participation_percent,
        )
        params = market_parameters(rpc, MARKET_ADDRESS)
        price_precision = int(params["price_precision"])
        size_precision = int(params["size_precision"])
        tick_size = int(params["tick_size"])
        checkpoint_book, _, _ = l3.load_checkpoint(rpc, MARKET_ADDRESS, start_block - 1)
        timestamp_blocks = {start_block - 1, *(event.block_number for event in events)}
        timestamps = {
            block: timestamp
            for block, timestamp in source_timestamps.items()
            if block in timestamp_blocks
        }
        missing_timestamp_blocks = timestamp_blocks - set(timestamps)
        if missing_timestamp_blocks:
            timestamps.update(rpc.block_timestamps(missing_timestamp_blocks))
        verified_input = {
            "event_source": validation["event_source"],
            "parity_status": validation["event_parity"]["status"],
            "envio_event_count": validation["event_parity"]["envio_event_count"],
            "rpc_event_count": expected_rpc_count,
            "maximum_price_level_error_raw": validation["validation"][
                "maximum_price_level_error_raw"
            ],
            "maximum_aggregate_size_error_raw": validation["validation"][
                "maximum_aggregate_size_error_raw"
            ],
            "historical_executed_volume_raw": interval.get(
                "executed_volume_raw"
            ),
            "historical_executed_volume_mon": interval.get(
                "executed_volume_mon"
            ),
        }
        summary, timeline = run_strategy(
            checkpoint_book,
            events,
            start_block,
            end_block,
            config,
            price_precision,
            size_precision,
            tick_size,
            timestamps,
            verified_input,
        )
        manifest = write_outputs(args.output_dir, summary, timeline)
    except (OSError, ValueError, RuntimeError, ReconstructionError, l3.HyperSyncError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=2, sort_keys=True))
    print(f"summary_sha256={manifest['summary_sha256']}")
    print(f"timeline_sha256={manifest['timeline_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
