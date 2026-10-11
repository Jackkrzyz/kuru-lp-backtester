"""LP parity checks against the unmodified published OrderBook bytecode."""

import hashlib
import json
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest
from eth_abi import encode, decode
from eth_utils import keccak

from kuru_backtester.engine import kuru_lp as lp
from kuru_backtester.engine import l3_vertical_slice as l3
from kuru_backtester.server.backtest_service import resolve_node

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def support():
    pytest.importorskip("eth_tester")
    import eth.vm.forks.spurious_dragon.computation as computation
    result = subprocess.run([resolve_node(), "sdk_reference/compile_contract_test_support.js"], cwd=ROOT,
                            check=True, capture_output=True, text=True)
    # This Monad artifact exceeds Ethereum's 24 KiB contract-size limit.
    # Permit deployment in the test VM; execute every opcode unchanged.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(computation, "EIP170_CODE_SIZE_LIMIT", 128 * 1024)
        yield json.loads(result.stdout)


class ContractMarket:
    def __init__(self, support, rebate=0, parameters=None):
        from eth_tester import EthereumTester, PyEVMBackend
        self.evm = EthereumTester(PyEVMBackend())
        self.maker, self.taker = self.evm.get_accounts()[:2]
        self.base, self.quote = self.evm.get_accounts()[8:10]
        self.margin = self.deploy("0x" + support["evm"]["bytecode"]["object"])
        artifact = json.loads((ROOT / "sdk_reference/node_modules/@kuru-labs/kuru-sdk/abi/OrderBook.json").read_text())
        # Prefer the implementation actually used by our historical market.
        rules_path = ROOT / ".cache/lp_contract_rules.json"
        if rules_path.is_file():
            row = json.loads(rules_path.read_text())["checkpoints"]["109097027"]
            code_path = ROOT / ".cache/lp_contract_implementations" / f"{row.get('implementation_code_sha256', '')}.json"
        else:
            code_path = ROOT / ".cache/no_historical_implementation"
        if code_path.is_file():
            runtime = json.loads(code_path.read_text())["code"][2:]
            assert hashlib.sha256(bytes.fromhex(runtime)).hexdigest() == row["implementation_code_sha256"]
            length = len(runtime)//2
            creation = f"61{length:04x}600e60003961{length:04x}6000f3"
            impl = self.deploy("0x"+creation+runtime)
        else:
            impl = self.deploy(artifact["bytecode"])
        self.market = self.deploy("0x3d602d80600a3d3981f3363d3d373d3d3d363d73" + impl[2:] + "5af43d82803e903d91602b57fd5bf3")
        params = parameters or {"size_precision": 100, "price_precision": 100, "tick_size": 1, "min_size": 100, "max_size": 10**12}
        self.tx(self.market, "initialize(address,uint8,address,uint256,address,uint256,address,uint96,uint32,uint32,uint96,uint96,uint256,uint256,address,uint96,address)",
                [self.maker, 0, self.base, 18, self.quote, 6, self.margin, params["size_precision"], params["price_precision"], params["tick_size"], params["min_size"], params["max_size"], 20, rebate, self.margin, 10, self.evm.get_accounts()[7]])
        for user in (self.maker, self.taker):
            for asset in (self.base, self.quote):
                self.tx(self.margin, "fund(address,address,uint256)", [user, asset, 10**24])

    def deploy(self, bytecode):
        tx = self.evm.send_transaction({"from": self.maker, "gas": 15000000, "data": bytecode})
        receipt = self.evm.get_transaction_receipt(tx)
        assert receipt["status"] == 1
        return receipt["contract_address"]

    def calldata(self, signature, values):
        types = signature.split("(")[1][:-1].split(",") if "()" not in signature else []
        return "0x" + (keccak(text=signature)[:4] + encode(types, values)).hex()

    def tx(self, target, signature, values, sender=None):
        tx = self.evm.send_transaction({"from": sender or self.maker, "to": target, "gas": 10000000,
                                        "data": self.calldata(signature, values)})
        receipt = self.evm.get_transaction_receipt(tx)
        if receipt["status"] != 1:
            self.evm.call({"from": sender or self.maker, "to": target, "data": self.calldata(signature, values)})
        assert receipt["status"] == 1, (signature, receipt)
        return receipt

    def call(self, target, signature, values, types):
        raw = self.evm.call({"from": self.maker, "to": target, "data": self.calldata(signature, values)})
        return decode(types, bytes.fromhex(raw[2:]))

    def order(self, order_id):
        return self.call(self.market, "s_orders(uint40)", [order_id],
                         ["address", "uint96", "uint40", "uint40", "uint40", "uint32", "uint32", "bool"])

    def balance(self, asset):
        return self.call(self.margin, "balances(address,address)", [self.maker, asset], ["uint256"])[0]


