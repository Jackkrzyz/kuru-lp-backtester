"""Contract-specific LP regressions, separate from archived MM assumptions."""

from decimal import Decimal

import pytest

import kuru_lp as lp
import l3_vertical_slice as l3
import lp_contract_rules as rules
from backtest_service import BacktestService, BacktestInputError


def config(**changes):
    return lp.LPConfig("spot",90,140,3000,10**7,10**19,contract_rules=True,
                       price_precision_raw=100, min_size_raw=1, **changes)


def initial(side="buy", price=100, flip=120, size=200, **changes):
    cfg = config(**changes)
    state = lp.LPState(Decimal(10),Decimal(10),contract_config=cfg)
    position = lp.LPPosition(side,price,size,flip)
    distribution = lp.LPDistribution((position,) if side=="buy" else (),
                                     (position,) if side=="sell" else (),0,0,False)
    lp.initial_orders(distribution,state,9,1,100,100)
    return cfg,state


def trade(side, size, price=100, index=1):
    return l3.ReplayEvent(10,index,index,"same-tx","Trade",{
        "filled_size":size,"updated_size":0,"price":price*10**16,
        "is_buy":side=="sell","order_id":99}, {})


def test_reference_uses_only_pre_interval_state():
    service=BacktestService()
    data=service.load_range("0x065c9d28e428a0db40191a54d33d5b7c71a9c394",109100000,109100200)
    bid,ask=data.book.best_prices()
    assert data.start_market["reference_price_raw"]==(bid+ask)//2==2707250


def test_historical_flip_growth_increases_queue_ahead():
    cfg,state=initial()
    book=l3.L3Book()
    book.append(99,"owner",100,100,True,0,0)
    lp.activate_orders(book,state,10,100)
    event=l3.ReplayEvent(10,1,1,"tx","FlipOrderUpdated",{"order_id":99,"size":150},{})
    lp.update_queue_ahead(event,state)
    assert state.orders["lp-1"].queue_ahead_raw==150
    event=l3.ReplayEvent(10,2,2,"tx","Trade",{"order_id":99,"filled_size":150,"updated_size":0},{})
    lp.update_queue_ahead(event,state)
    assert state.orders["lp-1"].queue_ahead_raw==0


def test_crossing_initial_order_is_skipped_and_capital_released():
    cfg,state=initial(price=120,flip=140)
    book=l3.L3Book()
    book.append(99,"owner",100,120,False,0,0)
    records=lp.activate_orders(book,state,10,100)
    assert records[0]["reason"]=="crosses_book"
    assert state.orders["lp-1"].status=="skipped"
    assert state.reserved_cash==0 and state.cash==10


def test_quote_rounding_matches_two_stage_contract_math():
    assert rules.quote_raw(2713900,2000000000001,10**8,10**10)==5427800
    assert rules.quote_raw(120,109,100,100,round_up=True)==1310000
    assert rules.quote_raw(120,109,100,100)==1300000


def test_internal_flips_can_fill_later_in_same_transaction():
    cfg,state=initial()
    book=l3.L3Book()
    lp.activate_orders(book,state,10,100)
    lp.apply_fills(trade("buy",100),state,cfg,100,100,None,110)
    lp.activate_orders(book,state,10,100)
    fills,_=lp.apply_fills(trade("sell",100,120,2),state,cfg,100,100,None,110)
    assert len(fills)==1
    assert state.orders["lp-1"].remaining_raw==220
    lp.assert_lp_invariants(state)


@pytest.mark.parametrize("start_side", ["buy", "sell"])
def test_spread_counts_completed_cycles_not_both_flip_legs(start_side):
    price, flip = (100, 120) if start_side == "buy" else (120, 100)
    cfg, state = initial(start_side, price, flip, size=100)
    book = l3.L3Book()
    lp.activate_orders(book, state, 10, 100)
    sizes = (100, 100, 120, 120) if start_side == "buy" else (100, 120, 120, 144)
    for index, size in enumerate(sizes):
        side = start_side if index % 2 == 0 else ("sell" if start_side == "buy" else "buy")
        execution_price = 100 if side == "buy" else 120
        lp.apply_fills(trade(side, size, execution_price, index), state, cfg, 100, 100, None, 110)
        lp.activate_orders(book, state, 10, 100)
        if index == 2:
            assert state.earned_flip_spread == Decimal("0.2")
    assert state.earned_flip_spread == Decimal("0.44")


@pytest.mark.parametrize("side,price,flip",[("buy",100,120),("sell",120,100)])
def test_maker_rebate_credits_contract_asset(side,price,flip):
    cfg,state=initial(side,price,flip,size=200,maker_fee_bps=Decimal(10))
    lp.activate_orders(l3.L3Book(),state,10,100)
    fills,_=lp.apply_fills(trade(side,100,price),state,cfg,100,100,None,110)
    assert fills[0]["maker_fee"]<0
    if side=="buy":
        assert state.cash-state.reserved_cash==Decimal("8.001")
    else:
        assert state.inventory-state.reserved_inventory==Decimal("8.001")


def test_removed_simulation_settings_cannot_change_engine_rules():
    service=BacktestService()
    data=service.load_interval("relatively_active")
    with pytest.raises(BacktestInputError) as error:
        service.normalize_request({"max_transaction_participation_percent":10},data)
    assert error.value.code=="unsupported_simulation_settings"


def test_backend_rejects_exact_grid_funding_shortage():
    from backtest_service import canonical_sdk_grid
    from golden_regression import base_request
    service = BacktestService()
    data = service.load_interval("relatively_active")
    request = {**base_request(), "interval": "relatively_active", "usdc_amount": "180"}
    _, sdk_input = service.normalize_request(request, data)
    request["order_grid"] = canonical_sdk_grid(sdk_input)
    with pytest.raises(BacktestInputError) as error:
        service.run(request, event_limit=1)
    assert error.value.code == "grid_deposit_shortage"
    assert int(error.value.details["usdc_shortage_raw"]) > 0


@pytest.mark.parametrize("change,code", [
    ({"implementation_code_sha256": "untested"}, "unsupported_contract_implementation"),
    ({"vault_bid_size": 1}, "unsupported_active_vault"),
])
def test_contract_rules_fail_closed_outside_verified_scope(monkeypatch, tmp_path, change, code):
    import json
    import backtest_service
    service = BacktestService()
    data = service.load_interval("relatively_active")
    payload = json.loads((backtest_service.ROOT / ".cache/lp_contract_rules.json").read_text())
    payload["checkpoints"]["109097027"].update(change)
    cache = tmp_path / ".cache"
    cache.mkdir()
    (cache / "lp_contract_rules.json").write_text(json.dumps(payload))
    monkeypatch.setattr(backtest_service, "ROOT", tmp_path)
    with pytest.raises(BacktestInputError) as error:
        service.contract_rules(data)
    assert error.value.code == code
