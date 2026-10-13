# kuruLab terminal

The product uses one full-width grid. The main chart, LP inputs, result metrics,
data controls, and job logs share the page. There is no named-regime selector
or dataflow diagram. The existing named intervals remain regression fixtures.

## Chart and data path

`terminal.js` → `GET /api/market-chart` → `MarketChart` → local event JSONL files
→ Trade-only candles → `terminal_chart.js` → TradingView Lightweight Charts.

The pinned chart version is **5.1.0**. The browser bundle is local. Rebuild it
with `npm run build:charts --prefix sdk_reference`. The Apache license and
TradingView notice are in `frontend/chart-license.txt`.

Each current job has its own full-width banner, stacked vertically below the
main header. Each shows its title, status, progress, and a short summary.
Details expand on request and remain open during status updates. A verification
queue and its active child appear in one banner. The Jobs button opens saved
history in a modal. The floating log contains technical activity only. Chart
height adjusts to keep the date axis above that panel.

The default candle is one UTC day. Weekly, hourly, and minute intervals are also
available. Weeks start on Monday at 00:00 UTC. Weekly gaps use whole weeks,
not six daily spacer slots between consecutive weekly candles.
Zoom reads the local index; it does not request network history.
Viewport filtering retains the complete selected UTC candle bucket. Responses
contain at most 1,500 candles; very wide minute ranges can use larger buckets.
No candle is created from a midpoint. Empty periods have no invented Trades.

The background reader caches each source file's minute candles. Cache identity
contains its relative path, file size, modification time, and selected blocks.
Overlapping source blocks are read only once. A changed source gets a new cache
entry. The chart shows indexing state and errors. A partial chart is not a
claim of complete archive coverage. These display caches do not replace the
hash and RPC checks used for validated backtest data.

The main chart reads verified partitions, including older contract versions
without LP approval. It checks event and checkpoint hashes and passing reports.
Each completed partition enters the chart before the full queue finishes.
The main chart never reads unverified downloads on page load. A saved daily
overview can open when its source identity matches. Prepared responses are reused.
New manifest entries trigger a local refresh. Appended entries retain existing
candles during refresh; removed or changed entries clear the affected view.
**Process saved data** starts broad local Trade discovery separately. It builds
archive candles but does not display unverified history in the main chart.

**Navigate** enables drag-to-pan and wheel zoom. **Select period** enables drag
highlighting. Background updates retain the current view; only **All data**
explicitly resets it. Detailed zoom responses retain the surrounding history.
Requests pause during gestures. Status-only polls do not redraw candles or replay
orders. Replay rendering processes new frames incrementally and retains unchanged
order lines.

Chart checks on 2026-10-12: the same 147 daily candles took **131.4, 129.5,
201.2 ms** through the old endpoint and **12.0, 6.0, 4.6 ms** through the prepared
overview path. These are local HTTP timings, not animation frame-rate results.
After full indexing completed, the prepared response took **13.7, 6.9, 4.7 ms**.
The full regression suite passed **330 tests and 12 subtests**. Final replay-pane
changes also passed the focused UI suite. This earlier version used a P&L pane
on the history chart. Replay now uses separate Canvas charts, described below.
Browser checks confirmed that drag-to-pan retained the block inputs, wheel zoom
worked, and the date axis remained above the floating log. The existing validated
test position still returned run `lp-9b529fa65b9fe2acb9f8`, **+1.5850 USDC**, and
**1,232 frames**. The final replay frame rendered with no console errors.

Chart selection:

- Orange shading and border: selected period.
- Faint orange grid: price and time guides, not data status.
- Coverage color bands are removed. All displayed Trades belong to verified
  partitions. LP eligibility is checked separately when selecting a backtest.

Drag selects blocks from Trade candle anchors. Dates are approximate when a
block has no Trade. Exact block inputs remain visible. Use smaller candles or
the block fields for a period within one day. The backend rejects a selected
range without continuous validated L3 coverage.

## Incremental candles and job trace — 2026-10-13

The overnight worker was progressing. Its active group had completed 781
partitions and about 5.7 million events when inspected. Checkpoint reuse was
enabled. One recent partition spent about 70 seconds reading RPC timestamps;
another spent about 5 seconds. This is variable archive RPC time, not candle
generation. Transport retries and waits now have explicit trace records.

