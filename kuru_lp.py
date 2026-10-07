#!/usr/bin/env python3
"""Kuru concentrated-liquidity backtest vertical slice.

This module consumes the verified reconstruction. It does not change it.
The integer position builder follows @kuru-labs/kuru-sdk 0.0.95.
"""

from __future__ import annotations

import argparse
import json
import time
from bisect import insort
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from decimal import Decimal, ROUND_FLOOR
from itertools import groupby
from pathlib import Path
from typing import Any, Optional, Sequence

try:
    import resource
except ImportError:  # pragma: no cover - not available in Windows Python.
    resource = None

import l3_vertical_slice as l3
import strategy_vertical_slice as mm
from rebuild_from_chain import MARKET_ADDRESS, RpcClient, market_parameters


FEE_DENOMINATOR = 1_000_000
ZERO = Decimal(0)


@dataclass(frozen=True)
class LPPosition:
    side: str
    price: int
    liquidity: int
    flip_price: int


@dataclass(frozen=True)
class LPDistribution:
    bids: tuple[LPPosition, ...]
    asks: tuple[LPPosition, ...]
    quote_liquidity: int
    base_liquidity: int
    min_size_error: bool


@dataclass(frozen=True)
class LPConfig:
    shape: str
    minimum_price_raw: int
    maximum_price_raw: int
    fee_pps: int
    quote_deposit_raw: int
    base_deposit_raw: int
    maker_fee_bps: Decimal = ZERO
    fixed_action_fee: Decimal = ZERO
    inclusion_delay_blocks: int = 1
    max_transaction_participation_percent: Optional[Decimal] = Decimal("10")
    material_impact_threshold_percent: Decimal = Decimal("25")
    min_size_raw: int = 0


@dataclass
class LPOrder:
    order_id: str
    side: str
    price_raw: int
    flip_price_raw: int
    size_raw: int
    remaining_raw: int
    status: str
    eligible_block: int
    placed_block: int
    queue_ahead_ids: list[int] = field(default_factory=list)
    queue_ahead_raw: int = 0
    origin_price_raw: Optional[int] = None
    created_transaction_hash: Optional[str] = None
    paired_order_id: Optional[str] = None
    reserved_cash: Decimal = ZERO
    reserved_inventory: Decimal = ZERO
    placement_sequence: int = 0
    simulated_ahead_ids: list[str] = field(default_factory=list)


@dataclass
class LPState:
    cash: Decimal
    inventory: Decimal
    orders: dict[str, LPOrder] = field(default_factory=dict)
    next_order_number: int = 1
    reserved_cash: Decimal = ZERO
    reserved_inventory: Decimal = ZERO
    placements: int = 0
    fills: int = 0
    filled_size: Decimal = ZERO
    turnover: Decimal = ZERO
    maker_fees: Decimal = ZERO
    action_fees: Decimal = ZERO
    earned_flip_spread: Decimal = ZERO
    pending_by_block: dict[int, list[str]] = field(default_factory=lambda: defaultdict(list))
    active_by_price: dict[tuple[str, int], list[str]] = field(default_factory=lambda: defaultdict(list))
    active_prices: dict[str, list[int]] = field(default_factory=lambda: {"buy": [], "sell": []})
    queue_followers: dict[int, set[str]] = field(default_factory=lambda: defaultdict(set))
    indexed_order_ids: set[str] = field(default_factory=set)


class BacktestCancelled(RuntimeError):
    """Stop a replay after an explicit job cancellation."""


def _order_sort_key(order: LPOrder) -> tuple[int, str]:
    return (order.placement_sequence, order.order_id)


def _index_order(state: LPState, order: LPOrder) -> None:
    if order.order_id in state.indexed_order_ids:
        return
    if order.placement_sequence <= 0:
        try:
            order.placement_sequence = int(order.order_id.rsplit("-", 1)[-1])
        except ValueError:
            order.placement_sequence = len(state.indexed_order_ids) + 1
    state.indexed_order_ids.add(order.order_id)
    if order.status == "pending":
        state.pending_by_block[order.eligible_block].append(order.order_id)
    elif order.status == "active":
        key = (order.side, order.price_raw)
        if not state.active_by_price[key]:
            insort(state.active_prices[order.side], order.price_raw)
        state.active_by_price[key].append(order.order_id)
        for historical_id in order.queue_ahead_ids:
            state.queue_followers[historical_id].add(order.order_id)


def _ensure_indexes(state: LPState) -> None:
    if len(state.indexed_order_ids) == len(state.orders):
        return
    for order in state.orders.values():
        if order.order_id not in state.indexed_order_ids:
            _index_order(state, order)


def _activate_indexed_order(state: LPState, order: LPOrder) -> None:
    key = (order.side, order.price_raw)
    if not state.active_by_price[key]:
        insort(state.active_prices[order.side], order.price_raw)
    state.active_by_price[key].append(order.order_id)
    for historical_id in order.queue_ahead_ids:
        state.queue_followers[historical_id].add(order.order_id)


def _live_order_payloads(
    state: LPState,
    price_precision: int,
    size_precision: int,
    order_ids: Optional[set[str]] = None,
) -> list[dict[str, Any]]:
    values = state.orders.values() if order_ids is None else (
        state.orders[item] for item in order_ids if item in state.orders
    )
    return [
        _order_payload(item, price_precision, size_precision)
        for item in sorted(values, key=_order_sort_key)
        if order_ids is not None or item.status in {"pending", "active"}
    ]


def ceil_div(x: int, y: int) -> int:
    return x // y if x % y == 0 else x // y + 1


def normalize_bid_size(price: int, size_precision: int, bid_size: int) -> int:
    if ceil_div(price * bid_size, size_precision) > (price * bid_size) // size_precision:
        return bid_size - ceil_div(size_precision, price)
    return bid_size


