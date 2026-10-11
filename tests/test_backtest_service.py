"""Integration tests for the configuration-to-Kuru-LP-engine path."""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal

from kuru_backtester.server import backtest_service
import pytest


MARKET = "0x065c9d28e428a0db40191a54d33d5b7c71a9c394"


def test_semantic_timeline_hash_is_ordered_and_ignores_unknown_storage_fields() -> None:
    record = {
        "sequence": 1, "record_type": "lp_fill", "block_number": 10,
        "transaction_index": 2, "log_index": 3,
        "market": {"best_bid": 1, "best_ask": 2, "midpoint": 1.5},
        "market_event": {"name": "Trade", "args": {"filled_size": 4}},
        "placements": [], "activations": [], "cancellations": [],
        "fills": [{"strategy_order_id": "lp-1", "size": 4}],
        "order_snapshot_complete": False, "active_simulated_orders": [],
        "simulated_order_deltas": [], "cash": 9, "inventory": 4,
        "reserved_cash": 1, "reserved_inventory": 2,
        "marked_portfolio_value": 15, "cumulative_pnl": 1,
        "hold_baseline_pnl": 0, "cumulative_fees": 0,
    }
    same = {**record, "storage_only_field": "ignored"}
    changed = {**record, "fills": [{"strategy_order_id": "lp-1", "size": 3}]}
    assert backtest_service.semantic_timeline_sha256([record]) == backtest_service.semantic_timeline_sha256([same])
    assert backtest_service.semantic_timeline_sha256([record]) != backtest_service.semantic_timeline_sha256([changed])


def test_percentage_range_uses_outward_tick_rounding() -> None:
    minimum, maximum = backtest_service.percentage_price_bounds(2_711_150, "3", "3", 100)
    assert (minimum, maximum) == (2_629_800, 2_792_500)
    assert minimum % 100 == 0
    assert maximum % 100 == 0


def test_interval_reference_is_pre_interval_midpoint() -> None:
    metadata = backtest_service.BacktestService().interval_metadata()
    active = next(item for item in metadata["intervals"] if item["name"] == "relatively_active")
    assert active["from_block"] == 109097028
    assert active["reference_price_raw"] == 2_711_150
    assert active["reference_price"] == "0.0271115"


def test_percentage_request_normalizes_to_deterministic_absolute_bounds() -> None:
    service = backtest_service.BacktestService()
    request = {
        **request_a(),
        "range_mode": "percentage",
        "lower_range_percent": "3",
        "upper_range_percent": "3",
        "minimum_price": "0.026298",
        "maximum_price": "0.027925",
        "reference_price_raw": "2711150",
    }
    data = service.load_interval("relatively_active")
    first, sdk_input = service.normalize_request(request, data)
    second, _ = service.normalize_request(request, data)
    assert first == second
    assert first["reference_price_raw"] == "2711150"
    assert first["minimum_price_raw"] == "2629800"
    assert first["maximum_price_raw"] == "2792500"
    assert sdk_input["minimumPrice"] == "0.026298"
    assert sdk_input["maximumPrice"] == "0.027925"


def test_backend_grid_validation_matches_final_sdk_integer_orders() -> None:
    service = backtest_service.BacktestService()
    data = service.load_interval("relatively_active")
    for shape in ("spot", "curve", "bid_ask"):
        request = {
            **request_a(),
            "shape": shape,
            "amount_driver": "quote",
            "mon_amount": "0.000000000000000001",
            "usdc_amount": "0.000001",
        }
        _, sdk_input = service.normalize_request(request, data)
        grid = backtest_service.normalize_grid(backtest_service.canonical_sdk_grid(sdk_input))
        result = backtest_service.validate_sdk_grid(grid, data.parameters["min_size"])
        expected_invalid = sum(
            int(order["size_raw"]) < data.parameters["min_size"] for order in grid["orders"]
        )
        assert result["invalid_order_count"] == expected_invalid
        assert result["deployable"] is (expected_invalid == 0)
        assert result["deployable"] is False


def test_backend_returns_structured_grid_minimum_error() -> None:
    service = backtest_service.BacktestService()
    request = {
        **request_a(),
        "amount_driver": "quote",
        "mon_amount": "0.000000000000000001",
        "usdc_amount": "0.000001",
    }
    submitted = add_canonical_grid(service, request)
    with pytest.raises(backtest_service.BacktestInputError) as caught:
        service.run(submitted, event_limit=1)
    payload = caught.value.payload()
    assert payload["code"] == "grid_below_market_minimum"
    assert payload["details"]["grid_validation"]["invalid_order_count"] > 0
    assert payload["details"]["grid_validation"]["offending_order"]["side"] in {"buy", "sell"}


def request_a() -> dict:
    return {
        "engine": "kuru_lp",
        "market": MARKET,
        "market_symbol": "MON / USDC",
        "interval": "relatively_active",
        "shape": "spot",
        "minimum_price": "0.02710",
        "maximum_price": "0.02718",
        "fee_percent": "0.01",
        "mon_amount": "6000",
        "usdc_amount": "180",
        "amount_driver": "both",
    }


