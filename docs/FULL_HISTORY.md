# Historical indexing and reconstruction

This pipeline downloads Kuru market events through Envio HyperSync. It does
not simulate an LP strategy. It does not send transactions.

## Current limit

The lifetime archive command is available. **Lifetime reconstruction is not
yet verified.** The deployment runtime now has a tested reconstruction profile:
`fb70240e4197152779b6195ad0388f9d972a6c1f1945946f70b8165e6940c86b`.
The later verified implementation is
`7c7a6e72320d71112aaf58ad3604224f15bdf0734f5790cf79d2b04194c80d42`.
See [the deployment-era validation report](LEGACY_RECONSTRUCTION.md) for the
three bounded checks and the remaining limits.

In reconstruction mode, the runner stops before an event download if the requested starting block
uses unsupported code. It also stops on active vault liquidity, a parameter
change, an unknown RPC event, or any failed reconstruction gate. Do not remove
these checks to make an old range appear valid. Support for these two runtimes
does not prove that every block or every intermediate upgrade is supported.

Download-only mode can archive the currently known event topics without that
contract gate. It does not prove that those topics cover every older contract
version. Candles are exported after successful reconstruction; download-only
mode does not yet have a separate candle export step.

The three golden fixtures and LP engine retain their verified behavior.
Verification writes a separate reconstruction manifest. The verification workers
now check passing partitions from the tested LP implementation for readiness and
publish them to `historical_index_manifest.json`. They bind the exact checkpoint,
capture contract parameters, check inactive vault liquidity, and recheck boundary
hashes. Other runtimes remain reconstruction-only. Verified reconstruction alone
does not certify an LP simulation for a different contract version.

The saved-data queue records unsupported partitions and continues from an
independent checkpoint after each gap. Failed parity or integrity checks still
stop the queue. Active vault events are detected locally before RPC work.
Explicit progress ranges preserve gaps in displayed verified coverage.
See [the bounded debug results](RECONSTRUCTION_DEBUG_REPORT.md).

The main chart shows only backtest-ready data. Its separate cache never reuses
the broad archive overview. Broad local Trade discovery starts explicitly through
**Process saved data**. The chronological sections below record earlier milestones;
older statements about manual promotion and chart color bands describe those versions.

## Commands in WSL

### RPC work during verification

Timestamp checks use batches of 50 block headers. If the RPC provider rejects
the batch size, the worker reduces it and uses the smaller size for later
requests. A check of 1,073 block timestamps needs 22 HTTP requests at the
default size, compared with 108 at the old size of 10. The number of checked
blocks is unchanged. Provider billing and quotas can count each method in a
batch, so fewer HTTP requests do not guarantee a lower quota cost.

For consecutive partitions in one verification job, the worker can use the
previous verified end checkpoint as the next starting checkpoint. It checks
the checkpoint content hash, market, block, fresh boundary hash, contract code,
market parameters, and queue invariants. A gap requires a fresh RPC checkpoint.
Restarted jobs can use saved results only after the existing report and file
hash checks pass. The first unchecked partition still needs a starting checkpoint
if no checked predecessor is available in that job.

Every partition retains event parity, independent timestamp checks, seven L2
checks, cancellation checks, queue checks, a full final RPC L3 comparison, and
fresh before/after boundary hash checks. Reports and checkpoint hashes retain
their existing format. Progress now shows `rpc_timestamps` as a separate phase.

Verification workers wait at least 0.1 seconds between HTTP request starts.
They still honor `Retry-After` on HTTP 429. Optional environment settings are:

```dotenv
KURU_RPC_TIMESTAMP_BATCH_SIZE=50
KURU_RPC_MIN_REQUEST_INTERVAL=0.1
```

The batch size must be 1–100. The interval is in seconds; increase it if the
provider limits request rates. The data page reads these keys from `.env`.
For a direct CLI command, export them in its shell. These settings apply to newly started workers.
An active job keeps its loaded code and settings. No worker is restarted by a
code update. Wall-clock improvement must be measured on a new job; local tests
prove request counts and unchanged validation results, not provider throughput.

