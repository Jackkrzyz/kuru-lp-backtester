"""Compatibility launcher. The implementation is in kuru_backtester.server."""

from kuru_backtester.server.backtest_server import main

if __name__ == "__main__":
    raise SystemExit(main())