The missing candles had two causes: the main chart required LP-approved runtime
rules, and the Trade coverage lane counted only the original discovery manifest.
The chart now reads passing verification manifests while the worker continues.
It retains the independent LP eligibility gate. The original golden reports use
their approved primary manifest's hashes; new verification reports must also bind
the event and checkpoint hashes. Changed or failed sources are excluded.

A local cold scan published its first partial candles within 3 seconds. The full
initial scan took about 162 seconds while the existing verifier continued.
Subsequent refreshes reuse per-file candle contributions and session hash checks.
Adding manifest entries keeps the displayed candles and current viewport.

Live API checks returned 26 daily candles from 133,421 verified Trades in
28.28 ms. Indexed chart ranges matched saved verified ranges at that sample:
43,660,999–44,850,998; 44,855,999–48,885,998; 70,000,000–70,029,999;
109,092,028–109,147,027. LP coverage remained 109,092,028–109,147,027.
These are observations during a running job, not fixed coverage claims.

The Jobs modal loads retained downloads, verification queues and workers,
backtests, and preparation jobs. New preparation jobs retain individual records.
Historical records do not generate duplicate activity entries. A queue and its
child share one banner and one worker trace. Recent external worker activity is
labelled explicitly; the server does not claim control of another process.

New workers emit method counts, RPC response time, HTTP retry waits, checkpoint
identity and reuse, source event counts, parity windows, timestamp progress,
getL2Book checks, cancellation audits, full ending L3 comparison, and content
hashes. RPC URLs and credentials are excluded. Old workers keep their existing
counter and phase trace until restarted; they are not changed in place.

Checks: the complete suite passed 380 tests and 14 subtests in 107.28 seconds.
After the final compatibility and refresh changes, 111 focused tests and 14
subtests passed. Browser checks confirmed partial candles during indexing,
separate current-job banners, the Jobs modal, and a technical activity log.

To view the new server while an old server owns a running verifier, use a
different local port, for example `python -m kuru_backtester serve --port 8005`.
Do not start a second verification queue. The new page can display the existing
worker as external. Restarting the owning server stops its managed workers;
resume verification later to load the new RPC trace code.

Backtest results use the existing engine and compact replay. The original
cached Canvas renderer now draws a separate price chart and portfolio-value
chart. Only best bid and best ask appear as price lines. Fill dots use their
recorded execution price and timestamp. The detached dashed order lines and
midpoint line are removed. Both charts use elapsed time, not compact-frame
index, for their horizontal position. Strategy and hold values appear below.
Fill prices can differ from the recorded post-trade book quotes; markers are
not moved onto a quote line to hide that difference. Playback and seeks
reveal a cached layer prefix. They do not change the historical candle chart
or make backend requests. Results are hidden before a run is complete or loaded.
Changing the configuration clears and hides the old result.

The left workspace is now an independent column. Data controls follow the
history chart immediately, apart from the short status footer, when results
are hidden. The LP input column no longer reserves three rows or exposes a
gray empty results cell.

### Sharpe diagnostic

