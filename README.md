# Kuru Market Making Backtester

## Documentation map

- [Codebase guide](CODEBASE_GUIDE.md): module ownership, units, ordering, invariants, strategy rules, and test coverage.
- [General historical system](HISTORICAL_SYSTEM.md): block-range architecture, coverage, indexing, golden parity, and lifetime size estimate.
- [Generalization results](GENERALIZATION_RESULTS.md): golden parity, browser evidence, tests, current gaps, and storage estimates.
- [Performance and accuracy audit](PERFORMANCE_ACCURACY_AUDIT.md): two full static review passes, prioritized findings, test gaps, and a safe optimization plan.
- [Live Kuru LP request path](BACKTEST_REQUEST_PATH.md): form-to-API-to-engine-to-replay behavior and integration results.
- [Official SDK reference](SDK_REFERENCE.md): pinned Kuru SDK version and fixture process.
- [Kuru LP results](KURU_LP_RESULTS.md): validated LP vertical-slice results.
- [Multi-regime results](MULTI_REGIME_RESULTS.md): fixed-parameter regime and participation comparison.
- [Reconstruction plan](PLAN.md): historical reconstruction design and milestones.

The generated browser SDK bundle is not hand-maintained. Edit `sdk_reference/browser_adapter.js`, then rebuild `frontend/kuru_sdk_adapter.js`.

This project has data, reconstruction, backtest, and replay tools for the Kuru MON-USDC market:

- `analyze_l2.py` downloads one published Kuru L2 Parquet file and makes a midpoint and spread chart.
- `rebuild_from_chain.py` rebuilds the market history from Monad event logs.
- `discover_interval.py` finds small ranges with selected Kuru events.
- `discover_envio_intervals.py` makes one resumable Envio pass and analyzes overlapping windows locally.
- `l3_vertical_slice.py` runs an Envio-first L3 queue replay, RPC parity checks, historical L2 checks, and one passive-order simulation.
- `kuru_lp.py` runs the Kuru concentrated-liquidity LP vertical slice with the official SDK grid rules.
- `historical_system.py` provides local event, checkpoint, reconstruction, cursor, and validation components for arbitrary covered block ranges.
- `index_history.py` downloads one bounded, resumable canonical event partition into unvalidated staging storage.

The market address is `0x065c9d28e428a0db40191a54d33d5b7c71a9c394`.

The project has a local replay frontend. It does not have wallet connection or trade execution.

The [full L3 backtesting plan](PLAN.md) describes the Envio ingestion, FIFO queue reconstruction, verification, historical replay, and liquidity-provider backtesting milestones.

## Why some daily files are missing

A date in the file name does not mean that an object exists for that date. The Kuru public S3 bucket returns only files that Kuru uploaded. The bucket does not make a file when a user requests a new date. If the object key is absent, S3 returns an error.

For example, a request for `2026-09-11` fails because the bucket has no object for this market and date. The downloader cannot calculate that file from other daily files.

The chain rebuild removes this dependency. It reads the market events from Monad and makes local historical data.

## Setup

Python 3.11 or a later version is required.

```bash
python -m venv .venv
```

Activate the environment on macOS or Linux:

```bash
source .venv/bin/activate
```