def test_real_contract_partial_flip_merge_compounding_and_same_transaction(support):
    market = ContractMarket(support)
    market.tx(market.market, "addFlipBuyOrder(uint32,uint32,uint96,bool)", [100, 120, 200, False])
    # minSize is 100, but this first internal flip has size 50.
    market.tx(market.market, "placeAndExecuteMarketSell(uint96,uint256,bool,bool)", [50, 0, True, False], market.taker)
    assert market.order(1)[1] == 150
    assert market.order(2)[1] == 50
    market.tx(market.market, "placeAndExecuteMarketSell(uint96,uint256,bool,bool)", [25, 0, True, False], market.taker)
    assert market.order(2)[1] == 75
    # Sell at 120 and reinvest at 100: 75 becomes 90 MON raw units.
    market.tx(market.market, "placeAndExecuteMarketBuy(uint96,uint256,bool,bool)", [90, 0, True, False], market.taker)
    assert market.order(1)[1] == 215


def test_real_contract_skips_crossing_provision_and_rejects_size_limit(support):
    market = ContractMarket(support)
    market.tx(market.market, "addFlipSellOrder(uint32,uint32,uint96,bool)", [120, 100, 200, False])
    market.tx(market.market, "addFlipBuyOrder(uint32,uint32,uint96,bool)", [120, 140, 200, False])
    assert market.call(market.market, "s_orderIdCounter()", [], ["uint40"])[0] == 1
    with pytest.raises(Exception):
        market.evm.call({"from": market.maker, "to": market.market,
                         "data": market.calldata("addFlipBuyOrder(uint32,uint32,uint96,bool)", [100, 120, 10**12 + 1, False])})


def test_simulated_partial_flips_match_real_contract_order_sizes(support):
    market = ContractMarket(support)
    original_quote = market.balance(market.quote)
    original_base = market.balance(market.base)
    market.tx(market.market, "addFlipBuyOrder(uint32,uint32,uint96,bool)", [100, 120, 200, False])
    config = lp.LPConfig("spot", 90, 140, 3000, 10**6, 0, contract_rules=True,
                         price_precision_raw=100, min_size_raw=100)
    state = lp.LPState(Decimal(10), Decimal(0), contract_config=config)
    lp.initial_orders(lp.LPDistribution((lp.LPPosition("buy",100,200,120),), (), 0,0,False),state,9,1,100,100)
    lp.activate_orders(l3.L3Book(), state,10,100)
    for index, size in enumerate((50,25)):
        market.tx(market.market,"placeAndExecuteMarketSell(uint96,uint256,bool,bool)",[size,0,True,False],market.taker)
        event = l3.ReplayEvent(10, index, index, "same-tx", "Trade",{
            "filled_size":size,"updated_size":0,"price":10**18,"is_buy":False,"order_id":9}, {})
        lp.apply_fills(event,state,config,100,100,None,110)
        lp.activate_orders(l3.L3Book(),state,10,100)
        assert state.orders["lp-1"].remaining_raw == market.order(1)[1]
        assert state.orders["lp-2"].remaining_raw == market.order(2)[1]
        contract_cash = Decimal(10) + Decimal(market.balance(market.quote)-original_quote)/10**6 + Decimal(market.order(1)[1])/100
        contract_inventory = Decimal(market.balance(market.base)-original_base)/10**18 + Decimal(market.order(2)[1])/100
        assert state.cash == contract_cash
        assert state.inventory == contract_inventory
    market.tx(market.market,"placeAndExecuteMarketBuy(uint96,uint256,bool,bool)",[90,0,True,False],market.taker)
    event = l3.ReplayEvent(10,3,3,"same-tx","Trade",{
        "filled_size":75,"updated_size":0,"price":12*10**17,"is_buy":True,"order_id":9},{})
    lp.apply_fills(event,state,config,100,100,None,110)
    assert state.orders["lp-1"].remaining_raw == market.order(1)[1]
    assert state.orders["lp-1"].reserved_cash == Decimal(215)/100
    assert state.cash == Decimal(10) + Decimal(market.balance(market.quote)-original_quote)/10**6 + Decimal(market.order(1)[1])/100