def _grid(
    fee_pps: int,
    start_price: int,
    end_price: int,
    best_ask_price: int,
    tick_size: int,
) -> tuple[list[LPPosition], list[LPPosition]]:
    if start_price % tick_size:
        start_price = start_price - start_price % tick_size + tick_size
    bids: list[LPPosition] = []
    asks: list[LPPosition] = []
    current = start_price
    while current < min(best_ask_price, end_price):
        next_price = current * (FEE_DENOMINATOR + fee_pps) // FEE_DENOMINATOR
        next_price -= next_price % tick_size
        if next_price == current:
            next_price += tick_size
        flip_price = next_price * (FEE_DENOMINATOR + fee_pps) // FEE_DENOMINATOR
        flip_price -= flip_price % tick_size
        if flip_price == next_price:
            flip_price += tick_size
        bids.append(LPPosition("buy", current, 0, flip_price))
        current = next_price
    while current < end_price:
        flip_price = (
            current * (FEE_DENOMINATOR - fee_pps) * (FEE_DENOMINATOR - fee_pps)
        ) // (FEE_DENOMINATOR * FEE_DENOMINATOR)
        if flip_price == current:
            flip_price -= tick_size
        flip_price -= flip_price % tick_size
        asks.append(LPPosition("sell", current, 0, flip_price))
        next_price = current * (FEE_DENOMINATOR + fee_pps) // FEE_DENOMINATOR
        next_price -= next_price % tick_size
        if next_price == current:
            next_price += tick_size
        current = next_price
    return bids, asks


def generate_distribution(
    *,
    shape: str,
    fee_pps: int,
    start_price: int,
    end_price: int,
    best_ask_price: int,
    price_precision: int,
    size_precision: int,
    quote_decimals: int,
    base_decimals: int,
    tick_size: int,
    min_size: int,
    quote_liquidity: int,
    base_liquidity: Optional[int] = None,
) -> LPDistribution:
    """Build the SDK 0.0.95 position arrays with exact integer math."""
    if shape not in {"spot", "curve", "bid_ask"}:
        raise ValueError("shape must be spot, curve, or bid_ask")
    bids, asks = _grid(fee_pps, start_price, end_price, best_ask_price, tick_size)
    if not bids or not asks:
        raise ValueError("the selected range must contain at least one bid and one ask")
    min_error = False
    output_bids: list[LPPosition] = []
    output_asks: list[LPPosition] = []
    if shape == "spot" and base_liquidity is not None:
        for item in bids:
            raw = quote_liquidity * size_precision * price_precision // (
                len(bids) * item.price * 10**quote_decimals
            )
            size = normalize_bid_size(item.price, size_precision, raw)
            min_error |= raw < min_size
            output_bids.append(LPPosition(item.side, item.price, size, item.flip_price))
        ask_size = base_liquidity * size_precision // (len(asks) * 10**base_decimals)
        min_error |= ask_size < min_size
        output_asks = [
            LPPosition(item.side, item.price, ask_size, item.flip_price) for item in asks
        ]
        # Spot 0.0.95 returns asks in descending price order.
        output_asks.sort(key=lambda item: item.price, reverse=True)
        total_base = base_liquidity
    else:
        bid_unit = 2 * quote_liquidity // (len(bids) * (len(bids) + 1))
        ask_unit = 2 * quote_liquidity // (len(asks) * (len(asks) + 1))
        total_base = 0
        for index, item in enumerate(bids):
            multiplier = index + 1 if shape == "curve" else len(bids) - index
            quote = bid_unit * multiplier
            raw = quote * price_precision * size_precision // (
                10**quote_decimals * item.price
            )
            size = normalize_bid_size(item.price, size_precision, raw)
            min_error |= raw < min_size
            output_bids.append(LPPosition(item.side, item.price, size, item.flip_price))
        for index, item in enumerate(asks):
            multiplier = len(asks) - index if shape == "curve" else index + 1
            quote = ask_unit * multiplier
            size = quote * price_precision * size_precision // (
                10**quote_decimals * item.price
            )
            total_base += quote * 10**base_decimals * price_precision // (
                10**quote_decimals * item.price
            )
            min_error |= size < min_size
            output_asks.append(LPPosition(item.side, item.price, size, item.flip_price))
    output_bids.sort(key=lambda item: item.price, reverse=True)
    if shape != "spot":
        output_asks.sort(key=lambda item: item.price)
    return LPDistribution(
        bids=tuple(output_bids),
        asks=tuple(output_asks),
        quote_liquidity=quote_liquidity,
        base_liquidity=total_base,
        min_size_error=min_error,
    )


def _reserve(state: LPState, order: LPOrder, price_precision: int, size_precision: int) -> None:
    size = Decimal(order.remaining_raw) / Decimal(size_precision)
    if order.side == "buy":
        order.reserved_cash = size * Decimal(order.price_raw) / Decimal(price_precision)
        if state.cash - state.reserved_cash < order.reserved_cash:
            raise RuntimeError("the USDC deposit is too small for the LP bids")
        state.reserved_cash += order.reserved_cash
    else:
        order.reserved_inventory = size
        if state.inventory - state.reserved_inventory < size:
            raise RuntimeError("the MON deposit is too small for the LP asks")
        state.reserved_inventory += size


def assert_lp_invariants(state: LPState) -> None:
    live = [item for item in state.orders.values() if item.status in {"pending", "active"}]
    if state.reserved_cash != sum((item.reserved_cash for item in live), ZERO):
        raise RuntimeError("LP cash reservations do not match live orders")
    if state.reserved_inventory != sum((item.reserved_inventory for item in live), ZERO):
        raise RuntimeError("LP inventory reservations do not match live orders")
    if state.reserved_cash < ZERO or state.reserved_inventory < ZERO:
        raise RuntimeError("an LP reservation is negative")
    if state.cash < state.reserved_cash:
        raise RuntimeError("live LP bids exceed the available USDC deposit")
    if state.inventory < state.reserved_inventory:
        raise RuntimeError("live LP asks exceed the available MON deposit")
    for order in live:
        if order.remaining_raw <= 0 or order.queue_ahead_raw < 0:
            raise RuntimeError("a live LP order has an invalid size or queue position")