Verification of this change: 369 tests and 14 subtests passed in the full
suite. The final focused run passed 108 tests, including saved RPC captures,
unchanged validation report bytes, legacy queue metadata, invalid checkpoint
handoffs, provider batch limits, request pacing, and `.env` precedence. No new
network verification job was started for these tests.

Use the project directory and its existing virtual environment:

```bash
cd /mnt/c/Users/jackk/Documents/repos/kuru-market-making-backtester
source .venv/bin/activate
```

Start the read-only progress screen in one terminal:

```bash
python -m kuru_backtester.server.full_history_monitor
```

Open `http://127.0.0.1:8001/`. The monitor does not start a job. If the backtest
server is already running, `/indexing.html` shows the default job status too.
For a different job directory, pass `--job-root` to the monitor.

In another terminal, use a bounded first run with your `ENVIO_API_TOKEN` set:

```bash
python -m kuru_backtester.history.full_history \
  --from-block 109097028 --to-block 109097127 \
  --partition-blocks 100 --execute
```

This uses a separate `.cache/full_history/` directory and
`full_history_manifest.json`. Use the **same command** to resume. Ctrl+C stops
the process. Committed partitions and page cursors remain available.

Inspect a lifetime plan without network calls or writes:

```bash
python -m kuru_backtester.history.full_history --to-finalized
```

For a full raw event archive only, use a different job directory:

```bash
python -m kuru_backtester.history.full_history --to-finalized --download-only \
  --job-root .cache/full_history_archive --execute
```

This command can be very large. It uses uncompressed JSONL files. The recent
activity sample projects about 406 GB of canonical events, but the lifetime
event count is not measured. Allow room for temporary data. The command stops
before the next partition when less than 256 MiB is free; this is a last safety
check, not a storage-capacity guarantee.

The finalized target is fixed in `job.json`. A resumed command does not chase
the moving chain head. Market, range, partition size, endpoints, local source
hash, and manifest path must match. Use a new job directory to change them.
Each partition is at most 10,000 blocks; the default is 5,000.

Archived partitions can be reconstructed with the same range and directory:

```bash
python -m kuru_backtester.history.full_history --to-finalized --reconstruct-only \
  --job-root .cache/full_history_archive --execute
```

This does not call HyperSync. It still uses RPC for verification. The deployment
runtime is supported. Unknown runtimes, later upgrade logs, and failed checks
still stop the job.

## Verify completed downloads while other data downloads

The Data suite now has a **Reconstruct and verify downloaded data** panel.
Select a saved download, keep or narrow the displayed completed block bounds,
then press **Reconstruct / verify**. Bounds must include whole source partitions.
The last partition can be shorter than 5,000 blocks. The selected end is fixed;
the job does not automatically follow new downloads. Select the source again
to update the fields after more partitions finish.

This job needs archive RPC but does not need an Envio token. It does not
download events again. It copies one completed partition at a time to a separate
verification directory. Copies remain there with their validation reports and
checkpoints, so allow extra disk space. The source cursor and event files are
never changed. Later partitions can continue downloading. **Stop verification**
stops only this server's verification worker, not its downloader. Use the same
source and bounds to resume; passing saved partitions are checked by hash.

The request path is `data_suite.js` → `POST /api/data-suite/verify` →
`DataSuite.start_verification()` → `history.verify_download` → the existing
`full_history.validate_partition()` and promotion checks. Progress and errors
appear in the shared job banner and log panel. The dataflow diagram is removed.
Saved coverage totals can take up to one scan cycle to refresh.

Output is under `.cache/data_suite/verifications/<verification_id>/`.
The separate root manifest is `full_history_verify_<verification_id>_manifest.json`.
No partition is added until all existing gates pass. The product's
`historical_index_manifest.json` is not changed. LP contract-rule checks and
separate backtest promotion remain required.