def request_b() -> dict:
    return {
        **request_a(),
        "shape": "bid_ask",
        "minimum_price": "0.025",
        "maximum_price": "0.030",
        "fee_percent": "1.00",
        "mon_amount": "16000",
        "usdc_amount": "450",
    }


def add_canonical_grid(service: backtest_service.BacktestService, request: dict) -> dict:
    complete = deepcopy(request)
    data = service.load_interval(complete["interval"])
    _, sdk_input = service.normalize_request(complete, data)
    complete["order_grid"] = backtest_service.canonical_sdk_grid(sdk_input)
    return complete


def add_generalized_grid(
    service: backtest_service.BacktestService, request: dict
) -> dict:
    complete = deepcopy(request)
    data = service.load_range(
        complete["market"], complete["from_block"], complete["to_block"]
    )
    _, sdk_input = service.normalize_request(complete, data)
    complete["order_grid"] = backtest_service.canonical_sdk_grid(sdk_input)
    return complete


def test_generalized_block_request_matches_golden_engine_behavior() -> None:
    service = backtest_service.BacktestService()
    legacy = request_a()
    generalized = {
        **legacy,
        "from_block": 109097028,
        "to_block": 109107027,
    }
    generalized.pop("interval")
    legacy_result = service.run(add_canonical_grid(service, legacy), event_limit=100)
    range_result = service.run(
        add_generalized_grid(service, generalized), event_limit=100
    )
    for key in (
        "starting_mon", "final_mon", "starting_usdc", "final_usdc", "fills",
        "turnover", "gross_pnl", "net_pnl", "maximum_drawdown",
        "earned_flip_spread",
    ):
        assert range_result[key] == legacy_result[key]
    assert range_result["generated_initial_bid_count"] == legacy_result["generated_initial_bid_count"]
    assert range_result["generated_initial_ask_count"] == legacy_result["generated_initial_ask_count"]
    assert range_result["normalized_configuration"]["from_block"] == 109097028
    assert range_result["normalized_configuration"]["to_block"] == 109107027


def test_two_different_submitted_configurations_run_the_real_engine() -> None:
    service = backtest_service.BacktestService()
    submitted_a = add_canonical_grid(service, request_a())
    submitted_b = add_canonical_grid(service, request_b())

    result_a = service.run(submitted_a)
    result_b = service.run(submitted_b)
    repeat_a = service.run(submitted_a)

    assert result_a["normalized_configuration"]["shape"] == submitted_a["shape"]
    assert result_a["normalized_configuration"]["fee_percent"] == submitted_a["fee_percent"]
    assert result_a["normalized_configuration"]["mon_amount"] == submitted_a["mon_amount"]
    assert result_a["normalized_configuration"]["usdc_amount"] == submitted_a["usdc_amount"]
    assert result_a["normalized_configuration"] != result_b["normalized_configuration"]
    assert result_a["initial_order_grid"]["orders"] != result_b["initial_order_grid"]["orders"]
    assert result_a["run_id"] != result_b["run_id"]
    assert result_a["summary_sha256"] != result_b["summary_sha256"]
    assert result_a["timeline_sha256"] != result_b["timeline_sha256"]
    assert result_a["run_id"] == repeat_a["run_id"]
    assert result_a["summary_sha256"] == repeat_a["summary_sha256"]
    assert result_a["timeline_sha256"] == repeat_a["timeline_sha256"]
    assert result_a["compact_timeline"] == repeat_a["compact_timeline"]


def test_fee_deposit_and_shape_each_change_the_sdk_grid() -> None:
    service = backtest_service.BacktestService()

    base = request_a()
    base_grid = add_canonical_grid(service, base)["order_grid"]

    fee_base = {**base, "minimum_price": "0.026", "maximum_price": "0.029"}
    fee_base_grid = add_canonical_grid(service, fee_base)["order_grid"]
    fee = {**fee_base, "fee_percent": "1.00"}
    fee_grid = add_canonical_grid(service, fee)["order_grid"]
    assert [item["flip_price_raw"] for item in fee_base_grid["orders"]] != [
        item["flip_price_raw"] for item in fee_grid["orders"]
    ]

    deposit = {**base, "mon_amount": "12000", "usdc_amount": "360"}
    deposit_grid = add_canonical_grid(service, deposit)["order_grid"]
    assert [item["size_raw"] for item in base_grid["orders"]] != [
        item["size_raw"] for item in deposit_grid["orders"]
    ]

    curve = {**base, "shape": "curve"}
    curve_grid = add_canonical_grid(service, curve)["order_grid"]
    base_sizes = [Decimal(item["size_raw"]) for item in base_grid["orders"]]
    curve_sizes = [Decimal(item["size_raw"]) for item in curve_grid["orders"]]
    assert base_sizes != curve_sizes