def assert_flip_minimum_semantics(state: LPState, min_size_raw: int) -> None:
    """Allow small internal flips, but never small initial user orders.

    Kuru checks ``minSize`` when the user provides the initial flip order.
    Its internal ``_handleFlipOrderUpdate`` function creates or adds to the
    paired order without that check. Thus, a small partial fill can make one
    contract-valid internal order below ``minSize``. Later fills add to the
    same paired order.
    """
    if min_size_raw <= 0:
        return
    for order in state.orders.values():
        if order.size_raw < min_size_raw and order.origin_price_raw is None:
            raise RuntimeError("an initial LP order is below the market minimum size")


def charge_initial_action_fee(state: LPState, config: LPConfig) -> None:
    """Charge one fixed cost for the batch provision transaction.

    Contract-created flip orders do not require a new user action.
    """
    if config.fixed_action_fee < ZERO:
        raise ValueError("fixed action fee cannot be negative")
    if state.cash < config.fixed_action_fee:
        raise RuntimeError("the USDC deposit cannot pay the initial action cost")
    state.cash -= config.fixed_action_fee
    state.action_fees += config.fixed_action_fee


def initial_orders(
    distribution: LPDistribution,
    state: LPState,
    checkpoint_block: int,
    inclusion_delay_blocks: int,
    price_precision: int,
    size_precision: int,
) -> list[dict[str, Any]]:
    placements: list[dict[str, Any]] = []
    for position in (*distribution.bids, *distribution.asks):
        placement_sequence = state.next_order_number
        order_id = f"lp-{placement_sequence}"
        state.next_order_number += 1
        order = LPOrder(
            order_id=order_id,
            side=position.side,
            price_raw=position.price,
            flip_price_raw=position.flip_price,
            size_raw=position.liquidity,
            remaining_raw=position.liquidity,
            status="pending",
            eligible_block=checkpoint_block + inclusion_delay_blocks,
            placed_block=checkpoint_block,
            placement_sequence=placement_sequence,
        )
        _reserve(state, order, price_precision, size_precision)
        state.orders[order_id] = order
        _index_order(state, order)
        state.placements += 1
        placements.append(_order_payload(order, price_precision, size_precision))
    return placements


def activate_orders(
    book: l3.L3Book,
    state: LPState,
    block: int,
    size_precision: int,
    counters: Optional[dict[str, int]] = None,
) -> list[dict[str, Any]]:
    _ensure_indexes(state)
    activations = []
    eligible_ids: list[str] = []
    for eligible_block in sorted(key for key in state.pending_by_block if key <= block):
        eligible_ids.extend(state.pending_by_block.pop(eligible_block))
    eligible_ids.sort(key=lambda item: _order_sort_key(state.orders[item]))
    for order_id in eligible_ids:
        order = state.orders[order_id]
        if order.status != "pending":
            continue
        order.status = "active"
        order.queue_ahead_ids = list(book.queue(order.side == "buy", order.price_raw))
        earlier_simulated = [
            item
            for item in state.active_by_price[(order.side, order.price_raw)]
            if state.orders[item].status == "active"
        ]
        order.simulated_ahead_ids = earlier_simulated
        order.queue_ahead_raw = (
            sum(book.orders[item].size for item in order.queue_ahead_ids)
            + sum(state.orders[item].remaining_raw for item in earlier_simulated)
        )
        _activate_indexed_order(state, order)
        if counters is not None:
            counters["queue_entries_scanned"] += len(order.queue_ahead_ids) + len(earlier_simulated)
            counters["simulated_order_activations"] += 1
        activations.append({
            "order_id": order.order_id,
            "side": order.side,
            "activation_block": block,
            "queue_ahead_size": order.queue_ahead_raw / size_precision,
        })
    return activations


def update_queue_ahead(
    event: l3.ReplayEvent,
    state: LPState,
    counters: Optional[dict[str, int]] = None,
) -> set[str]:
    if event.name in {"OrderCreated", "FlipOrderCreated", "FlippedOrderCreated"}:
        return set()
    _ensure_indexes(state)
    order_id = int(event.args.get("order_id", 0))
    followers = tuple(state.queue_followers.get(order_id, ()))
    changed: set[str] = set()
    if counters is not None:
        counters["queue_entries_scanned"] += len(followers)
    for simulated_id in followers:
        order = state.orders[simulated_id]
        if order.status != "active" or order_id not in order.queue_ahead_ids:
            continue
        if event.name == "OrderCanceled":
            order.queue_ahead_raw -= int(event.args["size"])
            order.queue_ahead_ids.remove(order_id)
        elif event.name == "Trade":
            order.queue_ahead_raw -= int(event.args["filled_size"])
            if int(event.args["updated_size"]) == 0:
                order.queue_ahead_ids.remove(order_id)
        if order_id not in order.queue_ahead_ids:
            state.queue_followers[order_id].discard(simulated_id)
        if order.queue_ahead_raw < 0:
            raise RuntimeError("LP queue-ahead size became negative")
        changed.add(simulated_id)
    return changed


def _update_simulated_followers(state: LPState, filled: LPOrder, fill_raw: int) -> set[str]:
    changed: set[str] = set()
    key = (filled.side, filled.price_raw)
    for candidate_id in state.active_by_price.get(key, []):
        if candidate_id == filled.order_id:
            continue
        candidate = state.orders[candidate_id]
        if candidate.status != "active" or filled.order_id not in candidate.simulated_ahead_ids:
            continue
        candidate.queue_ahead_raw -= fill_raw
        if filled.remaining_raw == 0:
            candidate.simulated_ahead_ids.remove(filled.order_id)
        if candidate.queue_ahead_raw < 0:
            raise RuntimeError("LP simulated queue-ahead size became negative")
        changed.add(candidate_id)
    return changed


def _increase_simulated_followers(state: LPState, order: LPOrder, added_raw: int) -> None:
    """Add merged active size to later simulated orders at the same FIFO level."""
    if order.status != "active":
        return
    for candidate_id in state.active_by_price.get((order.side, order.price_raw), []):
        if candidate_id == order.order_id:
            continue
        candidate = state.orders[candidate_id]
        if candidate.status == "active" and order.order_id in candidate.simulated_ahead_ids:
            candidate.queue_ahead_raw += added_raw