**Do not restart the server that owns an active download just to load this
control.** Start a second app server on an unused port, or use the CLI below.
The second server can observe saved jobs and verify their completed partitions.
It cannot stop the first server's download.

```bash
python -m kuru_backtester serve --host 127.0.0.1 --port 8003
```

Alternatively, this command selects the currently completed partitions from
one archive. Replace `<download_job_id>` with the ID shown on the Data page:

```bash
python -m kuru_backtester verify-download \
  --source-job .cache/data_suite/jobs/<download_job_id> \
  --job-root .cache/verification_manual \
  --manifest full_history_verify_manual_manifest.json
```

Add `--from-block` and `--to-block` together to select whole partitions within
that archive. For a different source or range, use a different output directory
and manifest. Use `--rpc-url` to select an archive RPC endpoint. The Data page
uses `MONAD_RPC_URL` from the server environment or `.env`; the CLI's default is
the existing public RPC endpoint. Do not put secret RPC URLs in shared logs.
CLI verification has its own `progress.json`, which the standalone monitor can
show with `--job-root .cache/verification_manual`.

The deployment runtime now has a version-specific reconstruction profile. This
does not certify all older archive topics. An unsupported implementation,
active vault, parity error, incomplete cursor, changed source, or failed book
comparison stops verification and leaves the source download intact.

Verification on 2026-10-11: **270 tests and 12 subtests passed**. Tests cover
read-only source use while its writer lock is held, incomplete and changed
partitions, parity failure, successful reconstruction, resume hashes, fixed job
identity, API bounds, independent stop controls, and frontend requests. The
browser used an isolated server with fake jobs; Start and Stop reached the real
API and displayed the expected state. No live RPC verification was run for this
feature test, and no user download was stopped or restarted.

## Data path and checks

```text
Monad Mainnet → HyperSync address/topic filter → canonical local events
                                                   ├→ exact L3 queues
                                                   └→ Trade-only hourly candles
Monad archive RPC → event parity, timestamps, L2, and final L3 queue comparison
Passing checks → separate validated manifest
```

The downloader uses the existing forward `next_block` cursor and rate-limit
retry behavior. It requests related block metadata. It rejects foreign market
logs. A durable byte boundary is saved with the page cursor. Resume removes
only an uncommitted page suffix from this job's event file. A process lock
prevents two writers from using the same directory.

Each reconstruction partition uses `l3_vertical_slice.apply_event()` without
changing its queue rules. A deployment-runtime adapter also records reciprocal
flip IDs and clears the surviving link after a full fill. The later runtime
path is unchanged. It checks:

- Exact Envio/RPC event payload parity in 500-block RPC batches.
- The known deployment transaction's `Upgraded` and `Initialized` logs in a
  separate audit. These two logs are outside the downloader's market-event
  filter. Unknown events and later upgrades are not silently excluded.
- RPC timestamps for every block with a matched event.
- Seven distributed `getL2Book()` comparisons, including first and last block.
- FIFO queue invariants at each comparison.
- Exact final order IDs, previous/next links, heads/tails, and sizes against an
  independently captured RPC L3 checkpoint.
- Individual cancellation and summary-event correspondence. Summary events
  are never applied as a second cancellation.
- Contract implementation, inactive vault state, and unchanged parameters.
- Stable boundary block hashes during validation.
- SHA-256 hashes for events, reports, starting/ending checkpoints, and candles.

Every passing partition has a start checkpoint at `from_block - 1`. There is
also an empty pre-deployment bootstrap path, but it still requires the deployed
implementation to pass the supported-code gate.

Memory is bounded by one event partition and its live book, not the lifetime
event count. RPC log batches contain at most 500 blocks. The command does not
build or save a full per-event UI timeline. Extremely busy blocks can still
increase one partition's memory use. Manifest metadata grows with coverage.

## Files and candles