Activate the environment on Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
```

Install the project:

```bash
python -m pip install -e .
```

Install the pinned official SDK and generate the parity fixtures:

```bash
npm install --ignore-scripts --prefix sdk_reference
npm run fixtures --prefix sdk_reference
npm run build:browser --prefix sdk_reference
```

## Run the Kuru LP vertical slice

The default LP run uses the verified relatively active interval, blocks `109097028` through `109107027`. It uses the saved Envio event stream and the RPC checkpoint at block `109097027`.

```bash
python kuru_lp.py
```

The command-line default initial position is:

- Spot shape;
- price range `0.0263` through `0.02795` USDC per MON;
- 0.30 percent fee and flip spread;
- 2,000 MON;
- 60 USDC;
- one-block initial inclusion delay;
- 10 percent maximum participation in each historical transaction.

The command generates the position with the integer rules from `@kuru-labs/kuru-sdk` version `0.0.95`. It stops if the generated prices, sizes, or flip prices do not match the SDK fixture. It puts each initial order at the historical queue tail. A partial fill creates or updates the paired opposite order with the official contract flip rules. The LP engine does not do periodic cancel and replace.

The raw timeline and small report files are under `lp_output/relatively_active/spot_max_10_percent/`. The raw timeline is ignored by Git. Build the compact browser file with:

```bash
python build_lp_frontend.py
```

See [the Kuru LP vertical-slice report](KURU_LP_RESULTS.md) and [the pinned SDK reference](SDK_REFERENCE.md).

## Run the verified L3 vertical slice

The selected range is block `44,853,800` through block `44,856,100`. It contains order creation, trades, individual and batch cancellation events, flipped-order events, and vault updates.

Envio HyperSync is the primary event source. Set an Envio token in the process environment:

```bash
export ENVIO_API_TOKEN=your_token
python l3_vertical_slice.py
```

PowerShell:

```powershell
$env:ENVIO_API_TOKEN = "your_token"
python l3_vertical_slice.py
```

The default command downloads the same market logs from Envio HyperSync and Monad RPC. It compares every address, block position, transaction hash, topic, and data field. It stops if one event differs.

The recorded run for blocks `44,853,800` through `44,856,100` passed with 598 events from Envio, 598 events from RPC, and zero missing events. All nine full-L2 checks had zero error.

It then:

- loads the exact contract queues at block `44,853,799`;
- checks all previous and next links, queue heads and tails, totals, and reachability;
- replays events in block, transaction, and log order;
- compares every explicit L2 price and size with historical `getL2Book()` at several blocks;
- moves a replay cursor through all events;
- places one simulated passive order at a real queue tail and calculates a counterfactual fill and gross marked P&L.

For development only, RPC can supply the events:

```bash
python l3_vertical_slice.py --rpc-only
```

This option still does the historical queue and L2 checks. It does not prove Envio parity. See [the recorded vertical-slice results](VERTICAL_SLICE_RESULTS.md).

Generate the separate reconstruction validation report and deterministic replay output:

```bash
python reconstruction_validation_report.py
```

This writes charts, numerical checks, `summary.json`, `timeline.jsonl`, and an artifact manifest under `validation_output/`. The report does not change the reconstruction engine and does not add playback delays or a frontend.

## Run the parameterized strategy slice

The strategy slice refuses to run unless `validation_output/summary.json` records passed Envio/RPC parity and zero reconstruction error for the same interval.

Run the default configuration:

```bash
python strategy_vertical_slice.py
```

Set cash, inventory, quote offsets, order size, inventory limits, cancel/replace threshold and fees:

```bash
python strategy_vertical_slice.py \
  --starting-cash 1000 \
  --starting-inventory 1000 \
  --bid-offset-bps 10 \
  --ask-offset-bps 10 \
  --order-size 200 \
  --minimum-inventory 0 \
  --maximum-inventory 2000 \
  --replace-threshold-bps 1 \
  --maker-fee-bps 5 \
  --fixed-action-fee 0.001 \
  --inclusion-delay-blocks 1 \
  --material-impact-threshold-percent 25
```

Use `--no-cancel-replace` to keep active quotes until they fill or an inventory limit requires cancellation.

The default inclusion delay is one block. An order placed after block N cannot fill from an event in block N. The engine reserves cash for bids and MON for asks. Active orders cannot use the same capital twice.

The replay assumes that simulated orders do not change historical taker behavior or later market events. Each fill keeps its per-event percentage as a diagnostic value. The run-level material-impact value groups all Trade events and simulated fills by transaction hash. The default material-impact threshold is 25 percent.

Use an optional transaction participation cap for a separate causal replay:

```bash
python strategy_vertical_slice.py \
  --max-transaction-participation-percent 10