def _release_fill_reserve(
    state: LPState, order: LPOrder, fill_raw: int, price_precision: int, size_precision: int
) -> None:
    size = Decimal(fill_raw) / Decimal(size_precision)
    if order.side == "buy":
        released = size * Decimal(order.price_raw) / Decimal(price_precision)
        order.reserved_cash -= released
        state.reserved_cash -= released
    else:
        order.reserved_inventory -= size
        state.reserved_inventory -= size


def _place_or_merge_flip(
    state: LPState,
    filled: LPOrder,
    fill_raw: int,
    event: l3.ReplayEvent,
    config: LPConfig,
    price_precision: int,
    size_precision: int,
) -> dict[str, Any]:
    side = "sell" if filled.side == "buy" else "buy"
    flip_size_raw = (
        fill_raw
        if filled.side == "buy"
        else fill_raw * filled.price_raw // filled.flip_price_raw
    )
    existing = state.orders.get(filled.paired_order_id or "")
    if existing is not None and existing.status not in {"pending", "active"}:
        existing = None
    if existing is not None:
        existing.size_raw += flip_size_raw
        existing.remaining_raw += flip_size_raw
        added = LPOrder("", side, existing.price_raw, existing.flip_price_raw, flip_size_raw, flip_size_raw,
                        "pending", event.block_number, event.block_number)
        _reserve(state, added, price_precision, size_precision)
        existing.reserved_cash += added.reserved_cash
        existing.reserved_inventory += added.reserved_inventory
        _increase_simulated_followers(state, existing, flip_size_raw)
        if filled.remaining_raw == 0:
            existing.paired_order_id = None
            filled.paired_order_id = None
        payload = _order_payload(existing, price_precision, size_precision)
        payload.update({
            "kind": "flip_update",
            "added_size_raw": flip_size_raw,
            "contract_internal_flip": True,
            "below_market_minimum": bool(config.min_size_raw and existing.size_raw < config.min_size_raw),
        })
        assert_flip_minimum_semantics(state, config.min_size_raw)
        return payload
    placement_sequence = state.next_order_number
    order_id = f"lp-{placement_sequence}"
    state.next_order_number += 1
    order = LPOrder(
        order_id=order_id,
        side=side,
        price_raw=filled.flip_price_raw,
        flip_price_raw=filled.price_raw,
        size_raw=flip_size_raw,
        remaining_raw=flip_size_raw,
        status="pending",
        # A contract-created flip is added during the fill transaction. It can
        # trade after that transaction without a second external inclusion delay.
        eligible_block=event.block_number,
        placed_block=event.block_number,
        origin_price_raw=filled.price_raw,
        created_transaction_hash=event.transaction_hash,
        paired_order_id=filled.order_id if filled.remaining_raw else None,
        placement_sequence=placement_sequence,
    )
    if filled.remaining_raw:
        filled.paired_order_id = order_id
    _reserve(state, order, price_precision, size_precision)
    state.orders[order_id] = order
    _index_order(state, order)
    state.placements += 1
    payload = _order_payload(order, price_precision, size_precision)
    payload.update({
        "kind": "flip_create",
        "contract_internal_flip": True,
        "below_market_minimum": bool(config.min_size_raw and order.size_raw < config.min_size_raw),
    })
    assert_flip_minimum_semantics(state, config.min_size_raw)
    return payload