The job directory contains `job.json`, `progress.json`, and partition folders.
Each folder contains `events.jsonl`, `state.json`, `checkpoint.json`,
`end_checkpoint.json`, `summary.json`, and `candles.json` after successful
validation. The final export is `candles.jsonl`. A SQLite contribution table
merges hours that cross partition boundaries without counting a resumed
partition twice.

Open, high, low, and close come only from `Trade.price`. Raw prices retain their
1e18 units. Raw filled sizes are retained. MON volume is
`filled_size / size_precision`. Empty hours are not filled with invented
trades. Download-only mode does not generate candles or claim L3 validation.

The kuruLab terminal polls `/api/data-suite` for saved job state. One banner
shows active jobs. One log panel combines downloads, reconstruction, verification,
chart indexing, and backtests. It retains up to 500 actions in memory and shows
the newest 120 for the selected filter. HyperSync request ranges, returned logs,
saved cursors, skipped duplicates, and 429 waits come from worker telemetry.
Counters are not simulated. Saved worker timestamps show the age of each update.
The former dataflow diagram is removed.

The shared main chart uses `/api/market-chart`. A background reader builds
resumable minute candles from local Trade events, with no Envio or RPC request.
Each response contains at most 1,500 candles. UTC dates and colored coverage
bands distinguish missing, downloaded, verified, and backtest-ready ranges.
Incomplete indexing remains explicit. The Trade chart does not establish L3
eligibility. See [terminal details](TERMINAL_UI.md).

Older workers can supply counters without an action trace. Detailed tracing
starts with a new worker; do not stop an active download merely to change the
UI. A server that does not own a worker can show its saved progress, but cannot
stop it. Telemetry does not change the canonical event file or cursor.

Process-trace verification on 2026-10-10: **235 tests passed, 12 subtests passed**.
The observer-on/off test produced identical canonical event bytes and final
cursors. Tests cover the bounded trace, real page counts, retry delays,
duplicate reporting, and read-only observation of another server's job.
The browser showed the actual ongoing download through saved progress, with
an explicit old-worker trace notice. No download was stopped or started to
test this screen.

`.cache/` and `full_history*_manifest.json` outputs are gitignored. Existing
local raw artifacts are not deleted.

## Data suite controls

### Current combined control

Use **Download + verify** in the single preparation
panel. Check the finalized block first, select a bounded range, and confirm
the download. The range defaults to deployment through the checked head.
The target does not expand while a job runs.

The coordinator checks complete durable source states and saved validation
hashes. It downloads gaps only and reuses matching partial-job cursors.
Verification starts with all completed local unverified L3 data, including
CLI archives. Downloading proceeds at the same time. New completed partitions
are checked by later queue snapshots. A final snapshot runs after downloading
completes. The verifier never reads an incomplete source partition.

The request is `POST /api/data-suite/prepare`. Planning runs in a background
thread, not the HTTP handler. Existing download and verification worker
commands and engine checks are unchanged. **Stop** uses
`POST /api/data-suite/prepare-stop` and stops both owned workers. Failures stop
both tasks and retain local data. Passing checks do not grant LP index coverage.
No network work starts on page load or after a server restart.

The sections below record earlier separate controls and their existing
low-level API paths. Those APIs remain available. The current UI replaces
their two panels with the combined control described above.

### Initial page loading — 2026-10-12

The page waits for its initial coverage, inventory totals, and worker status
before it displays the controls. This is a data-loading screen. It does not
require a full deployment-to-present archive. The backtest page can still use
its existing validated intervals.

The inventory scanner saves its last completed result in
`.cache/data_suite/inventory.json`. A new server can display that saved result
while a current scan runs. The page shows the original check time and whether
the totals came from a saved scan. This snapshot does not grant backtest
coverage or bypass content-hash checks. Inventory snapshot files are excluded
from the displayed saved-data total.

Download coverage also uses the worker's committed `downloaded_block` on each
status request. Storage totals and verified coverage still refresh through the
existing background scan. Initial loading checks run every 250 milliseconds;
normal page polling returns to five seconds after the inventory is available.
Failed initial requests keep the loading screen visible and expose Retry.