The UI adds a **replay estimate**, not a new canonical engine metric. It samples
marked portfolio value on UTC one-minute boundaries. Each boundary uses only
the preceding compact frame, with no future interpolation. Simple returns are
`equity_now / equity_previous - 1`. The ratio is mean return divided by sample
standard deviation, with a stated zero risk-free rate. It is not annualized.
This follows [Sharpe's definition](https://web.stanford.edu/~wfsharpe/art/sr/SR.htm).

At least 30 complete returns are required. Zero volatility, non-positive
equity, or missing timestamps return no value and a reason. Short runs remain
weak evidence even when the minimum count passes. Downsampling can miss marks;
this compact-replay estimate must not be treated as an exact full-event statistic.
The diagnostic does not alter strategy accounting, saved artifacts, or hashes.

Checks on 2026-10-12: **356 tests and 14 subtests passed**, in 88.99 seconds.
The browser measured hidden results height as **0 px**. Data controls followed
the history chart with only its **21.6 px** status footer between them.
The saved run `lp-9b529fa65b9fe2acb9f8` retained **+1.5850 USDC** and
**1,232 frames**; its diagnostic was **0.157** from **49** one-minute returns.
There were no browser warnings or errors. Local-processing HTTP tests used
controlled worker processes. No download or full RPC verification was started
for these checks. The temporary UI test server was stopped after verification.

## Saved results and header

Open **Previous backtests**, select a completed local run, and press **Load
results**. `GET /api/saved-backtests` lists compact summary metadata, not raw
timelines. `GET /api/saved-backtests/<run_id>` checks completion, summary, and
timeline hashes, then returns the saved result. The normal server access checks
apply. No owner token is exposed. Loading restores input values and loads the
same result renderer as a fresh run. A changed input rejects a late load reply.
Incomplete or damaged summaries are omitted. Damaged timelines fail visibly.
The cache is `.cache/backtest_runs/`; source files and saved artifacts remain
unchanged.

Browser check on 2026-10-12: the saved run
`lp-9b529fa65b9fe2acb9f8` loaded with **+1.5850 USDC** and **1,232 frames**.
100x playback advanced the Canvas charts. The final seek reached block
**109107026**. Editing the fee hid the old result. The weekly history request
returned **25** candles with no daily spacer bars. There were no browser
warnings or errors. No simulation or network download was started. The temporary
test server was stopped after these checks.

Complete suite: **351 tests and 14 subtests passed**, in 82.38 seconds.
Tests cover saved hash checks, incomplete results, server access checks,
late saved replies, Canvas forward/backward playback and cached layers,
weekly spacing, measured progress bars, and live verified coverage.

The header uses the original kuruLab text wordmark. The favicon uses
`Kurulabnobg.png`. The README starts with `kurulabbanner1.png`.
The oval wallet pill is not a button or link. Its hover text says wallet and
execution support are coming soon. It does not connect a wallet.

The job banner and log use bold task names, active status dots, and progress
bars. Known totals use measured block, partition, file, or event counts.
Unknown totals use an animated bar. No estimated percentage is invented.
Verified coverage can use newly committed `validated_block` progress before
the inventory scan catches up. The LP eligibility gates remain unchanged.
Preparation range fields are inside a closed details menu. The default range
is deployment through the checked finalized block. Custom fields remain
available. A network confirmation is still required.

**Process saved data** starts the existing RPC verification queue and requests
a local Trade candle refresh. It needs no Envio token, finalized-head lookup,
or download confirmation. Completed partitions only are eligible. Archive RPC
is still a network source for checks; this button is not an offline verifier.
It does not issue an Envio Trade-discovery scan. The request is
`POST /api/data-suite/prepare-local`. **Stop** stops verification for this mode;
the normal read-only candle reader can continue. In a combined job, Stop still
stops both managed download and verification workers.

Queue progress now includes promoted partitions within the current group.
The on-disk group counter and source files remain unchanged. In the saved job
record examined for this change, **904,767** events had been verified and the
current worker had reached **7/7** L2 checks. The old queue display still showed
zero because its 4,183-partition group had not finished. Verification uses
archive RPC, so it does not consume Envio download requests.

## Jobs and network use

One banner and one log panel show actual saved job state. The panel combines
downloads, verification, local candle indexing, and backtests. The log keeps
up to 500 actions in memory and shows the newest 120 for the selected filter.
The slightly transparent log panel stays at the bottom of the screen. Use
**Minimize** to reduce it to a small bar, or **Expand** to open it again. The
browser remembers this choice. The top **Logs** link also opens the panel.
Job updates continue while the panel is minimized.

Chart cache writes use a private temporary file for each writer. This prevents
two local servers from using the same `.json.tmp` file and failing during its
rename. Each complete file replaces the cache entry atomically. Failed writes
retain the previous entry and remove only their own temporary file. The chart
does not need a new download after this correction.

Only downloads use HyperSync. Reconstruction reads local event files and uses
archive RPC for independent parity, L2, timestamp, and checkpoint checks.
Therefore a reconstruction-only job need not use any HyperSync allowance.
The UI does not start a download or verification queue on page load.

## Cancellation-audit correction — 2026-10-12

The reported partition **43,960,999–43,965,998** failed before RPC parity because
the audit assumed an exact match between individual cancellations and summary
IDs. At block **43,961,137**, transaction
`0xbf502ea1ffa859dc05c0c575c4d53015c876e6c85da789f42790b04094e34371`
contains 32 `OrderCanceled` events and one `FlipOrdersCanceled` event with 31
requested IDs. Order **2750** is the reciprocal partner of requested order
**1304**. Both links exist in the starting checkpoint.

The corrected audit accepts the extra cancellation only with reciprocal pair
evidence from the reconstructed book in that same transaction. Missing summary
targets, unknown pairs, wrong-transaction evidence, and duplicate individual
cancellations still fail. Summary IDs are never applied as extra cancellations.
The queue and fill rules are unchanged.

A bounded live RPC check passed:

- Saved Envio events: **33**; RPC events: **33**; missing events: **0**.
- L2 blocks: **43960999, 43961832, 43962665, 43963498, 43964331, 43965164, 43965998**.
- L2 checks: **7/7**; maximum price-level, aggregate-size, and midpoint errors: **0**.
- Queue failures: **0**; final complete L3 checkpoint: **match**.
- Automatic paired cancellations accepted: **1**; duplicate mutations: **0**.

The capture is `tests/fixtures/paired_cancellation.json.gz`. Its SHA-256 is
`6455175ca61e47f200b99f8835177f60d20c2e5840768903cc7e0445dc381ea5`.
The regression test replays the full captured RPC responses with no network.
The check did not start the full queue, alter the downloaded archive, or promote
this range into LP backtest coverage. Existing failed job records stay visible.
After a server restart, use **Download + verify**.
Previously verified ranges are checked and skipped.

## Combined data preparation

One panel now controls downloads and verification. Its range defaults to
deployment through the checked finalized block. The upper bound is fixed for
each job. A confirmation is required before network work starts.

The HTTP handler starts a background coordinator. It reads complete durable
partitions and checks saved verification hashes. It downloads only gaps and
resumes existing partial archive jobs with their original range and cursor.
The first verification queue processes the saved backlog. The downloader can
run at the same time. After each queue completes, another queue checks new
completed partitions, with a 30-second idle wait while downloads remain active.
A final queue runs after the last download completes. No incomplete partition
is read by the verifier. The reconstruction engine and its checks are unchanged.

Stop affects both children owned by this server. An error stops the combined
job and preserves local source data. Restarting the server does not restart
work. Saved progress is shown as unmanaged until the user starts again.
Archive RPC and extra disk space are required. Verified archive coverage does
not bypass the separate LP index promotion gates.

Request path: `data_suite.js` → `POST /api/data-suite/prepare` →
`DataSuite.start_preparation()` → background coordinator → existing
`full_history --download-only` and `verify_all_downloads` workers.
`POST /api/data-suite/prepare-stop` stops this combined job. The earlier API
routes remain available for CLI and compatibility tests, not as duplicate UI
panels. Pipeline progress is saved in `.cache/data_suite/preparation.json`.

Checks on 2026-10-12: `python -m pytest -q` passed **339 tests and 12
subtests** in 77.98 seconds. The seven coordinator tests also passed after
the final gap-planning change. The Node UI test passed. The tests used local
HTTP requests and controlled child processes. No full network download or
live archive verification was started for this change.

## Tests

Weekly candles are available on both chart pages. They start on Monday at
00:00 UTC. Daily candles remain the default. Weekly OHLC and volume use only
Trade events. Empty weeks have no invented candles. The saved daily overview
can also form weekly candles while the minute index loads. Tests compare
that result with the full Trade index and check week boundaries, full-week
zoom values, volume, and block anchors.

Run `python -m pytest -q`. Chart tests cover Trade-only OHLC, daily boundaries,
empty periods, overlapping files, resumable caches, foreign logs, bounded
responses, UTC labels, drag selection, and timestamp-correct fill markers.
Existing SDK, queue, reconstruction, accounting, and asynchronous request tests
remain in the suite.

Complete suite on 2026-10-12: **325 passed, 12 subtests passed**.
Refresh regression: the existing broad chart stays visible until its next
background refresh is complete. First-time indexing can still show partial data.

The verification skill guided the browser-to-engine check. It used the actual
local request path, not a sample replay:
Spot, blocks **109097028–109107027**, absolute prices **0.027–0.0273**,
fee **1%**, **10,000 MON**, and **1,000 USDC**. The backend result reached the
new terminal with **1,232 replay frames** and displayed **+1.5850 USDC** P&L.
This was a UI check, not strategy tuning or a return forecast.

- Run: `lp-9b529fa65b9fe2acb9f8`.
- Summary SHA-256: `ccbf962361613d64197f7548c5315137ea3ab5344f26356713c31f0de99d4f72`.
- Timeline SHA-256: `b3c0376e95cf604fce78a8f0268df4be54c4f83c33b1aedb5f508097ee5c1d57`.

The browser showed the normalized configuration, completed metrics, and final
replay frame at block **109107026**. Its console had no warnings or errors.
The local candle API returned real downloaded Trade data while its background
index was still incomplete. No full download or verification queue was started.