```

The uncapped default keeps the prior fill behavior. For a capped run, the engine fixes one budget from the total Trade volume in each atomic transaction. It consumes that budget in log order. An unfilled order remainder stays active and keeps its normal queue state.

The backend completes the full replay before it returns. It writes `summary.json`, `timeline.jsonl`, and `manifest.json` under `strategy_output/`. The timeline has no playback delay. A future frontend can control playback speed without rerunning the strategy.

See [the recorded strategy-slice results](STRATEGY_SLICE_RESULTS.md) and [the causality and accounting audit](STRATEGY_ENGINE_AUDIT.md).

## Find larger intervals with Envio

Set your Envio token in the same terminal that runs the command:

```bash
export ENVIO_API_TOKEN="your-token"
```

Run a bounded two-million-block scan. This command does not backfill the chain and does not run the strategy:

```bash
python discover_envio_intervals.py \
  --from-block 44856101 \
  --to-block 46856100 \
  --window-blocks 10000 \
  --step-blocks 10000 \
  --minimum-trades 20
```

Envio supplies the filtered market events and related block timestamps. The command prints the selected market at startup. The broad-range log filter contains the normalized market address and only the Trade topic0 value. The request also selects the returned log address, and each cached event stores that address. The command stops if any returned log has a different address.

The request explicitly selects the other log fields and the block `number` and `timestamp` fields. It keeps the default log-to-block join and does not request all blocks. For each response page, the command maps every returned block number to its timestamp before it processes any logs. Block and log array positions do not need to match.

The broad scan follows Envio's `next_block` cursor in one forward pass. It stores unique Trade events and the next cursor under `.cache/envio_interval_discovery/`. The cache identity contains schema version 4, market address, Trade topic set, block range, selected fields, and HyperSync URL. Version 4 invalidates the current version 3 cache and preserves its files with a `.stale-v3.0-*` suffix. The 218,578-event cache cannot be reused as Trade-only data.

If a related block is missing, the command prints the response batch keys and requests only that block's timestamp. It does not commit the page cursor unless that timestamp lookup succeeds. If the command stops, run the same command again. It resumes from the saved cursor.

After the Trade fetch is complete, the command computes all overlapping 10,000-block windows with 5,000-block steps from the local cache. It does not send one request for each analysis window. It ranks windows with Trade count, summed Trade `filled_size` in base units, and first-to-last Trade price movement. It selects three separate windows: relatively quiet, relatively active, and directional.

Only after selection does the command request the dense order-event topics. It requests them only for the three final candidate ranges and stores them in separate bounded caches. It prints and records each event name, topic0 hash, and count. The report contains block bounds, observed duration, Trade count, executed volume, order-event count, and Trade-derived price movement. It writes `envio_interval_candidates.json`.

For HTTP 429, the command prints the available rate-limit headers. It uses `Retry-After` or a rate-limit reset value when the server supplies one. Otherwise, it uses bounded exponential backoff with jitter. It saves the current cursor before each wait and before a final rate-limit exit.

Run a smaller 50,000-block smoke test with the same explicit bounds:

```bash
python discover_envio_intervals.py \
  --from-block 109092028 \
  --to-block 111092027 \
  --smoke-test \
  --smoke-test-blocks 50000 \
  --output envio_interval_smoke.json
```

The command requires both block bounds. It rejects a scan above 2,000,000 blocks unless you explicitly change `--max-scan-blocks`.

The accepted smoke-test intervals are stored in `interval_manifest.json`. The first processed interval is `relatively_active`, blocks `109097028` through `109107027`. Run its reconstruction validation with:

```bash
python reconstruction_validation_report.py \
  --interval relatively_active \
  --validation-points 7