Measured locally in WSL against 13,589 files:

- Original complete scan: **41.30 seconds**.
- Complete scan after this change: **46.47 seconds**; no claim that the disk walk is faster.
- Status from the saved inventory after a new server starts: **41.17 milliseconds**.
- Reported downloaded blocks: **31,394,003**; recorded verified blocks: **60,000**.

The first scan without a saved inventory can still take tens of seconds.
The loading screen remains visible during that scan. No download or
verification job was stopped for these measurements.

Verification: **285 tests passed, 12 subtests passed** in 73.83 seconds.
The browser check used a separate local server on port 8004. The data page
displayed measured coverage and dated saved totals. The backtest page showed
its loading screen first, then the active interval's chart, starting price,
Trade price statistics, and exact block bounds. Browser errors: **0**. The
test server and its temporary browser tab were closed afterward.

Open the Data section on the main page, or `/indexing.html` for the data-only view. Both pages use a compact terminal layout with a faint orange grid, flat controls, one main chart, one job banner, and one log panel.

- **Check finalized block** makes two narrow RPC calls: chain ID and finalized block. It saves that fixed target locally. Ordinary page polling does not query RPC or HyperSync.
- The coverage graph shows inclusive block ranges from market deployment to the checked finalized block. Until checked, the end is the latest locally known block and is explicitly labelled as such. Blank areas are not recorded. Percentages use blocks, not an invented network event count.
- **Downloaded L3** uses local canonical partition cursors and manifest event files. Trade-only discovery is a separate lane. Duplicate and overlapping ranges count once.
- **Verified L3** uses saved manifest validation and checkpoint metadata with existing files. The page does not rehash large files on each poll. The engine's normal hash and validation gates remain unchanged.
- Storage totals cover `.cache/`, `validation_output/`, `strategy_output/`, `lp_output/`, `multi_regime_output/`, `chain_data/`, and `output/`. They include retained results, old raw data, and duplicate archives, not only unique market data. Source files and SDK dependencies are excluded. Symlinks are not followed. One background scan refreshes totals at most once per minute, measured from scan completion. HTTP requests return the last snapshot without waiting for the scan. Before the first scan finishes, unknown totals show `—`, not zero. Scan errors retain the last totals and wait one minute before retry.

### Status performance correction — 2026-10-11

The old inventory scan took **20.501 seconds** over **4,289 files** on the local
WSL checkout. It held the job lock, which blocked progress requests. Its cache
timer started before the scan, so the cache had expired before the response.
Browser timeouts then caused broken-pipe errors. The error handler also tried
to send a second response to a closed connection.

The status route now starts at most one background scan. Progress and job
controls do not wait for that scan. Directory entries supply file type and
size without the old duplicate file-status calls. Both local HTTP servers
handle closed connections during headers, body writes, and flushes. Other
server errors remain visible. Failed transfers do not count as successful
backtest-result transmissions.

Read-only measurements against the same **4,289 saved files**:

- Three `/api/data-suite` calls during a scan: **21.56, 15.54, 15.33 ms**.
- Three `/api/indexing-progress` calls: **7.14, 7.61, 7.39 ms**.
- Background scan: **10.643 seconds**. These are local observations, not a
  guaranteed rate. The HTTP response no longer waits for the scan.

The frontend keeps unchanged tables and charts in place. The worker-update
age and retry countdown still update. Hidden tabs poll less often. An old
worker snapshot does not imply that a worker is running. The UI shows the
storage scan time and whether a background refresh is active.

Verification: **259 tests passed, 12 subtests passed**. Tests include concurrent
HTTP status/progress calls during a blocked scan, single-scan scheduling,
cache age after completion, failed refreshes, symlink exclusion, disconnected
clients, unchanged UI snapshots, and retry countdowns. No reconstruction,
grid, queue, fill, or accounting rules changed. No download or backtest was
started for the performance measurement.

