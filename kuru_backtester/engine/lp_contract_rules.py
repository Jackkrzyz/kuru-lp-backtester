"""Integer rules checked against the historical Kuru OrderBook implementation.

This module contains LP-specific rules. Historical market reconstruction is
unchanged. Funding rounds up at both conversion steps. Withdrawable quote
rounds down at both steps. The deployed bytecode tests check these rules;
the public source has a different final funding conversion.
"""

from decimal import Decimal


def quote_raw(price: int, size: int, price_precision: int, size_precision: int,
              quote_decimals: int = 6, *, round_up: bool = False) -> int:
    numerator = price * size
    ticks = (numerator + size_precision - 1) // size_precision if round_up else numerator // size_precision
    token_numerator = ticks * 10**quote_decimals
    return ((token_numerator + price_precision - 1) // price_precision
            if round_up else token_numerator // price_precision)


def base_raw(size: int, size_precision: int, base_decimals: int = 18) -> int:
    return size * 10**base_decimals // size_precision


def token_amount(raw: int, decimals: int) -> Decimal:
    return Decimal(raw) / Decimal(10**decimals)


def validate_initial_order(side: str, price: int, flip_price: int, size: int,
                           tick_size: int, min_size: int, max_size: int) -> None:
    if not (0 < price < 2**32 - 1 and 0 < flip_price < 2**32 - 1):
        raise ValueError("initial flip-order prices exceed contract limits")
    if price % tick_size or flip_price % tick_size:
        raise ValueError("initial flip-order prices are not tick aligned")
    if not min_size <= size <= max_size:
        raise ValueError("initial flip-order size exceeds contract limits")
    if (side == "buy" and price >= flip_price) or (side == "sell" and price <= flip_price):
        raise ValueError("initial flip price is on the wrong side")
