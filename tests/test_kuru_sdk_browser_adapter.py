"""Tests for the official Kuru SDK browser adapter."""

import json
import subprocess
from pathlib import Path

from backtest_service import resolve_node
from sdk_grid_broker import PersistentSdkProcess


ROOT = Path(__file__).resolve().parents[1]


def test_browser_adapter_matches_all_official_shape_fixtures() -> None:
    completed = subprocess.run(
        ["node", "test_browser_adapter.js"],
        cwd=ROOT / "sdk_reference",
        check=True,
        capture_output=True,
        text=True,
    )
    assert "tests passed" in completed.stdout


def test_browser_bundle_is_built_from_the_sdk_adapter() -> None:
    source = (ROOT / "sdk_reference" / "browser_adapter.js").read_text(encoding="utf-8")
    bundle = (ROOT / "frontend" / "kuru_sdk_adapter.js").read_text(encoding="utf-8")
    assert 'require("@kuru-labs/kuru-sdk")' in source
    assert "generateOrderGrid" in bundle
    assert "getSpotBatchLPDetails" in bundle
    assert "serializeOrderGrid" in bundle


def test_frontend_accepts_only_the_latest_request() -> None:
    completed = subprocess.run(
        ["node", "test_request_gate.js"],
        cwd=ROOT / "sdk_reference",
        check=True,
        capture_output=True,
        text=True,
    )
    assert "tests passed" in completed.stdout


def test_frontend_async_handlers_preserve_latest_job_ownership() -> None:
    completed = subprocess.run(
        [resolve_node(), "test_frontend_async.js"], cwd=ROOT / "sdk_reference",
        check=True, capture_output=True, text=True,
    )
    assert "tests passed" in completed.stdout


def test_persistent_sdk_worker_reuses_one_process_without_grid_changes() -> None:
    fixture = json.loads(
        (ROOT / "sdk_reference" / "fixtures" / "concentrated_liquidity_v0_0_95.json")
        .read_text(encoding="utf-8")
    )
    values = fixture["inputs"]
    sdk_input = {
        "shape": "spot",
        "minimumPrice": "0.0263",
        "maximumPrice": "0.02795",
        "feePercent": "0.30",
        "monAmount": "2000",
        "usdcAmount": "60",
        "driver": "both",
        "bestAskPriceRaw": values["bestAskPrice"],
        "pricePrecision": values["pricePrecision"],
        "sizePrecision": values["sizePrecision"],
        "quoteAssetDecimals": int(values["quoteAssetDecimals"]),
        "baseAssetDecimals": int(values["baseAssetDecimals"]),
        "tickSize": values["tickSize"],
        "minSize": values["minSize"],
    }
    worker = PersistentSdkProcess(
        resolve_node(),
        ROOT / "sdk_reference" / "generate_grid_worker.js",
        ROOT,
    )
    try:
        first = worker.request(sdk_input)
        process_id = worker._process.pid if worker._process is not None else None
        second = worker.request(sdk_input)
        assert worker._process is not None
        assert worker._process.pid == process_id
        assert first == second
    finally:
        worker.close()