Restart the backtest server to load the Python changes, then refresh the data
page. If a download is running, stop it through its owning server first and
resume the same range after restart. Saved pages and cursors are retained.

Enter explicit **From block** and **To block**, confirm the bounded download, then press **Start / resume download**. The server reads `ENVIO_API_TOKEN` and optional `MONAD_RPC_URL` from its environment or local `.env`. It does not send credentials to the browser.

The request path is `data_suite.js` → `POST /api/data-suite/download` → `DataSuite.start()` → `python -m kuru_backtester.history.full_history --download-only --execute`. The worker uses 5,000-block partitions and the existing address/topic filters, pagination, retry logic, durable cursor, and writer lock. No engine rules change. Starting the same range reuses the same job directory and cursor. One server-owned download can run at a time.

Jobs, logs, and cursors are saved under `.cache/data_suite/jobs/<job_id>/`. **Stop download** terminates only the worker started by this server. A restart resumes from committed pages; incomplete page bytes are repaired by the existing downloader. Closing the server also stops its worker. A different terminal's download is not stopped. After a server restart, choose a saved job's range and start it again. A worker lock prevents concurrent writers to the same archive.

The download controls download data only. The separate verification panel runs reconstruction, RPC parity, and L2 checks on completed partitions. A completed download or a separate verification manifest does not automatically add backtest coverage. The standalone monitor is read-only and disables job controls. No lifetime download was started while building this page.

### Check all completed downloads

Press **Reconstruct / verify all downloaded data**. No block fields or Envio
token are needed. The server starts a worker immediately. Archive discovery
and saved-file hash checks run in that worker, not in the HTTP request.

The worker finds full-history archive jobs under `.cache/`, including CLI
jobs. It selects only whole partitions with complete durable cursors and
matching file sizes, market, topics, and fields. It does not use the cached
inventory totals to select work. Trade-only caches are not selected.

The fixed plan skips coverage with passing reports and valid event, report,
and checkpoint hashes. It also checks end-checkpoint and candle hashes when
the report contains them. Duplicate covered partitions are skipped. A partly
overlapping partition still needs a full check; the verifier does not split
or change downloaded partitions.

The remaining ranges run in block order through the existing
`verify_download.verify()` function. That function preserves the source
files, reconstructs L3, checks RPC event parity, compares L2, and checks queue
invariants. A failed check stops the queue. Completed partitions retain their
separate verification manifests. No LP index promotion occurs.

The request path is `data_suite.js` → `POST /api/data-suite/verify-all` →
`DataSuite.start_all_verification()` →
`python -m kuru_backtester.history.verify_all_downloads` → the existing
verifier. The page shows planning, saved-hash checks, partition counts, the
current range, L2 checks, and errors.

Queue plans, progress, and `queue.log` are under
`.cache/data_suite/verification_queues/<job_id>/`. Per-range output remains
under `.cache/data_suite/verifications/<job_id>/`, with root
`full_history_verify_<job_id>_manifest.json` files. These are generated local
artifacts. No raw download is removed.

**Stop verification** also stops a queue during planning. It does not stop a
download. A page refresh or server restart does not start work. After a stop
or failure, press the all-data button again: a new plan checks saved results
and selects what remains. Downloads completed after the fixed plan require
another start. **Verify selected range** remains available for a manual check.

Checks on 2026-10-12: `python -m pytest -q` passed **310 tests and 12
subtests**. The read-only plan over the local archive found **6,271** completed
partitions, skipped **9** with valid saved checks, and selected **6,262**:

- `43660999–43930998`: 54 partitions.
- `43935999–65000000`: 4,213 partitions.
- `70030000–80000000`: 1,995 partitions.

These counts are a snapshot, not fixed targets. This check did not start the
queue, make network requests, or change downloaded data. Tests cover the UI
request, worker launch and stop, completed-state selection, duplicate ranges,
saved hashes, golden reports, discovery-manifest exclusion, failure and retry,
concurrent downloads, and source-file preservation.

