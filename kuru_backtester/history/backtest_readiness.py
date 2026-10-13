"""Publish checked, LP-compatible partitions to the local backtest index.

Reconstruction of an old runtime does not establish LP simulation support.
This gate accepts only the implementation covered by the existing LP tests.
"""
import json
from pathlib import Path

from eth_abi import decode

from kuru_backtester.paths import ROOT
from kuru_backtester.history import full_history as history, reconstruction_profiles as profiles
from kuru_backtester.engine import strategy_vertical_slice as strategy
from kuru_backtester.engine.rebuild_from_chain import market_parameters


def publish(directory: Path, rpc, root: Path = ROOT) -> dict:
    summary_path = directory / "summary.json"
    summary = strategy.verify_validation_summary(summary_path)
    guards = summary.get("contract_guards", [])
    if not guards or any(row.get("implementation_code_sha256") != profiles.CURRENT_IMPLEMENTATION
                         or row.get("vault_active") is not False for row in guards):
        return {"status": "reconstruction_only", "reason": "This runtime has not passed LP simulation parity tests."}
    for name, digest in (("events.jsonl", summary["event_input"]["source_sha256"]),
                         ("checkpoint.json", summary["checkpoint_sha256"]),
                         ("end_checkpoint.json", summary["end_checkpoint_sha256"])):
        if history.content_hash(directory / name) != digest:
            raise ValueError(f"Backtest readiness hash mismatch: {name}")
    checkpoint = json.loads((directory / "checkpoint.json").read_text())
    first, last = summary["interval"]["from_block"], summary["interval"]["to_block"]
    block, market = first - 1, summary["market"]
    if checkpoint["checkpoint_block"] != block or checkpoint["market"] != market:
        raise ValueError("Backtest readiness checkpoint identity mismatch")
    locks = root / ".cache/verification_locks"
    locks.mkdir(parents=True, exist_ok=True)
    with history.writer_lock(locks / "backtest-readiness.lock"):
        manifest_path = root / "historical_index_manifest.json"
        manifest = json.loads(manifest_path.read_text())
        if manifest["market"] != market:
            raise ValueError("Backtest index market mismatch")
        overlaps = [row for row in manifest["event_partitions"]
                    if row["from_block"] <= last and row["to_block"] >= first]
        if overlaps:
            cursor = first
            for row in sorted(overlaps, key=lambda item: item["from_block"]):
                if row["from_block"] > cursor:
                    break
                cursor = max(cursor, row["to_block"] + 1)
            return {"status": "already_indexed" if cursor > last else "overlap_requires_review",
                    "from_block": first, "to_block": last}
        before = rpc.batch([("eth_getBlockByNumber", [hex(number), False]) for number in (block, last)])
        if any(header["hash"] != summary["boundary_block_hashes"][str(number)]
               for number, header in zip((block, last), before, strict=True)):
            raise ValueError("Backtest readiness boundary hash changed")
        guard = history.contract_guard(rpc, market, block)
        if guard["implementation_code_sha256"] != profiles.CURRENT_IMPLEMENTATION:
            raise ValueError("Backtest readiness implementation changed")
        parameters = market_parameters(rpc, market, block)
        final_parameters = market_parameters(rpc, market, last)
        if parameters != final_parameters or any(int(parameters[key]) != value
                                                for key, value in checkpoint["parameters"].items()):
            raise ValueError("Backtest readiness market parameters changed")
        vault = decode(("address", "uint256", "uint96", "uint256", "uint96", "uint96", "uint96", "uint96"),
                       bytes.fromhex(rpc.contract_call(market, "getVaultParams()", block)[2:]))
        if vault[5] - vault[2] or vault[6] - vault[4]:
            raise ValueError("Backtest readiness requires inactive vault liquidity")
        after = rpc.batch([("eth_getBlockByNumber", [hex(number), False]) for number in (block, last)])
        if [header["hash"] for header in before] != [header["hash"] for header in after]:
            raise ValueError("Backtest readiness boundary hash changed during capture")
        path = root / ".cache/lp_contract_rules.json"
        rules = json.loads(path.read_text()) if path.exists() else {
            "schema_version": "1.0", "market": market, "checkpoints": {}}
        if rules["market"] != market:
            raise ValueError("LP rules market mismatch")
        row = {"parameters": parameters, "vault_best_bid": int(vault[1]), "vault_best_ask": int(vault[3]),
               "vault_bid_size": 0, "vault_ask_size": 0, "checkpoint_sha256": summary["checkpoint_sha256"],
               "implementation_code_sha256": guard["implementation_code_sha256"],
               "validation_summary_sha256": history.content_hash(summary_path)}
        old = rules["checkpoints"].get(str(block))
        if old is not None and old["checkpoint_sha256"] != row["checkpoint_sha256"]:
            raise ValueError("Existing LP rules bind a different checkpoint")
        if old is None:
            rules["checkpoints"][str(block)] = row
            history.atomic_json(path, rules)
        # Publish last. A reader must never see indexed coverage without rules.
        history.promote(manifest_path, directory / "events.jsonl", summary_path, directory / "checkpoint.json")
        result = {"status": "ready", "from_block": first, "to_block": last,
                  "checkpoint_sha256": summary["checkpoint_sha256"]}
        history.atomic_json(directory / "backtest_readiness.json", result)
        return result
