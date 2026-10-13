# Archived demo replays

The current app does not use these files. The small catalogs retain the original raw-to-compact counts and hashes. The large replay JSON files are ignored by Git and preserved locally.

Two optional artifact tests read them when available and skip when the large
local files are absent. Current engine and API tests use their own
configurations and verified history.

The product has one strategy mode: Kuru LP. Its Run backtest path computes a
result from the submitted configuration and local validated history. These
archived files are not a fallback when the backend fails. See the
[current request path](../../../docs/BACKTEST_REQUEST_PATH.md) and
[project setup](../../../README.md). The 2026-10-13 full local suite passed
**381 tests and 14 subtests** in **94.91 seconds**, before commit `1d8066e`.
This does not update the archived replay hashes.

The current history chart reads verified Trade events. The current backtest
replay uses Canvas and the result of a submitted or loaded LP run. It does not
read these archived demo files. Current jobs and saved job history are separate
from saved backtest results.

To rebuild the old artifacts from existing raw timelines, run from the repository root:

```bash
python -m kuru_backtester.compact_replays
python -m kuru_backtester.legacy.build_lp_frontend
```

These commands write here. They do not add demo data to the public frontend.
