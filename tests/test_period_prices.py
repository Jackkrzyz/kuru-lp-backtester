"""Read-only historical price summary checks."""

from types import SimpleNamespace

from kuru_backtester.server.backtest_service import period_trade_prices


def test_period_prices_use_all_trades_and_1e18_units() -> None:
    events = [
        SimpleNamespace(name="OrderCreated", args={"price": 999999}),
        SimpleNamespace(name="Trade", args={"price": 27100000000000000}),
        SimpleNamespace(name="Trade", args={"price": 28000000000000000}),
        SimpleNamespace(name="Trade", args={"price": 26000000000000000}),
        SimpleNamespace(name="Trade", args={"price": 27400000000000000}),
    ]
    assert period_trade_prices(events) == {
        "ending_price": "0.0274", "highest_price": "0.028", "lowest_price": "0.026",
    }


def test_period_without_trades_has_no_invented_price() -> None:
    assert period_trade_prices([]) == {
        "ending_price": None, "highest_price": None, "lowest_price": None,
    }
