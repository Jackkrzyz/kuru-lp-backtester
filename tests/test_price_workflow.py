"""UI-only range and workflow checks. The verified engine is not changed."""

from pathlib import Path
import subprocess

from kuru_backtester.server.backtest_service import resolve_node


def test_range_controls_sdk_grid_and_sequential_workflow() -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [resolve_node(), "test_price_workflow.js"],
        cwd=root / "sdk_reference", check=True, capture_output=True, text=True,
    )
    assert "tests passed" in result.stdout
