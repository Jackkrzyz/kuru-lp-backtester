# Archived demo replays

The current app does not use these files. The small catalogs retain the original raw-to-compact counts and hashes. The large replay JSON files are ignored by Git and preserved locally.

Two optional artifact tests read them when available. Current engine and API tests use their own configurations and verified history.

The product has one strategy mode: Kuru LP. Its Run backtest path computes a
result from the submitted configuration and local validated history. These
archived files are not a fallback when the backend fails. See the
[current request path](../../../docs/BACKTEST_REQUEST_PATH.md) and
[project setup](../../../README.md). The 2026-10-11 full local suite passed
259 tests and 12 subtests; this does not update the old replay hashes.

To rebuild the old artifacts from existing raw timelines, run from the repository root:

```bash
python -m kuru_backtester.compact_replays
python -m kuru_backtester.legacy.build_lp_frontend
```

These commands write here. They do not add demo data to the public frontend.