def apply_fills(
    event: l3.ReplayEvent,
    state: LPState,
    config: LPConfig,
    price_precision: int,
    size_precision: int,
    budget: Optional[mm.TransactionFillBudget],
    reference_midpoint_raw: Optional[int],
    counters: Optional[dict[str, int]] = None,
    changed_order_ids: Optional[set[str]] = None,
    stage_seconds: Optional[dict[str, float]] = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if event.name != "Trade":
        return [], []
    remaining_event = int(event.args["filled_size"])
    fills: list[dict[str, Any]] = []
    flips: list[dict[str, Any]] = []
    _ensure_indexes(state)
    taker_is_buy = bool(event.args["is_buy"])
    side = "sell" if taker_is_buy else "buy"
    prices = state.active_prices[side]
    selected_prices = prices if side == "sell" else list(reversed(prices))
    candidates = [
        state.orders[order_id]
        for price_raw in selected_prices
        for order_id in state.active_by_price[(side, price_raw)]
    ]
    if counters is not None:
        counters["sorts_performed"] += 0
        counters["simulated_orders_scanned"] += len(candidates)
        counters["price_levels_touched"] += len(selected_prices)
    for order in candidates:
        if remaining_event <= 0 or order.status != "active" or order.queue_ahead_raw:
            continue
        if event.block_number < order.eligible_block or order.created_transaction_hash == event.transaction_hash:
            continue
        proxy = mm.StrategyOrder(order.order_id, order.side, order.price_raw, order.size_raw,
                                 order.remaining_raw, order.status, order.eligible_block,
                                 order.placed_block, [], 0, order.placed_block, -1)
        if not mm.cross_reaches_order(event, proxy, price_precision):
            continue
        fill_raw = min(order.remaining_raw, remaining_event)
        if budget is not None:
            fill_raw = min(fill_raw, budget.remaining_raw)
        if fill_raw <= 0:
            continue
        accounting_started = time.perf_counter()
        size = Decimal(fill_raw) / Decimal(size_precision)
        price = Decimal(order.price_raw) / Decimal(price_precision)
        cash_delta, inventory_delta, fee, notional = mm.maker_fill_accounting(
            order.side, size, price, config.maker_fee_bps
        )
        _release_fill_reserve(state, order, fill_raw, price_precision, size_precision)
        state.cash += cash_delta
        state.inventory += inventory_delta
        state.fills += 1
        state.filled_size += size
        state.turnover += notional
        state.maker_fees += fee
        order.remaining_raw -= fill_raw
        follower_changes = _update_simulated_followers(state, order, fill_raw)
        if changed_order_ids is not None:
            changed_order_ids.add(order.order_id)
            changed_order_ids.update(follower_changes)
        remaining_event -= fill_raw
        if budget is not None:
            budget.remaining_raw -= fill_raw
        if order.origin_price_raw is not None:
            state.earned_flip_spread += (
                Decimal(abs(order.price_raw - order.origin_price_raw))
                / Decimal(price_precision) * size
            )
        fill = {
            "strategy_order_id": order.order_id,
            "side": order.side,
            "price_raw": order.price_raw,
            "price": mm.decimal_to_number(price),
            "size": mm.decimal_to_number(size),
            "notional": mm.decimal_to_number(notional),
            "maker_fee": mm.decimal_to_number(fee),
            "simulated_fill_size_raw": fill_raw,
            "transaction_hash": event.transaction_hash,
            "historical_event_block_number": event.block_number,
            "historical_event_transaction_index": event.transaction_index,
            "historical_event_log_index": event.log_index,
            "historical_trade_order_id": int(event.args["order_id"]),
            "historical_taker_is_buy": bool(event.args["is_buy"]),
            "historical_event_fill_size_raw": int(event.args["filled_size"]),
            "reference_midpoint_raw": reference_midpoint_raw,
            "is_flip_fill": order.origin_price_raw is not None,
        }
        fills.append(fill)
        if order.remaining_raw == 0:
            order.status = "filled"
        if stage_seconds is not None:
            stage_seconds["capital_accounting"] += time.perf_counter() - accounting_started
        flip_started = time.perf_counter()
        flip_payload = _place_or_merge_flip(
            state, order, fill_raw, event, config, price_precision, size_precision
        )
        flips.append(flip_payload)
        if stage_seconds is not None:
            stage_seconds["flip_order_creation"] += time.perf_counter() - flip_started
        if changed_order_ids is not None:
            changed_order_ids.add(str(flip_payload["order_id"]))
        if counters is not None:
            counters["simulated_fills"] += 1
            counters["flip_orders_created_or_updated"] += 1
    return fills, flips


def _order_payload(order: LPOrder, price_precision: int, size_precision: int) -> dict[str, Any]:
    return {
        "order_id": order.order_id,
        "side": order.side,
        "status": order.status,
        "price_raw": order.price_raw,
        "price": order.price_raw / price_precision,
        "flip_price_raw": order.flip_price_raw,
        "flip_price": order.flip_price_raw / price_precision,
        "size_raw": order.size_raw,
        "size": order.size_raw / size_precision,
        "remaining_size_raw": order.remaining_raw,
        "remaining_size": order.remaining_raw / size_precision,
        "queue_ahead_order_ids": list(order.queue_ahead_ids),
        "queue_ahead_size_raw": order.queue_ahead_raw,
        "queue_ahead_size": order.queue_ahead_raw / size_precision,
        "eligible_block": order.eligible_block,
        "reserved_cash": mm.decimal_to_number(order.reserved_cash),
        "reserved_inventory": mm.decimal_to_number(order.reserved_inventory),
    }


def _timeline_record(
    *, sequence: int, record_type: str, block: int, tx_index: int, log_index: int,
    timestamp: int, book: l3.L3Book, state: LPState, initial_value: Decimal,
    initial_cash: Decimal, initial_inventory: Decimal, price_precision: int,
    size_precision: int, fills: Optional[list[dict[str, Any]]] = None,
    placements: Optional[list[dict[str, Any]]] = None,
    activations: Optional[list[dict[str, Any]]] = None,
    market_event: Optional[dict[str, Any]] = None,
    full_order_snapshot: bool = False,
    changed_order_ids: Optional[set[str]] = None,
) -> dict[str, Any]:
    mark_raw = mm.midpoint_raw(book)
    mark = Decimal(mark_raw) / Decimal(price_precision) if mark_raw is not None else ZERO
    value = state.cash + state.inventory * mark
    hold_value = initial_cash + initial_inventory * mark
    from datetime import datetime, timezone
    timestamp_text = datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "schema_version": "1.2", "sequence": sequence, "record_type": record_type,
        "block_number": block, "transaction_index": tx_index, "log_index": log_index,
        "timestamp": timestamp_text, "market_event": market_event,
        "market": mm.market_payload(book, price_precision),
        "order_snapshot_complete": full_order_snapshot,
        "active_simulated_orders": (
            _live_order_payloads(state, price_precision, size_precision)
            if full_order_snapshot else []
        ),
        "simulated_order_deltas": _live_order_payloads(
            state, price_precision, size_precision, changed_order_ids or set()
        ),
        "activations": activations or [], "fills": fills or [], "cancellations": [],
        "placements": placements or [], "cash": mm.decimal_to_number(state.cash),
        "inventory": mm.decimal_to_number(state.inventory),
        "reserved_cash": mm.decimal_to_number(state.reserved_cash),
        "reserved_inventory": mm.decimal_to_number(state.reserved_inventory),
        "available_cash": mm.decimal_to_number(state.cash - state.reserved_cash),
        "available_inventory": mm.decimal_to_number(state.inventory - state.reserved_inventory),
        "marked_portfolio_value": mm.decimal_to_number(value),
        "cumulative_pnl": mm.decimal_to_number(value - initial_value),
        "hold_baseline_pnl": mm.decimal_to_number(hold_value - initial_value),
        "cumulative_fees": mm.decimal_to_number(state.maker_fees + state.action_fees),
    }


