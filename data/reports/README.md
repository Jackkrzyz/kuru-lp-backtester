# Recorded kuruLab results

- `lp_contract_regression_report.json`: current contract-corrected LP golden results.
- `golden_regression_report.json`: older generalization results. It is historical evidence, not the current engine baseline.
- `envio_interval_smoke.json`: bounded Trade discovery and final candidate order enrichment.

The files retain their recorded numerical content and hashes. They are reference
results, not current download progress or new backtest output. See the
[LP contract checks](../../docs/LP_CONTRACT_PARITY.md) for the tested scope.
New local diagnostic reports belong in `.cache/` unless they are selected as
small versioned evidence.

Current setup and commands are in the [project README](../../README.md). The
[history guide](../../docs/FULL_HISTORY.md) records download and verification
controls. The [terminal report](../../docs/TERMINAL_UI.md) records incremental
verified candles, job banners, and the activity trace. Those measurements do not
replace the golden engine results.

Latest complete local check, 2026-10-13: **381 tests and 14 subtests passed**
in **94.91 seconds**, before commit `1d8066e`.

To rerun the three LP golden comparisons from the repository root:

```bash
python -m kuru_backtester regression
```

This requires the corresponding local verified history and contract cache. It
writes `lp_contract_regression_report.json`; inspect numerical and hash changes
before accepting a new baseline. Named intervals remain regression fixtures.

Download, verification, Trade indexing, and LP eligibility are separate states.
The main chart can show a verified older runtime before its LP simulation is
approved. LP promotion requires event parity, L2 and full L3 comparisons, queue
and cancellation audits, content hashes, and supported contract rules.
Never add API tokens, raw event archives, or full replay timelines here.
