# Historical indexing and reconstruction

This pipeline downloads Kuru market events through Envio HyperSync. It does
not simulate an LP strategy. It does not send transactions.

## Current limit

The lifetime archive command is available. **Lifetime reconstruction is not
yet verified.** The market at deployment block `43660999` uses implementation
SHA-256 `fb70240e4197152779b6195ad0388f9d972a6c1f1945946f70b8165e6940c86b`.
The current verified implementation is
`7c7a6e72320d71112aaf58ad3604224f15bdf0734f5790cf79d2b04194c80d42`.

In reconstruction mode, the runner stops before an event download if the requested starting block
uses unsupported code. It also stops on active vault liquidity, a parameter
change, an unknown RPC event, or any failed reconstruction gate. Do not remove
these checks to make an old range appear valid. A separate historical contract
parity task is required before deployment-to-present L3 coverage can pass.

Download-only mode can archive the currently known event topics without that
contract gate. It does not prove that those topics cover every older contract
version. Candles are exported after successful reconstruction; download-only
mode does not yet have a separate candle export step.

The original `historical_index_manifest.json`, three golden fixtures, and LP
engine remain unchanged. The new manifest is separate. Its partitions are not
automatically added to the backtest app. The existing LP contract-rule checks
also remain in place. Verified reconstruction alone does not certify an LP
simulation for a different contract version.

## Commands in WSL

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

Once the older implementation has passed a separate parity audit, the archived
partitions can be reconstructed with the same range and directory:

```bash
python -m kuru_backtester.history.full_history --to-finalized --reconstruct-only \
  --job-root .cache/full_history_archive --execute
```

This does not call HyperSync. It still uses RPC for verification. It currently
stops at the unsupported deployment implementation.

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
changing its rules. It checks:

- Exact Envio/RPC event payload parity in 500-block RPC batches.
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

The monitor polls `/api/indexing-progress` once per second. It displays actual
requests, pages, saved bytes, decoded response bytes, rate-limit waits, blocks,
event-name counts, live orders, price levels, and validation status. Response
bytes are an estimate of decoded JSON size, **not measured network traffic**.
Request counters describe the current process. Event counters include resumed
partitions. A highlight occurs only when a new process snapshot arrives. A
lost connection or old heartbeat is shown explicitly.

The progress screen uses a minimal dark research layout with cosmic-orange
marks. Small data pulses move only when real counters increase. Idle states,
rate-limit waits, failed jobs, and lost connections do not show a fake stream.
The first loaded snapshot does not replay old work as new activity. The screen
respects reduced-motion settings. Diagnostic text is under a closed Details
section. Chart values still come only from actual Trade candles.

`.cache/` and `full_history*_manifest.json` outputs are gitignored. Existing
local raw artifacts are not deleted.

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
