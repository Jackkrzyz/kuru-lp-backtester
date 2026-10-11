"""Check packaged commands and the actual HTTP-to-worker result path."""

import json
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen

from kuru_backtester.paths import ROOT
from kuru_backtester.server.backtest_jobs import BacktestJobManager
from kuru_backtester.server.backtest_server import BacktestHandler
from kuru_backtester.server.backtest_service import BacktestService, canonical_sdk_grid
from kuru_backtester.tools.golden_regression import base_request


def test_lightweight_server_import_and_command_help():
    subprocess.run([
        sys.executable, "-c",
        "import sys; from kuru_backtester.server import backtest_server; "
        "assert 'pandas' not in sys.modules; assert 'matplotlib.pyplot' not in sys.modules",
    ], cwd=ROOT, check=True, capture_output=True, text=True)
    result = subprocess.run(
        [sys.executable, "-m", "kuru_backtester", "serve", "--help"],
        cwd=ROOT, check=True, capture_output=True, text=True,
    )
    assert "--maximum-workers" in result.stdout
    plan = subprocess.run([
        sys.executable, "-m", "kuru_backtester", "history", "--to-finalized", "--download-only",
    ], cwd=ROOT, check=True, capture_output=True, text=True)
    assert "No network calls or downloads" in plan.stdout


def test_http_job_launches_packaged_worker_and_returns_its_result(tmp_path):
    service = BacktestService()
    manager = BacktestJobManager(tmp_path / "jobs", maximum_workers=1)
    handler = type("PackageTestHandler", (BacktestHandler,), {
        "service": service, "jobs": manager, "access_token": None,
    })
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(root, timeout=10) as response:
            assert b'id="run-button-label"' in response.read()
        request = {**base_request(), "from_block": 109097028, "to_block": 109097127}
        data = service.load_range(request["market"], request["from_block"], request["to_block"])
        _, sdk_input = service.normalize_request(request, data)
        request["order_grid"] = canonical_sdk_grid(sdk_input)
        with urlopen(Request(root + "/api/backtests", json.dumps(request).encode(), {
            "Content-Type": "application/json",
        }), timeout=30) as response:
            assert response.status == 202
            job = json.load(response)
        headers = {"X-Backtest-Owner": job["owner_token"]}
        deadline = time.monotonic() + 90
        while True:
            with urlopen(Request(root + job["status_url"], headers=headers), timeout=10) as response:
                status = json.load(response)
            if status["status"] in {"completed", "failed", "canceled", "stalled"}:
                break
            assert time.monotonic() < deadline, status
            time.sleep(0.1)
        assert status["status"] == "completed", status
        with urlopen(Request(root + job["status_url"] + "/result", headers=headers), timeout=15) as response:
            result = json.load(response)
        assert result["run_id"] == status["result_run_id"]
        assert result["normalized_configuration"]["from_block"] == request["from_block"]
        assert result["normalized_configuration"]["to_block"] == request["to_block"]
        assert result["normalized_configuration"]["shape"] == request["shape"]
        assert result["generated_initial_bid_count"] > 0
        assert len(result["summary_sha256"]) == 64
        assert len(result["timeline_sha256"]) == 64
        saved_request = json.loads((manager.root / job["run_id"] / "request.json").read_text())
        assert saved_request == request
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
        manager.close()
