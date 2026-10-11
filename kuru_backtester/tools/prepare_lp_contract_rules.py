#!/usr/bin/env python3
"""Capture historical contract parameters for local LP runs; no event backfill."""

import argparse
import hashlib
import json
from pathlib import Path

from eth_abi import decode
from eth_utils import keccak
from kuru_backtester.engine.rebuild_from_chain import RpcClient, DEFAULT_RPC_URL
from kuru_backtester.server.backtest_service import atomic_write_bytes, canonical_bytes

from kuru_backtester.paths import ROOT
OUTPUT = ROOT / ".cache/lp_contract_rules.json"
PARAM_TYPES = ("uint32", "uint96", "address", "uint256", "address", "uint256",
               "uint32", "uint96", "uint96", "uint256", "uint256")
PARAM_KEYS = ("price_precision", "size_precision", "base_asset", "base_decimals",
              "quote_asset", "quote_decimals", "tick_size", "min_size", "max_size",
              "taker_fee_bps", "maker_fee_bps")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rpc-url", default=DEFAULT_RPC_URL)
    args = parser.parse_args()
    manifest = json.loads((ROOT / "historical_index_manifest.json").read_text())
    market = manifest["market"]
    rpc = RpcClient(args.rpc_url)
    rows = {}
    for checkpoint in manifest["checkpoints"]:
        block = checkpoint["block_number"]
        raw = rpc.contract_call(market, "getMarketParams()", block)
        values = decode(PARAM_TYPES, bytes.fromhex(raw[2:]))
        parameters = dict(zip(PARAM_KEYS, values))
        raw_vault = rpc.contract_call(market, "getVaultParams()", block)
        vault = decode(("address", "uint256", "uint96", "uint256", "uint96", "uint96", "uint96", "uint96"), bytes.fromhex(raw_vault[2:]))
        code = rpc.call("eth_getCode", [market, hex(block)])
        slot = hex(int.from_bytes(keccak(text="eip1967.proxy.implementation"), "big") - 1)
        implementation = rpc.call("eth_getStorageAt", [market, slot, hex(block)])
        implementation_address = "0x" + implementation[-40:]
        implementation_code = rpc.call("eth_getCode", [implementation_address, hex(block)])
        if implementation_code == "0x":
            raise RuntimeError("historical market implementation code is unavailable")
        rows[str(block)] = {
            "parameters": parameters,
            "vault_best_bid": int(vault[1]), "vault_best_ask": int(vault[3]),
            "vault_bid_size": int(vault[5]) - int(vault[2]),
            "vault_ask_size": int(vault[6]) - int(vault[4]),
            "checkpoint_sha256": checkpoint["sha256"],
            "rpc_response_sha256": hashlib.sha256(bytes.fromhex(raw[2:])).hexdigest(),
            "market_code_sha256": hashlib.sha256(bytes.fromhex(code[2:])).hexdigest(),
            "implementation_address": implementation_address,
            "implementation_code_sha256": hashlib.sha256(bytes.fromhex(implementation_code[2:])).hexdigest(),
        }
        code_path = ROOT / ".cache/lp_contract_implementations" / f"{rows[str(block)]['implementation_code_sha256']}.json"
        atomic_write_bytes(code_path, canonical_bytes({"code": implementation_code}) + b"\n")
        print(f"block={block} maker_rebate_bps={parameters['maker_fee_bps']} max_size={parameters['max_size']}")
    atomic_write_bytes(OUTPUT, canonical_bytes({"schema_version": "1.0", "market": market, "checkpoints": rows}) + b"\n")
    print(f"Saved {OUTPUT}")


if __name__ == "__main__":
    main()
