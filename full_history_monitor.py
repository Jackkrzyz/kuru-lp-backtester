"""Compatibility launcher for the local indexing monitor."""

from kuru_backtester.server.full_history_monitor import main

if __name__ == "__main__":
    raise SystemExit(main())
