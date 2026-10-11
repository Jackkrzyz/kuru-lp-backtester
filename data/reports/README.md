# Recorded results

- `lp_contract_regression_report.json`: current contract-corrected LP golden results.
- `golden_regression_report.json`: older generalization results. It is historical evidence, not the current engine baseline.
- `envio_interval_smoke.json`: bounded Trade discovery and final candidate order enrichment.

The files retain their recorded numerical content. See the [LP contract checks](../../docs/LP_CONTRACT_PARITY.md) for the tested contract scope. New local diagnostic reports belong in `.cache/` unless they are selected as small versioned evidence.

Current setup and commands are in the [project README](../../README.md). The
[history guide](../../docs/FULL_HISTORY.md) records the 2026-10-11 status-page
performance checks. Those checks do not replace the golden engine results.
The latest full suite passed 259 tests and 12 subtests.

Do not treat a downloaded range as validated because it appears in the Data
suite. Promotion requires the existing event-parity, L2, queue, and hash checks.
Never add API tokens, raw event archives, or full replay timelines here.
