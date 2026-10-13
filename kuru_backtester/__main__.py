"""One command menu. Import only the selected tool and pass its options through."""

import argparse
import runpy
import sys

COMMANDS = {
    "serve": "server.backtest_server",
    "monitor": "server.full_history_monitor",
    "history": "history.full_history",
    "verify-download": "history.verify_download",
    "index": "history.index_history",
    "validate": "history.reconstruction_validation_report",
    "promote": "history.promote_historical_partition",
    "discover": "history.discover_envio_intervals",
    "trade-chart": "tools.generate_trade_history_chart",
    "contract-rules": "tools.prepare_lp_contract_rules",
    "regression": "tools.golden_regression",
    "benchmark": "tools.benchmark_lp",
}


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(
        description="kuruLab. Run a command with --help for its options."
    )
    parser.add_argument("command", choices=COMMANDS)
    # Let the selected tool own its full option parser, including --help.
    selected = parser.parse_args(arguments[:1])
    original = sys.argv
    try:
        sys.argv = [f"kuru {selected.command}", *arguments[1:]]
        runpy.run_module(f"kuru_backtester.{COMMANDS[selected.command]}", run_name="__main__")
    finally:
        sys.argv = original
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
