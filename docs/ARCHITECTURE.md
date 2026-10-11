# Architecture

This is a source-checkout application. Install it in editable mode. A Python wheel alone does not contain the frontend, Node SDK, or local historical data.

## Runtime path

1. `frontend/app.js` uses `sdk_reference/browser_adapter.js`, bundled for the browser, to create the preview grid.
2. `kuru_backtester/server/backtest_server.py` accepts a job. `backtest_jobs.py` starts `python -m kuru_backtester.server.backtest_worker`.
3. `backtest_service.py` validates the configuration and the exact SDK grid. It loads only covered local history.
4. `history/historical_system.py` verifies file hashes, loads a checkpoint, and replays to the requested start. Its cursor streams events in block, transaction, and log order.
5. `engine/kuru_lp.py` runs the position over that cursor. `engine/l3_vertical_slice.py` owns the historical FIFO book. `engine/lp_contract_rules.py` owns the checked integer conversion rules.
6. `compact_replays.py` produces the bounded replay. The worker writes the result and detailed fills with content hashes. The browser controls replay timing.

The old `engine/strategy_vertical_slice.py` name is retained because the module supplies shared accounting, transaction budgets, queues, and event loading. It also retains the old MM regression engine. It is not a selectable product mode. Do not delete this module as dead demo code.

## Data preparation

`history/discover_envio_intervals.py` downloads Trade events in one forward pass. Window ranking is local. Order-event enrichment is limited to candidate intervals.

`history/index_history.py` downloads a bounded canonical event partition. `history/reconstruction_validation_report.py` compares it with RPC and contract L2. `history/promote_historical_partition.py` adds only passing data to the local manifest.

`history/full_history.py` composes these steps in resumable partitions. It has a durable page cursor and a writer lock. Download-only output remains unvalidated. An unsupported implementation or vault state stops reconstruction. The standalone progress monitor is read-only. The app's Data suite uses `server/data_suite.py` to inspect local metadata and start bounded download-only subprocesses through the same CLI. No reconstruction or strategy rules are changed.

The root manifests retain their locations to preserve relative event paths and existing content identities. `paths.py` supplies the common repository root for Python modules. Do not derive the root from a nested module's parent directory.

The Data suite serves a cached inventory snapshot. One background thread scans
saved file metadata at most once per minute after completion. Its scan lock is
separate from job controls and progress reads. First-load totals are unknown
until a scan completes. Scan failures preserve the last snapshot. Both local
HTTP handlers use `server/http_response.py` to handle a closed client without
sending another error response. The browser retains unchanged tables and
updates worker age and retry timers separately.

## Source and generated files

| Location | Purpose |
|---|---|
| `engine/`, `history/`, `server/` | Current runtime code |
| `tools/` | Bounded diagnostics and reproducible reports |
| `legacy/` | L2 snapshot analysis, early RPC discovery, old MM comparison, old demo export |
| `sdk_reference/fixtures/` | Exact SDK regression inputs and expected grids |
| `data/reports/` | Small saved numerical reports; preserve their engine version |
| `docs/archive/` | Historical development records; not current setup instructions |
| `tests/fixtures/legacy_replays/` | Small tracked catalogs and optional large local demo replays |
| `.cache/` and output folders | Local generated data; preserve before moving a checkout |

Do not hand-edit `frontend/kuru_sdk_adapter.js`. Change the SDK adapter and rebuild with `npm run build:browser --prefix sdk_reference`.

## Commands

`python -m kuru_backtester` dispatches to one module. It imports only that command. The `kuru` console command uses the same dispatcher. Three root scripts preserve the existing app, download, and monitor commands.

Other early root scripts now run as modules. Examples:

```bash
python -m kuru_backtester.legacy.analyze_l2 --date 2026-05-01
python -m kuru_backtester.legacy.multi_regime_evaluation --help
python -m kuru_backtester.engine.l3_vertical_slice --help
```

These tools remain available for reproducing old evidence. The current user flow is the Kuru LP app.

## Change checks

Use `python -m pytest -q` for the complete suite. It includes exact SDK grids, queue priority, causal fills, capital constraints, contract bytecode tests, accounting, asynchronous frontend handlers, and download resume tests.

Run all three golden backtests after an engine or integration change. Compare the complete report, including fill and semantic timeline hashes, with the saved version. Tests with only synthetic data are not sufficient evidence for historical parity.

Use the bounded benchmark to measure performance. Do not change grid, queue, flip, fill, or accounting rules to improve a benchmark score.