```

This command writes to `validation_output/relatively_active/`. It does not overwrite the original verified slice. It replays the cached Envio payloads in block, transaction, and log order and verifies them against bounded RPC log requests.

Run the default strategy on the passing validation:

```bash
python strategy_vertical_slice.py \
  --validation-summary validation_output/relatively_active/summary.json \
  --output-dir strategy_output/relatively_active
```

The recorded validation and strategy results are in `RELATIVELY_ACTIVE_RESULTS.md`.

Run the final fixed-parameter evaluation across all three verified regimes and four participation scenarios:

```bash
python multi_regime_evaluation.py \
  --validation-root validation_output \
  --output-root multi_regime_output
```

This command requires passing named validation reports for `relatively_quiet`, `relatively_active`, and `directional`. It writes one `comparison.json` file and 12 separate raw timelines. See [the recorded multi-regime results](MULTI_REGIME_RESULTS.md).

## Run the frontend replay

The current product runs Kuru LP only. The Advanced MM demo and advanced simulation controls have been removed. Configure the market, block range, shape, price range, spread, MON amount, and USDC amount.

See [the current LP contract checks](LP_CONTRACT_PARITY.md) for corrections, test evidence, fixed replay assumptions, and known limits. Contract mechanics are checked; exact counterfactual trader behavior is not claimed.

See [the cleanup review](CLEANUP_REVIEW.md) for current maintenance fixes and remaining risks. The [interval expansion plan](ANY_INTERVAL_PLAN.md) describes Trade-derived hourly candles and bounded on-demand L3 preparation. That preparation service is not implemented yet.

Before the first run, capture the historical contract limits and rebates:

```bash
python prepare_lp_contract_rules.py
```

This bounded command reads the six existing checkpoints. Interactive runs use its local cache. The grid reference is the market state before the first selected block. Initial orders become eligible at the start of that block. Contract-created flips are available to later matching events without an extra block delay. There is no artificial participation cap. Gas is excluded from P&L. Material-impact warnings explain the limits of replaying unchanged historical trader activity.

Build the compact client artifacts from the existing raw timelines:

```bash
python compact_replays.py
```

Start the local frontend and backtest API:

```bash
python backtest_server.py --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000`. Kuru LP is the default mode. Select any block range inside one complete validated L3 segment and edit the position. **Run backtest** sends the market address, `from_block`, `to_block`, exact form values, and exact preview grid to the local API. A named regime is not required. The API reconstructs the requested start from a local verified checkpoint and local indexed events. It does not call Envio or RPC. Playback stays client-side. Changing playback speed does not run Python or recompute the backtest.

If the browser runs on Windows and the server runs in WSL, WSL localhost forwarding might be unavailable. In that case, start the server with:

```bash
python backtest_server.py --host 0.0.0.0 --port 8000
```

The server prints complete access URLs with its detected IP addresses. Open the WSL IP URL in the Windows browser. The URL sets a private browser access cookie. Keep the server terminal open. Use `ss -ltnp | grep ':8000'` to find a process that still uses the port.

The server permits two active backtests by default. Use `--maximum-workers` to change this limit. Extra submissions receive HTTP 429. Server shutdown stops its active workers.

The history chart is made from Trade events. Complete validated L3 coverage is continuous from block 109092028 through block 109142027. The API accepts any interval inside these inclusive bounds and rejects ranges outside local validated coverage. The old quiet, active, and directional names remain as convenient golden presets only.

The block-range request path is:

```text
HistoricalEventStore -> CheckpointStore -> ReconstructionEngine -> ReplayCursor -> KuruLpBacktestEngine
```

See [the general historical system guide](HISTORICAL_SYSTEM.md) for current coverage and indexing rules.

The primary range inputs are percentages below and above the midpoint immediately before the selected interval. The UI calculates the absolute limits with integer math. It rounds the lower limit down to a tick and the upper limit up to a tick. It shows the reference midpoint and final absolute limits. The range can also be entered as absolute prices.

The API uses isolated local jobs. `POST /api/backtests` returns a job ID. The browser polls progress and gets the completed result from a separate endpoint. You can cancel a queued, running, or stalled job. An old browser request cannot hold a global engine lock or replace a newer result.

The preserved 2,500-order regression workload now completes through the warm API path in 3.18 seconds on the measured host. It processes 98,178 events, 2,500 initial orders, and 444 fills. The event cache loads in 0.93 seconds. The official SDK grid takes 0.03 seconds after the shared Node.js worker is warm. The strategy simulation uses 1.91 seconds. The ordered fill hash and compact timeline hash are unchanged. See [the runtime diagnostic report](PERFORMANCE_DIAGNOSTIC_REPORT.md) for baseline data, cold and warm results, memory, parity hashes, and limits.

The first run after a server start also starts the official SDK worker. Later jobs share that worker, but each strategy still runs in its own Python process. Decoded events use a versioned SQLite cache in the native temporary file system. The cache identity includes the source-event hash and decoder hashes. A stale or damaged cache is rebuilt from the verified JSONL source.

The main result contains the compact replay and fill summary. It does not contain the full fill list. Request detailed records only when needed:

```text
GET /api/backtests/{job_id}/fills
X-Backtest-Owner: {owner_token}
```

The detailed-fill file and the main result have separate SHA-256 checks.

Run a bounded performance check with:

```bash
python benchmark_lp.py --event-limit 10000 --product-mode
```

Use `--interval relatively_quiet`, `--interval relatively_active`, or `--interval directional` to compare verified regimes. Generated benchmark JSON is ignored by Git. The committed workload definition is `benchmarks/large_grid_workload.json`.

The LP form shows fee and flip spread as a percentage. The SDK adapter converts this value to the SDK `minFeesPps` unit. In SDK version `0.0.95`, 100 pps equals one basis point. Thus, 0.30 percent equals 3,000 pps.

The same SDK order-grid object supplies the preview, the required MON and USDC amounts, and the orders submitted to the backtester. The backend creates the same grid again with the installed SDK and rejects a mismatch. The preview sorts all bid and ask levels by numeric price. Its bar height is quote notional (`base size × price`) with contract integer rounding. The green line is the midpoint before the selected first block. The prior reversed bid display was a chart-ordering defect. It was not an SDK distribution defect.

The UI compares every final SDK order size with the market `minSize` after all tick and decimal rounding. It shows the smallest order, the market minimum, the number of invalid orders, and the first invalid side and price. Invalid bars are amber. **Run backtest** stays disabled until the position has valid order sizes and sufficient entered deposits. The backend repeats the same raw-integer checks and returns a structured error if the submitted position is not deployable.

Edit either `MON amount` or `USDC amount` to make that token the position driver. Select **Auto-fill amounts** to calculate the other token from the exact tick-rounded grid. Auto-fill changes only simulator inputs. It does not perform a token swap.

**Use minimum valid amounts** uses the official SDK in a deterministic raw-integer search. It doubles the selected driver amount until all orders pass, then uses binary search for the smallest passing amount. It checks the final grid again and confirms that one raw unit less fails. This control keeps the selected driver. It is different from **Auto-fill amounts**, which only balances the token ratio.

The initial user-provided flip orders must meet `minSize`. Kuru's contract can create a smaller internal paired flip from a small partial fill. Later partial fills add to that same paired order. The simulator keeps this contract exemption explicit and does not drop filled assets or create multiple dust orders.

The screen runs only new Kuru LP simulations. The previous MM research code and reports remain as historical regression artifacts. The screen has no wallet connection and no live order execution. See [the Kuru LP request path](BACKTEST_REQUEST_PATH.md).

To find another interval:

```bash
python discover_interval.py \
  --from-block 44853800 \
  --to-block 44856100