def run_lp(
    initial_book: l3.L3Book, events: list[l3.ReplayEvent], start_block: int, end_block: int,
    config: LPConfig, distribution: LPDistribution, price_precision: int,
    size_precision: int, timestamps: dict[int, int], verified_input: dict[str, Any],
    timeline_stride: int = 1,
    *,
    audit_invariants: bool = True,
    progress_callback: Optional[Any] = None,
    should_cancel: Optional[Any] = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if timeline_stride < 1:
        raise ValueError("timeline stride must be at least one")
    wall_start = time.perf_counter()
    cpu_start = time.process_time()
    stage_seconds = {
        "reconstruction_event_application": 0.0,
        "strategy_order_activation": 0.0,
        "queue_position_updates": 0.0,
        "fill_matching_flip_and_accounting": 0.0,
        "capital_accounting": 0.0,
        "flip_order_creation": 0.0,
        "invariant_validation": 0.0,
        "metric_calculation": 0.0,
        "timeline_construction": 0.0,
    }
    counters: dict[str, int] = defaultdict(int)
    book = initial_book.copy()
    stage_start = time.perf_counter()
    book.check_invariants()
    stage_seconds["invariant_validation"] += time.perf_counter() - stage_start
    initial_mark_raw = mm.midpoint_raw(book)
    if initial_mark_raw is None:
        raise RuntimeError("the checkpoint book has an empty side")
    initial_cash = Decimal(config.quote_deposit_raw) / Decimal(10**6)
    initial_inventory = Decimal(config.base_deposit_raw) / Decimal(10**18)
    state = LPState(initial_cash, initial_inventory)
    initial_mark = Decimal(initial_mark_raw) / Decimal(price_precision)
    initial_value = initial_cash + initial_inventory * initial_mark
    charge_initial_action_fee(state, config)
    placements = initial_orders(distribution, state, start_block - 1,
                                config.inclusion_delay_blocks, price_precision, size_precision)
    assert_flip_minimum_semantics(state, config.min_size_raw)
    assert_lp_invariants(state)
    initial_record = _timeline_record(
        sequence=1, record_type="lp_provision", block=start_block - 1, tx_index=-1,
        log_index=-1, timestamp=timestamps[start_block - 1], book=book, state=state,
        initial_value=initial_value, initial_cash=initial_cash,
        initial_inventory=initial_inventory, price_precision=price_precision,
        size_precision=size_precision, placements=placements, full_order_snapshot=True,
    )
    timeline = [initial_record]
    counters["timeline_records_constructed"] = 1
    counters["timeline_records_retained"] = 1
    counters["simulated_orders_created"] = len(state.orders)
    full_record_count = 1
    utilization_sum = ZERO
    metric_record_count = 0
    high_water = Decimal(str(initial_record["marked_portfolio_value"]))
    maximum_drawdown = ZERO

    def capture_metrics() -> None:
        nonlocal utilization_sum, metric_record_count, high_water, maximum_drawdown
        mark_raw = mm.midpoint_raw(book)
        mark = Decimal(mark_raw) / Decimal(price_precision) if mark_raw is not None else ZERO
        midpoint_value = Decimal(str(mm.decimal_to_number(mark)))
        equity_value = Decimal(str(mm.decimal_to_number(state.cash + state.inventory * mark)))
        reserved_value = Decimal(str(mm.decimal_to_number(state.reserved_cash))) + (
            Decimal(str(mm.decimal_to_number(state.reserved_inventory))) * midpoint_value
        )
        utilization_sum += (
            min(Decimal(100), reserved_value / equity_value * 100)
            if equity_value else ZERO
        )
        metric_record_count += 1
        if equity_value > high_water:
            high_water = equity_value
        drawdown = high_water - equity_value
        if drawdown > maximum_drawdown:
            maximum_drawdown = drawdown

    capture_metrics()
    sequence = 1
    checks = 1
    budgets = mm.transaction_fill_budgets(events, config.max_transaction_participation_percent)
    def interval_events() -> Any:
        return (
            event for event in events
            if start_block <= event.block_number <= end_block
        )

    in_range = 0
    observed = 0
    interval_event_count = sum(1 for _ in interval_events())
    event_ordinal = 0
    dirty_order_ids: set[str] = set()
    progress_interval = max(1, interval_event_count // 200)
    last_progress_emit = time.perf_counter()
    # Verified source events are already in deterministic chain order. Group
    # that stream directly so a second block-to-events dictionary is not made.
    for block, block_events in groupby(interval_events(), key=lambda item: item.block_number):
        stage_start = time.perf_counter()
        activations = activate_orders(book, state, block, size_precision, counters)
        stage_seconds["strategy_order_activation"] += time.perf_counter() - stage_start
        dirty_order_ids.update(str(item["order_id"]) for item in activations)
        for index, event in enumerate(block_events):
            # A cancel marker can be on a Windows-mounted path. A file-system
            # stat for every event adds large I/O wait. The API also terminates
            # the isolated worker at once, so this bounded check is a fallback.
            if should_cancel is not None and event_ordinal % 256 == 0 and should_cancel():
                raise BacktestCancelled("backtest job was canceled")
            if should_cancel is not None and event_ordinal % 256 == 0:
                counters["cancellation_checks"] += 1
            event_ordinal += 1
            mid_before = mm.midpoint_raw(book)
            if mid_before is not None:
                observed += 1
                in_range += int(config.minimum_price_raw <= mid_before <= config.maximum_price_raw)
            stage_start = time.perf_counter()
            fills, flip_placements = apply_fills(
                event, state, config, price_precision, size_precision,
                budgets.get(event.transaction_hash), mid_before, counters, dirty_order_ids,
                stage_seconds,
            )
            stage_seconds["fill_matching_flip_and_accounting"] += time.perf_counter() - stage_start
            stage_start = time.perf_counter()
            dirty_order_ids.update(update_queue_ahead(event, state, counters))
            stage_seconds["queue_position_updates"] += time.perf_counter() - stage_start
            stage_start = time.perf_counter()
            l3.apply_event(book, event, price_precision)
            stage_seconds["reconstruction_event_application"] += time.perf_counter() - stage_start
            if audit_invariants:
                stage_start = time.perf_counter()
                book.check_invariants()
                assert_lp_invariants(state)
                stage_seconds["invariant_validation"] += time.perf_counter() - stage_start
                checks += 1
            stage_start = time.perf_counter()
            later_activations = activate_orders(book, state, block, size_precision, counters)
            stage_seconds["strategy_order_activation"] += time.perf_counter() - stage_start
            event_activations = (activations if index == 0 else []) + later_activations
            dirty_order_ids.update(str(item["order_id"]) for item in later_activations)
            sequence += 1
            stage_start = time.perf_counter()
            capture_metrics()
            stage_seconds["metric_calculation"] += time.perf_counter() - stage_start
            full_record_count += 1
            retain_market = (
                timeline_stride == 1
                or event_ordinal == interval_event_count
                or event_ordinal % timeline_stride == 0
                or bool(event_activations)
            )
            if retain_market:
                stage_start = time.perf_counter()
                market_record = _timeline_record(
                    sequence=sequence, record_type="market_event", block=block,
                    tx_index=event.transaction_index, log_index=event.log_index,
                    timestamp=timestamps[block], book=book, state=state, initial_value=initial_value,
                    initial_cash=initial_cash, initial_inventory=initial_inventory,
                    price_precision=price_precision, size_precision=size_precision,
                    activations=event_activations,
                    market_event={"name": event.name, "args": event.args},
                    changed_order_ids=set(dirty_order_ids),
                )
                timeline.append(market_record)
                dirty_order_ids.clear()
                counters["timeline_records_constructed"] += 1
                counters["timeline_records_retained"] += 1
                stage_seconds["timeline_construction"] += time.perf_counter() - stage_start
            if fills or flip_placements:
                sequence += 1
                stage_start = time.perf_counter()
                fill_record = _timeline_record(
                    sequence=sequence, record_type="lp_fill", block=block,
                    tx_index=event.transaction_index, log_index=event.log_index,
                    timestamp=timestamps[block], book=book, state=state, initial_value=initial_value,
                    initial_cash=initial_cash, initial_inventory=initial_inventory,
                    price_precision=price_precision, size_precision=size_precision,
                    fills=fills, placements=flip_placements,
                    market_event={"name": event.name, "args": event.args},
                    changed_order_ids=set(dirty_order_ids),
                )
                timeline.append(fill_record)
                dirty_order_ids.clear()
                counters["timeline_records_constructed"] += 1
                counters["timeline_records_retained"] += 1
                stage_seconds["timeline_construction"] += time.perf_counter() - stage_start
                stage_start = time.perf_counter()
                capture_metrics()
                stage_seconds["metric_calculation"] += time.perf_counter() - stage_start
                full_record_count += 1
            counters["historical_events_processed"] = event_ordinal
            if progress_callback is not None and (
                event_ordinal == interval_event_count
                or (
                    event_ordinal % progress_interval == 0
                    and time.perf_counter() - last_progress_emit >= 5.0
                )
            ):
                counters["progress_heartbeats"] += 1
                progress_callback({
                    "stage": "simulation",
                    "processed_events": event_ordinal,
                    "total_events": interval_event_count,
                    "active_simulated_orders": sum(
                        1 for item in state.orders.values() if item.status in {"pending", "active"}
                    ),
                    "counters": dict(counters),
                })
                last_progress_emit = time.perf_counter()
    if not audit_invariants:
        stage_start = time.perf_counter()
        book.check_invariants()
        assert_lp_invariants(state)
        stage_seconds["invariant_validation"] += time.perf_counter() - stage_start
        checks += 1
    final_mark_raw = mm.midpoint_raw(book)
    if final_mark_raw is None:
        raise RuntimeError("the final reconstructed book has an empty side")
    final_mark = Decimal(final_mark_raw) / Decimal(price_precision)
    final_value = state.cash + state.inventory * final_mark
    hold_value = initial_cash + initial_inventory * final_mark
    fill_records = [item for record in timeline for item in record["fills"]]
    impacts = mm.build_transaction_impacts(
        events, fill_records, size_precision, config.material_impact_threshold_percent
    )
    utilization = utilization_sum / Decimal(metric_record_count)
    counters["simulated_orders_created"] = len(state.orders)
    counters["active_simulated_orders_final"] = sum(
        1 for item in state.orders.values() if item.status in {"pending", "active"}
    )
    counters["invariant_checks_performed"] = checks
    summary = {
        "schema_version": "1.0", "status": "pass", "engine": "kuru_lp",
        "market": MARKET_ADDRESS,
        "interval": {"checkpoint_block": start_block - 1, "from_block": start_block, "to_block": end_block},
        "verified_input": verified_input,
        "causality": {
            "initial_inclusion_delay_blocks": config.inclusion_delay_blocks,
            "initial_orders_first_eligible_block": start_block - 1 + config.inclusion_delay_blocks,
            "flip_order_rule": "created after its causal fill and eligible after that transaction",
            "same_transaction_flip_fill_allowed": False,
        },
        "sdk_reference": {"package": "@kuru-labs/kuru-sdk", "version": "0.0.95",
                          "npm_git_head": "c6a1e9eced12d634fa879ec46c52baff8a50f3c3"},
        "config": {key: mm.decimal_to_number(value) if isinstance(value, Decimal) else value
                   for key, value in asdict(config).items()},
        "initial_position": {
            "sdk_fixture_parity": True,
            "order_count": len(distribution.bids) + len(distribution.asks),
            "bid_count": len(distribution.bids), "ask_count": len(distribution.asks),
            "required_quote_raw": distribution.quote_liquidity,
            "required_base_raw": distribution.base_liquidity,
            "required_mon_usdc_ratio": mm.decimal_to_number(
                (Decimal(distribution.base_liquidity) / Decimal(10**18)) /
                (Decimal(distribution.quote_liquidity) / Decimal(10**6))
            ),
            "positions": [_order_payload(LPOrder("fixture", item.side, item.price, item.flip_price,
                         item.liquidity, item.liquidity, "fixture", 0, 0), price_precision, size_precision)
                         for item in (*distribution.bids, *distribution.asks)],
        },
        "metrics": {
            "initial_cash": mm.decimal_to_number(initial_cash),
            "initial_inventory": mm.decimal_to_number(initial_inventory),
            "initial_midpoint": mm.decimal_to_number(initial_mark),
            "initial_portfolio_value": mm.decimal_to_number(initial_value),
            "final_cash": mm.decimal_to_number(state.cash),
            "final_inventory": mm.decimal_to_number(state.inventory),
            "final_midpoint": mm.decimal_to_number(final_mark),
            "final_portfolio_value": mm.decimal_to_number(final_value),
            "position_pnl": mm.decimal_to_number(final_value - initial_value),
            "hold_pnl": mm.decimal_to_number(hold_value - initial_value),
            "excess_pnl": mm.decimal_to_number(final_value - hold_value),
            "earned_flip_spread": mm.decimal_to_number(state.earned_flip_spread),
            "turnover": mm.decimal_to_number(state.turnover), "fill_count": state.fills,
            "filled_size": mm.decimal_to_number(state.filled_size),
            "ending_mon_percent": mm.decimal_to_number(state.inventory * final_mark / final_value * 100 if final_value else ZERO),
            "ending_usdc_percent": mm.decimal_to_number(state.cash / final_value * 100 if final_value else ZERO),
            "time_in_range_percent": mm.decimal_to_number(Decimal(in_range) / Decimal(observed) * 100 if observed else ZERO),
            "capital_utilization_percent": mm.decimal_to_number(utilization),
            "maximum_drawdown": mm.decimal_to_number(maximum_drawdown),
            "maker_fees": mm.decimal_to_number(state.maker_fees),
            "action_fees": mm.decimal_to_number(state.action_fees),
            "placement_count": state.placements, "cancellation_count": 0,
            "user_action_count": 1,
            "queue_invariant_checks": checks,
        },
        "hold_baseline": {"final_portfolio_value": mm.decimal_to_number(hold_value),
                           "hold_pnl": mm.decimal_to_number(hold_value - initial_value)},
        "counterfactual": {
            "assumption": "Simulated LP orders do not change historical taker behavior or later market events.",
            "maximum_transaction_participation_percent": mm.decimal_to_number(config.max_transaction_participation_percent) if config.max_transaction_participation_percent is not None else None,
            "material_impact_threshold_percent": mm.decimal_to_number(config.material_impact_threshold_percent),
            "max_transaction_impact_percent": max((item["simulated_to_historical_percent"] for item in impacts), default=0),
            "run_flagged": any(item["material_impact"] for item in impacts),
            "transaction_impacts": impacts,
        },
        "fills": fill_records,
        "timeline": {
            "record_count": len(timeline),
            "full_record_count": full_record_count,
            "sampling_stride": timeline_stride,
            "backend_is_complete": True,
        },
        "performance": {
            "audit_invariants": audit_invariants,
            "wall_seconds": time.perf_counter() - wall_start,
            "cpu_seconds": time.process_time() - cpu_start,
            "stage_seconds": stage_seconds,
            "counters": dict(counters),
            "peak_rss_bytes": (
                int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * 1024
                if resource is not None else None
            ),
        },
    }
    return summary, timeline


def fixture_distribution(path: Path, shape: str) -> LPDistribution:
    data = json.loads(path.read_text(encoding="utf-8"))
    value = data["shapes"][shape]
    convert = lambda item, side: LPPosition(side, int(item["price"]), int(item["liquidity"]), int(item["flipPrice"]))
    return LPDistribution(tuple(convert(item, "buy") for item in value["bids"]),
                          tuple(convert(item, "sell") for item in value["asks"]),
                          int(value["quoteLiquidity"]), int(value["baseLiquidity"]),
                          bool(value["minSizeError"]))


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validation-summary", type=Path, default=Path("validation_output/relatively_active/summary.json"))
    parser.add_argument("--fixture", type=Path, default=Path("sdk_reference/fixtures/concentrated_liquidity_v0_0_95.json"))
    parser.add_argument("--output-dir", type=Path, default=Path("lp_output/relatively_active/spot_max_10_percent"))
    parser.add_argument("--rpc-url", default=l3.DEFAULT_RPC_URL)
    args = parser.parse_args(argv)
    validation = mm.verify_validation_summary(args.validation_summary)
    events, source_timestamps = mm.load_verified_source_events(args.validation_summary, validation) or ([], {})
    interval = validation["interval"]
    start, end = int(interval["from_block"]), int(interval["to_block"])
    rpc = RpcClient(args.rpc_url, timeout=180)
    params = market_parameters(rpc, MARKET_ADDRESS)
    book, _, _ = l3.load_checkpoint(rpc, MARKET_ADDRESS, start - 1)
    blocks = {start - 1, *(event.block_number for event in events)}
    timestamps = {block: value for block, value in source_timestamps.items() if block in blocks}
    missing = blocks - timestamps.keys()
    if missing:
        timestamps.update(rpc.block_timestamps(missing))
    fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
    inputs = fixture["inputs"]
    config = LPConfig("spot", int(inputs["startPrice"]), int(inputs["endPrice"]),
                      int(inputs["minFeesPps"]), int(inputs["quoteLiquidity"]),
                      int(inputs["baseLiquidity"]))
    generated_distribution = generate_distribution(
        shape="spot",
        fee_pps=int(inputs["minFeesPps"]),
        start_price=int(inputs["startPrice"]),
        end_price=int(inputs["endPrice"]),
        best_ask_price=int(inputs["bestAskPrice"]),
        price_precision=int(inputs["pricePrecision"]),
        size_precision=int(inputs["sizePrecision"]),
        quote_decimals=int(inputs["quoteAssetDecimals"]),
        base_decimals=int(inputs["baseAssetDecimals"]),
        tick_size=int(inputs["tickSize"]),
        min_size=int(inputs["minSize"]),
        quote_liquidity=int(inputs["quoteLiquidity"]),
        base_liquidity=int(inputs["baseLiquidity"]),
    )
    distribution = fixture_distribution(args.fixture, "spot")
    if generated_distribution != distribution:
        raise RuntimeError("generated Spot position does not match the official SDK fixture")
    verified = {"event_source": validation["event_source"],
                "parity_status": validation["event_parity"]["status"],
                "event_count": validation["event_parity"]["rpc_event_count"],
                "maximum_price_level_error_raw": validation["validation"]["maximum_price_level_error_raw"],
                "maximum_aggregate_size_error_raw": validation["validation"]["maximum_aggregate_size_error_raw"]}
    summary, timeline = run_lp(book, events, start, end, config, distribution,
                               int(params["price_precision"]), int(params["size_precision"]),
                               timestamps, verified)
    manifest = mm.write_outputs(args.output_dir, summary, timeline)
    print(json.dumps({"metrics": summary["metrics"], "manifest": manifest}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