def test_real_contract_allows_two_flip_legs_in_one_transaction(support):
    market = ContractMarket(support)
    for asset in (market.base,market.quote):
        market.tx(market.margin,"fund(address,address,uint256)",[market.margin,asset,10**24])
    market.tx(market.market,"addFlipBuyOrder(uint32,uint32,uint96,bool)",[100,120,200,False])
    receipt=market.tx(market.margin,"cycle(address,uint96,uint96)",[market.market,100,120])
    assert receipt["status"]==1
    assert market.order(1)[1]==220


def test_simulated_rounding_matches_contract_withdrawable_quote(support):
    market=ContractMarket(support)
    before=market.balance(market.quote)
    market.tx(market.market,"addFlipBuyOrder(uint32,uint32,uint96,bool)",[120,140,109,False])
    assert before-market.balance(market.quote)==1310000
    config=lp.LPConfig("spot",90,150,3000,10**7,0,contract_rules=True,price_precision_raw=100,min_size_raw=100)
    state=lp.LPState(Decimal(10),Decimal(0),contract_config=config)
    lp.initial_orders(lp.LPDistribution((lp.LPPosition("buy",120,109,140),),(),0,0,False),state,9,1,100,100)
    lp.activate_orders(l3.L3Book(),state,10,100)
    assert state.cash==Decimal("9.99")
    assert state.reserved_cash==Decimal("1.30")


@pytest.mark.parametrize("side",["buy","sell"])
def test_simulator_rebate_matches_real_contract_asset(support,side):
    market=ContractMarket(support,rebate=10)
    asset=market.quote if side=="buy" else market.base
    market.tx(market.market,"addFlipBuyOrder(uint32,uint32,uint96,bool)" if side=="buy" else "addFlipSellOrder(uint32,uint32,uint96,bool)",
              [100,120,200,False] if side=="buy" else [120,100,200,False])
    before=market.balance(asset)
    market.tx(market.market,"placeAndExecuteMarketSell(uint96,uint256,bool,bool)" if side=="buy" else "placeAndExecuteMarketBuy(uint96,uint256,bool,bool)",
              [100,0,True,False] if side=="buy" else [120,0,True,False],market.taker)
    assert market.balance(asset)-before==(1000 if side=="buy" else 10**15)


@pytest.mark.parametrize("shape", ["spot", "curve", "bid_ask"])
def test_sdk_grid_matches_contract_initial_orders_and_token_debits(support, shape):
    fixture = json.loads((ROOT / "sdk_reference/fixtures/concentrated_liquidity_v0_0_95.json").read_text())
    grid = fixture["shapes"][shape]
    # Use the official fixture sizes, including its deliberately small levels.
    # Contract minimum enforcement is checked separately above.
    params = {"size_precision": 10**10, "price_precision": 10**8,
              "tick_size": 100, "min_size": 1, "max_size": 2 * 10**18}
    market = ContractMarket(support, parameters=params)
    orders = [(item, True) for item in grid["bids"]] + [(item, False) for item in grid["asks"]]
    prices = [int(item["price"]) for item, _ in orders]
    flips = [int(item["flipPrice"]) for item, _ in orders]
    sizes = [int(item["liquidity"]) for item, _ in orders]
    sides = [side for _, side in orders]
    cash_before, inventory_before = market.balance(market.quote), market.balance(market.base)
    market.tx(market.market, "batchProvisionLiquidity(uint32[],uint32[],uint96[],bool[],bool)",
              [prices, flips, sizes, sides, False])
    for order_id, (item, side) in enumerate(orders, 1):
        actual = market.order(order_id)
        assert (actual[5], actual[1], actual[6], actual[7]) == (
            int(item["price"]), int(item["liquidity"]), int(item["flipPrice"]), side)
    from kuru_backtester.engine import lp_contract_rules as rules
    expected_quote = sum(rules.quote_raw(price, size, 10**8, 10**10, round_up=True)
                         for price, size, side in zip(prices, sizes, sides) if side)
    expected_base = sum(rules.base_raw(size, 10**10) for size, side in zip(sizes, sides) if not side)
    assert cash_before - market.balance(market.quote) == expected_quote
    assert inventory_before - market.balance(market.base) == expected_base
    # Cancel each bid to verify its withdrawable value independently of the
    # amount rounded up on initial funding.
    before_cancel = market.balance(market.quote)
    bid_ids = list(range(1, len(grid["bids"]) + 1))
    market.tx(market.market, "batchCancelFlipOrders(uint40[])", [bid_ids])
    expected_refund = sum(rules.quote_raw(price, size, 10**8, 10**10)
                          for price, size, side in zip(prices, sizes, sides) if side)
    assert market.balance(market.quote) - before_cancel == expected_refund