```

## Rebuild the history from the chain

The market contract was first present at Monad block `43,660,999`. This block has the UTC time `2025-12-22 07:43:25`. Blocks before this point cannot have orders for this market.

Run a full backfill from the saved checkpoint to the latest block:

```bash
python rebuild_from_chain.py
```

This is a local batch job. It does not need a server. A full backfill can take a long time and can use much disk space. You can process a fixed number of blocks in one run:

```bash
python rebuild_from_chain.py --max-blocks 1000000
```

Run the same command again to continue. The script starts at the first block that is not complete.

You can set a fixed end block:

```bash
python rebuild_from_chain.py --to-block 60000000
```

The default historical RPC is `https://rpc2.monad.xyz`. To use another archive RPC:

```bash
MONAD_RPC_URL=https://your-rpc.example python rebuild_from_chain.py
```

The script reduces its log range when the RPC rejects a large result. It also retries temporary network errors.

### Chain output

The script writes these files under `chain_data/`:

```text
chain_data/
├── state.sqlite3
├── events/
│   └── part-<first-block>-<last-block>.parquet
└── snapshots/
    └── part-<first-block>-<last-block>.parquet
```

`state.sqlite3` contains:

- the next block checkpoint;
- all active L3 orders;
- aggregate price levels;
- the Kuru AMM vault state;
- market precision and asset metadata.

