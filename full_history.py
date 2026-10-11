"""Compatibility launcher for the resumable historical indexing command."""

from kuru_backtester.history.full_history import main

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError) as error:
        raise SystemExit(f"Error: {error}") from error
