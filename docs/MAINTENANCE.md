# Organization and maintenance results

Date: 2026-10-10.

## Current follow-up — 2026-10-11

After commit `dc8a924` was pushed, a separate
[code review](reviews/2026-10-11_REVIEW.md) recorded job-state bugs, public
hosting risks, and speed work. The review did not change runtime code or stop
the active download. See the proposed
[hosting plan](plans/HOSTING_AND_SUBMISSION.md) and
[wallet execution plan](plans/WALLET_EXECUTION.md). Neither plan is implemented.

The later UI and Data suite changes passed **259 tests and 12 subtests**. The
LP page now uses a three-step flow, exact SDK range previews, manual token
amounts, and visible historical prices and dates. The Data suite combines the
Envio flow diagram, progress trace, coverage, storage totals, and bounded
download controls.

The status-page correction moved storage scans to one background thread,
reduced duplicate file-status calls, separated scan and job locks, and handled
closed HTTP connections. Real local status requests took 15–22 ms during a
scan. No engine rules changed. See [the current history guide](FULL_HISTORY.md)
for the test commands, measurements, and restart instructions. The dated
cleanup results below remain a record of that earlier milestone.

## Project organization

- Moved 26 Python modules into `kuru_backtester/`, grouped by engine, history, server, tools, and legacy work.
- Added one command menu: `python -m kuru_backtester`, or `kuru` after an editable install.
- Kept the three existing root launchers for the app, full-history job, and progress monitor.
- Reduced the root from 50 previously tracked files to 9 current project files. Ignored local files are outside this count.
- Kept the two root manifests in place. Their existing relative paths, checkpoint hashes, and data identities are unchanged.
- Moved small numerical reports to `data/reports/`.
- Replaced the long development README with current setup, run, data, and test instructions. Moved historical reports to `docs/archive/` and marked them as historical. Kept the future interval plan in `docs/plans/`.
- Moved 13 old demo replays, **103,845,381 bytes**, from the public web folder to `tests/fixtures/legacy_replays/`. The files remain on disk and are now ignored. Small catalogs remain eligible for version control. The current public data folder contains only the 44,978-byte Trade chart.

No raw history, checkpoints, strategy timelines, or user results were deleted. The Git history was not rewritten. Previously committed large files remain in Git history. The cleanup does not reclaim their local disk space.

The old L3 and MM module names remain inside `engine/`. These modules contain live shared queue and accounting functions. Removing them would remove verified behavior. The old MM user mode remains absent.

## Bugs and performance

| Finding | Change |
|---|---|
| A worker can remove its heartbeat between the existence check and the read. | Read the durable terminal status when the heartbeat disappears. |
| A worker can finish after the parent reads running status but before the exit check. | Read status again after child exit. Preserve completed output instead of marking it failed. |
| `.env` was not ignored. | Ignore `.env` and private variants. Add a token-free `.env.example`. |
| Download-only completion printed paths for validation and candles that it did not produce. | Print the archive directory and state that validation and candle export have not run. |
| Server imports loaded pandas through the old Parquet exporter. | Load pandas only when exporting Parquet. |
| Full-history validation helpers loaded matplotlib when no plot was requested. | Load matplotlib inside the two plot functions. |
| Plot and Parquet packages were mandatory for every install. | Put them in the `reports` extra. The app uses the smaller base dependency set. |
| The monitor test expected a removed title. | Update it to the current title. |

One WSL process measurement before cleanup took **4.372 seconds** to import the server. The same import after cleanup took **1.256 seconds**. These are local startup measurements, not a strategy replay speed claim. The tests also check that server import does not load pandas or matplotlib.

The source move changes the decoded-event cache key because that key hashes decoder source files. The first named-interval load can rebuild this derived cache. Canonical events and result identities are unchanged.

## Verification

Before cleanup, the suite had **209 passing tests, 12 passing subtests, and one failure** from the old monitor title. After cleanup, the complete suite passed: **215 tests and 12 subtests**, with no skips on this machine.

The new integration test uses the real local HTTP handler and packaged worker process over blocks **109097028–109097127**. It verifies the submitted grid and request, a completed job, the returned run ID, normalized block bounds, order counts, and hash fields. Existing frontend tests execute the actual asynchronous handlers and SDK adapter.

The editable install succeeded. The installed `kuru` command works. `pip check` found no broken dependencies. The history plan command made no network calls. No historical backfill was started.

A syntax-tree comparison of all five engine modules found no changes after excluding import statements. This includes reconstruction, queues, LP flips, fills, contract rules, and shared accounting.

All three golden intervals were rerun through both named and generalized paths. The post-cleanup run used a new result-cache directory, `.cache/organization_fresh_runs/`. The complete report equals the pre-cleanup report. It also equals the saved contract-corrected report.

| Fixture | Blocks | Fills | Net P&L, USDC | Result |
|---|---|---:|---:|---|
| relatively_quiet | 109132028–109142027 | 3 | -1.7249738019380478 | Exact match |
| relatively_active | 109097028–109107027 | 11 | +2.292635767594539 | Exact match |
| directional | 109117028–109127027 | 14 | -5.088984279593537 | Exact match |

Run IDs, summary hashes, replay hashes, and semantic timeline hashes are unchanged. The recorded values are in [the golden report](../data/reports/lp_contract_regression_report.json). Canonical event and checkpoint hashes are still checked by the existing tests. These runs use prior validated data; they are not a new Envio/RPC comparison.

Commands:

```bash
python -m pip install --no-deps -e .
python -m pytest -q
kuru --help
python -m kuru_backtester history --to-finalized --download-only
python -m pip check
```

The before and after reports are saved locally as `.cache/organization_before.json` and `.cache/organization_after.json`. The temporary source and documentation check is `.cache/check_organization.py`.

## Remaining work

These findings need separate measured changes or a clear reporting definition. They were not changed during this cleanup.

1. **Repeated event scans.** Generalized range reads scan JSONL partition prefixes. Counts, timestamps, transaction budgets, and replay can require separate passes. A derived block-offset index, keyed by the canonical file hash, could reduce read and decode work. Compare all golden hashes and arbitrary overlapping intervals before using it.
2. **Job health.** The fixed 120-second stall rule can flag a slow preparation stage. Use stage-specific progress and an explicit recovery policy. A worker that has stopped sending progress must not be confused with a completed worker.
3. **Report definitions.** Time in range and capital utilization use activity samples. They are not elapsed-time-weighted. Correcting this needs a defined metric and a report-version change.
4. **Large SDK grids.** The order-count guard runs after SDK generation. A worker with a resource limit can protect the browser and server without changing the official distribution formula.
5. **Long history.** Some reporting maps retain transaction or timestamp data for the requested range. The event cursor alone does not make every report bounded in memory. Profile a large bounded interval before a lifetime replay.
6. **Earlier contracts.** The deployment implementation still needs a separate contract audit. Current known event topics do not prove all old versions are covered. Keep the reconstruction gate in place.
7. **Installation.** This remains a source-checkout app. A standalone wheel needs deliberate packaging of frontend assets and the pinned Node SDK.

Historical replay still assumes simulated orders do not change later taker behavior. This is a model limit, not a code cleanup defect. Transaction-impact warnings remain part of the result.