The event Parquet parts contain the raw log fields, decoded event name, decoded JSON arguments, block time, transaction position, and log position. These files are the durable historical L3 change stream.

The snapshot Parquet parts contain one row after all market events in a block. A row has:

- explicit-book best bid and ask;
- vault best bid and ask;
- combined best bid and ask;
- midpoint, spread, and spread percentage;
- active L3 order count.

Blocks with no market event do not get a snapshot row because the book did not change.

After each chunk, the script calls the historical `bestBidAsk()` contract function. It compares the contract prices with the rebuilt prices. If they do not match, the script stops and does not advance the checkpoint.

## Use a published daily L2 file

The default date is `2026-05-01`. A complete published file exists for this market and date.

```bash
python analyze_l2.py
```

To request another UTC date:

```bash
python analyze_l2.py --date 2026-05-01
```

The script prints the first five rows and saves this chart:

```text
output/mon_usdc_2026-05-01_midpoint_spread.png
```

Use `--force` to download an existing local file again. Use `--output-dir PATH` to select another output folder.

If a book side is empty or malformed, the script keeps the row and uses `NaN` for the affected values. It keeps a malformed timestamp as `NaT`.

## Tests

```bash
python -m unittest discover -s tests -v
```

The tests cover the daily-file parser, ABI event decoding, exact L3 links, queue heads and tails, head-only fills, invariant failures, replay cursor movement, event parity, AMM vault changes, combined top-of-book values, no look-ahead, inclusion delay, simulated queue position, capital reservations, fee accounting, P&L, turnover, drawdown, transaction-level counterfactual impact, downstream displacement, bounded Envio pagination, Trade-only broad filtering, candidate-only order filtering, address serialization, foreign-address rejection, cache identity isolation, executed-volume ranking, named interval loading, evenly distributed L2 checks, cancellation-summary handling, verified event-artifact loading, named topic breakdowns, explicit block fields, unordered block joins, integer and string timestamps, missing-block fallback, stale-cache invalidation, cache resume, duplicate removal, local sliding windows, HTTP 429 retry behavior, and deterministic outputs.

## Sources

- [Kuru Python SDK historical L2 documentation](https://github.com/Kuru-Labs/kuru-sdk-py#historic-l2-order-book-data)
- [Kuru public order-book contract source](https://github.com/Kuru-Labs/Kuru-contracts-dex-public)
- [Monad network and public RPC documentation](https://docs.monad.xyz/developer-essentials/network-information)
- [Envio HyperSync clients](https://docs.envio.dev/docs/HyperSync/hypersync-clients)
- [Envio Monad endpoints](https://envio.dev/chains/monad)
