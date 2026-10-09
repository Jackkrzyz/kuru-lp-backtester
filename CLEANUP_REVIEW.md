# Code cleanup and review

Date: 2026-10-08. Scope: maintenance and an interval-expansion plan.

The working tree already contained the LP contract corrections and server hardening from the previous task. This review preserved those changes. It did not commit or push code, stop the existing server, change LP matching or accounting, or start a historical download.

## Review scope

Reviewed the request path, worker and SDK process ownership, result/status writes, frontend asynchronous handlers, canonical event indexing, checkpoint metadata, coverage checks, Trade chart generation, and related regression tests. Reviewed the current LP replay and the previous performance audit for remaining risks. Third-party packages and generated SDK bundle code were not hand-edited.

This is not a claim that every possible code path is bug-free. The changes below have specific regression tests.

## Changes

| Area | Defect or cost | Correction |
|---|---|---|
| Job startup | The parent could overwrite a fast worker's terminal status with queued status. | The worker alone publishes its PID and progress after launch. |
| Worker launch | Parent log handles were not explicitly closed; failed launches left queued jobs. | Close handles through context managers. Record and surface launch failure. |
| Atomic output | Concurrent writes in one process shared a PID-only temporary path. | Give every write a unique temporary file. Keep atomic replacement and durability. |
| Server startup | Broad temporary-file cleanup could remove another server's active write. | Remove automatic deletion. Existing raw and temporary artifacts are preserved. |
| Price formatting | Stripping all trailing zeros changed whole-number prices such as 10 to 1. | Strip zeros only from the fractional part. Preserve integer values. |
| Frontend preview | Editing inputs during preview preparation could still submit a stale request. | Check the configuration token again before POST. |
| Frontend job ownership | A late POST reply could replace a newer attached job. | Cancel only the stale reply's job and leave the newer job in place. |
| Frontend progress | A late status reply could overwrite a newer UI state. | Check job ownership and configuration again after the response. |
| Auto-fill | A slow balance search could overwrite a later token edit. | Check the configuration revision before applying its values or errors. |
| Cancel display | A late cancellation could hide a newer job's cancel control. | Hide it only when no newer job is attached. |
| Index pagination | Missing `next_block` was interpreted as completion. | Reject the page and retain the saved cursor. Also reject out-of-page log blocks. |
| Checkpoint metadata | The parameter reader defaulted to the deployment block when indexing a later checkpoint. | Add an explicit block argument and use the checkpoint block. Save actual token decimals. Existing callers keep the legacy default. |
| Checkpoint path | Checkpoint paths did not have the event store's root-boundary check. | Reject a path outside the project root before reading it. |
| Trade chart memory | Hashing used `read_bytes()` for a complete second in-memory source copy. | Hash in 1 MiB chunks. This changes memory use, not the hash. |
| Trade chart input | Foreign addressed rows, out-of-range blocks, and nonfinite prices could enter the chart. | Reject these invalid inputs. Keep compatibility with legacy rows that have no address. |
| Coverage display | The chart artifact still listed only the three original ranges. | Use current backend coverage in the renderer and refresh the small chart artifact. The 494 price points and Trade source identity are unchanged. |

The concurrency and frontend tests use controlled scheduling. The frontend tests execute the actual application handlers with delayed replies; they do not test a separately invented handler.

## Verification

Commands used in WSL:

```bash
python -m pytest -q
node --check frontend/app.js
node sdk_reference/test_frontend_async.js
python -m pip check
python generate_trade_history_chart.py
```

The complete suite passed: **194 tests and 12 subtests**. The JavaScript syntax check passed. Four controlled asynchronous frontend scenarios passed. The dependency check found no broken requirements. `git diff --check` passed.

A separate call to `golden_regression.run_report()` writes `.cache/cleanup_regression_report.json` and compares its complete parsed content with `lp_contract_regression_report.json`. All three corrected LP fixture results and deterministic hashes are unchanged. No financial fixture is overwritten.

The regenerated Trade chart is compared with the independently generated `.cache/cleanup_trade_chart.json`. The Trade points, source hash, and count remain the same. Coverage metadata now includes all six validated partitions. No network requests were used for these backtests or chart checks.

## Remaining work found in the review

1. **Reporting accuracy:** Time in range and capital utilization are activity-sampled. They are not elapsed-time-weighted. This was not changed during cleanup because it needs its own definition, tests, and report-version change. It affects comparison across intervals with different event rates, not the current fill or cash rules.
2. **Long-job health:** The fixed 120-second stall rule can misclassify long I/O or validation stages. A stalled worker can also remain alive. Add stage-aware heartbeats and an explicit recovery policy before large preparation jobs. Do not kill an unowned PID after a server restart.
3. **Crash recovery:** An interrupted append can leave a partial staging JSONL line. Current startup does not repair such a line. Use committed byte offsets or atomic pages with a tested recovery policy before unattended large indexing.
4. **Preparation completeness:** Hourly candles and known-event filters do not prove L3 completeness. Unknown contract versions, parameter changes, vault activity, and reorganizations need explicit gates. Current LP service correctly rejects untested implementations and active vault state.
5. **Range-load speed:** JSONL range reads still scan partition prefixes. Counts, transaction-volume analysis, and replay can make multiple passes. Add a hash-keyed derived seek index and measure the change. Do not replace canonical files or verified reconstruction rules.
6. **SDK workload:** The order-count safety limit is checked after grid generation. Very small spread inputs or large ranges can consume substantial SDK and browser work before rejection. Add a source-verified preflight or a bounded worker path; do not invent a distribution formula.
7. **Installation:** The app depends on source-tree frontend assets and the pinned Node SDK directory. A Python wheel alone is not a complete deployment package. Use the documented repository setup until packaging is tested.
8. **Counterfactual trading:** Added simulated orders do not change the historical taker's budget, limit, or later behavior. Contract parity tests do not remove this limitation. Keep transaction-impact warnings and gas exclusion visible.

The next data work is in [the interval plan](ANY_INTERVAL_PLAN.md). It separates Trade candles, checkpoint preparation, dense event acquisition, validation, and local simulation. No always-on market recorder is required.