Historical reconstruction now supports three exact implementation runtime
hashes. See [early-runtime corrections](LEGACY_RECONSTRUCTION.md) for the
deployment and intermediate-version checks. RPC HTTP 429 retries honor
`Retry-After` as seconds or an HTTP date. Without a valid header, the five-attempt
client waits 15, 30, 60, then 120 seconds. Exhausted retries still fail the job.
The terminal log prints each wait. Resumed verification reports the saved
reconstruction block and L2 check count, not initial zero values.

Data suite checks on 2026-10-10:

- Full suite: `python -m pytest -q` → **232 passed, 12 subtests passed**. Focused checks after the final storage scope update: **36 passed, 12 subtests passed**.
- Browser → API → local metadata: **50,000** downloaded blocks and **50,000** recorded verified blocks, both **109092028–109142027**. Total retained local data: **3.82 GiB** across the listed storage locations. These are snapshot values, not fixed constants.
- Browser finalized-block refresh: **112354686**, Monad Mainnet. Deployment-to-target scope: **68,693,688** blocks. Each local coverage lane covers about **0.073%** of that target.
- Start/stop/resume and submitted-range handling passed HTTP integration tests with an isolated fake subprocess. The real Envio download was not run: no token was available in the test server. The UI showed that condition and kept Start disabled. No large network job was launched.
- No changes were made in `engine/` or `history/`. The page uses the existing downloader and existing validation records. It does not change reconstruction, fill, or accounting behavior.

## Verification recorded on 2026-10-10

This bounded command used existing Envio events and fresh live Monad RPC reads.
It did not redownload the interval or start a lifetime backfill:

```bash
python -m kuru_backtester.history.full_history \
  --from-block 109097028 --to-block 109097127 --partition-blocks 100 \
  --local-events validation_output/relatively_active/source_events.jsonl \
  --job-root .cache/full_history_smoke \
  --manifest full_history_smoke_manifest.json --execute
```

Results:

- Blocks: `109097028–109097127`; initial checkpoint: `109097027`.
- Envio events: **839**; RPC events: **839**; missing events: **0**.
- `OrderCreated`: **307**; `OrderCanceled`: **306**;
  `OrdersCanceled`: **225**; `Trade`: **1**.
- L2 checks: `109097028`, `109097044`, `109097061`, `109097077`,
  `109097094`, `109097110`, `109097127`.
- Maximum price-level, aggregate-size, and midpoint errors: **0**.
- Queue failures: **0**; final complete L3 checkpoint match: **pass**.
- Cancellation-summary audit and timestamp parity: **pass**.
- Final book: **43 orders**, **38 price levels**.
- One hourly Trade candle; executed volume: **7009.8501496171 MON**.
- Event SHA-256:
  `d382bd8902a22c48365b999a11c6259da2b2993663362ec2ed4751ddb6c3023d`.
- Start checkpoint SHA-256:
  `3f211241d7001d2a9ac408531c7f16a123b14a0142a1d03d953c1d24bad3393f`.
- End checkpoint SHA-256:
  `afa96f7acf4ef84e9b2230b1690bcb9fb1b6ee078da74f112fb5c2a68612a7e6`.
- Candle SHA-256:
  `7d7ee4af4311dea45d19c99f0c0a31268b7fc784e877a7512d03555c4d9e3e40`.

Resume of this completed job performs no HyperSync event request. It verifies
the saved hashes and retains the same event counts. The browser shows these
real files through the monitor API; it has no sample-data fallback.

Run tests with `python -m pytest -q`. Tests cover page interruption and resume,
deduplication, process locks, empty pages, deterministic reports, Trade candles,
hour-boundary merging, parity/timestamp/queue/vault failures, download-only
coverage isolation, monitor HTTP paths, and telemetry.

Metropolis eligibility is not certified by this implementation. The project
uses Monad Mainnet data, but the authenticated submission rules still need to
confirm whether an analytics app without its own deployed contract qualifies.
