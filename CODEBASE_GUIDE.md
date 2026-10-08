# Codebase guide

## Purpose

This guide documents the first-party source code without changing the running engine. It describes module ownership, data flow, units, ordering, invariants, and output contracts.

The generated file `frontend/kuru_sdk_adapter.js` is not hand-maintained source. Build it from `sdk_reference/browser_adapter.js`.

This guide was checked against all hand-maintained source and test files on 2026-10-07. Third-party dependencies and generated output are outside this guide.

## System map

```text
Envio HyperSync ─┐
                 ├─ validated event JSONL ─ checkpoint L3 book ─ Kuru LP replay
Archive RPC ─────┘                                  │                 │
                                                    │                 ├─ summary
Official Kuru SDK ─ canonical initial order grid ───┘                 └─ compact timeline
                                                                          │
Form configuration ─ local HTTP API ───────────────────────────────────────┘
                                                                          │
                                                                    browser playback
```

The reconstruction and strategy layers are separate:

- Reconstruction changes only the historical market state.
- A simulated strategy reads that historical state and keeps its orders in separate strategy state.
- Simulated orders do not change later historical trades. The transaction-participation cap limits this counterfactual assumption.

## Fixed units

| Value | External display | Internal form |
|---|---|---|
| Kuru price | USDC per MON | integer divided by `price_precision` |
| Kuru order size | MON | integer divided by `size_precision` |
| MON token amount | MON | integer with 18 token decimals at API boundaries |
| USDC token amount | USDC | integer with 6 token decimals at API boundaries |
| Fee / flip spread | percent | SDK pps; 1 percent equals 10,000 pps |
| Maker fee | basis points | `Decimal` in the strategy engines |
| Block timestamp | UTC | integer Unix seconds in validated inputs |

Do not mix token-decimal raw amounts with Kuru `size_precision` order sizes. They have different scales.

## Deterministic ordering

All historical events must use this order:

1. block number;
2. transaction index;
3. log index.

The validation input records this rule. Strategy decisions can use only the book state produced by events that are already processed. Initial orders use the configured block inclusion delay. A contract-created flip cannot fill from the transaction that created it.

## Main product request path

### `historical_system.py`

This module removes the named-regime requirement from the product engine.

| Symbol | Responsibility |
|---|---|
| `HistoricalEventStore` | Verify and stream canonical local partitions by market and block range. |
| `CheckpointStore` | Find the latest verified L3 checkpoint at or before the requested state block. |
| `ReconstructionEngine` | Replay local events from the checkpoint to the state before `from_block`. |
| `ReplayCursor` | Provide a repeatable chronological stream through `to_block`. |
| `KuruLpBacktestEngine` | Pass the local cursor to the unchanged verified LP engine with bounded replay sampling. |
| `ValidationLayer` | Enforce saved parity, L2, queue, checkpoint, and content-hash gates. |

`historical_index_manifest.json` records complete L3 partitions and checkpoints. The Trade chart scope is separate and does not make a block range backtestable.

### `backtest_server.py`

This is the local HTTP entry point.

- `BacktestHandler.do_GET()` serves interval metadata, job status, compact results, optional detailed fills, or static frontend files.
- `BacktestHandler.do_POST()` creates an owner-scoped `/api/backtests` job and returns HTTP 202.
- `BacktestHandler.do_DELETE()` cancels a queued, running, or stalled job.
- Input errors return HTTP 400.
- Engine failures return HTTP 500.
- The server never returns demo data after an API failure.

The server uses `ThreadingHTTPServer`. It does not run a strategy inside the request thread. `BacktestJobManager` starts one isolated worker process for each job. There is no global strategy-run lock.

### `backtest_jobs.py` and `backtest_worker.py`

`BacktestJobManager` owns job IDs, owner tokens, worker processes, progress reads, cancellation, stall detection, and hash-verified results. The worker writes live heartbeat data to the native temporary file system. This avoids slow status writes on a Windows-mounted WSL path. Request, final status, compact result, detailed fills, and completion files stay in `.cache/backtest_jobs/`.

Every path ends in `completed`, `failed`, `canceled`, or `stalled`. Final output is valid only when its completion marker and SHA-256 match.

### `sdk_grid_broker.py`

This module keeps one official-SDK Node.js process warm across isolated jobs. It exposes a private authenticated localhost endpoint to child workers. `PersistentSdkProcess` uses one JSON request and response per line, keeps requests sequential, and restarts the Node.js process once after a transport or response failure. The job manager passes the private address and token only in the child process environment. Strategy state does not cross this boundary.

### `backtest_service.py`

This module joins the API, validated local inputs, official SDK adapter, LP engine, and compact replay builder.

| Symbol | Responsibility |
|---|---|
| `IntervalData` | Immutable bundle of a checkpoint book, decoded events, timestamps, parameters, and validation metadata. |
| `decimal_text()` | Validate and normalize user decimal input without accepting excess precision. |
| `percentage_price_bounds()` | Convert first-block midpoint percentages to outward tick-rounded raw prices. |
| `validate_sdk_grid()` | Report each final SDK order that is below the market minimum size. |
| `serialize_book()` / `deserialize_book()` | Store and restore the exact L3 checkpoint links and level totals. |
| `prepare_interval_cache()` | Make a one-time RPC checkpoint cache before interactive use. |
| `canonical_sdk_grid()` | Use the warm official SDK broker, or run the same installed adapter in a one-shot Node.js process as a fallback. |
| `normalize_grid()` | Convert SDK values to a stable JSON form for equality and hashing. |
| `BacktestService.load_interval()` | Compatibility wrapper for the three named golden fixtures. |
| `BacktestService.load_range()` | Build an arbitrary covered local block range from the generalized stores. |
| `BacktestService.interval_metadata()` | Replay only the first block from the local validated event prefix and return its midpoint, best prices, and market parameters. |
| `BacktestService.range_metadata()` | Return first-block price and market data for a submitted local block range. |
| `BacktestService.normalize_request()` | Validate every user field and make the SDK input. |
| `BacktestService.run()` | Verify the submitted grid, run LP replay, compact output, hash the result, and save small cache artifacts. |

The decoded-event cache is a versioned SQLite file in the native temporary directory. Its identity includes the exact event-file hash and decoder source hashes. It contains only data. A stale, damaged, or absent cache is rebuilt from the verified JSONL source.

The deterministic run identity includes the engine version, replay schema, normalized configuration, exact canonical SDK grid, event-file hash, checkpoint hash, and market parameters. A completed result cache requires matching atomic metadata and file hashes. The full fill list is not in the main browser response. Its separate owner-scoped artifact has its own hash.

### `kuru_lp.py`

This is the Kuru concentrated-liquidity strategy engine. It consumes verified historical events. It does not fetch network data.

| Symbol | Responsibility |
|---|---|
| `LPPosition` | One initial SDK grid order: side, price, size, and paired flip price. |
| `LPDistribution` | Full bid and ask grid plus required token amounts. |
| `LPConfig` | Deposits, fees, inclusion delay, participation cap, and impact threshold. |
| `LPOrder` | Mutable simulated order state, including queue ahead, reservations, and pair link. |
| `LPState` | Cash, inventory, live orders, reservations, actions, fills, P&L, and indexed pending, price, and queue-follower state. |
| `generate_distribution()` | Python parity builder for pinned SDK fixtures. The product API uses the official SDK grid. |
| `_reserve()` | Reserve complete order capital before an order can become live. |
| `assert_lp_invariants()` | Confirm reservation, capital, size, and queue rules. |
| `assert_flip_minimum_semantics()` | Reject small initial orders while allowing the contract's small internal paired flips. |
| `initial_orders()` | Put every generated grid order into pending strategy state. |
| `activate_orders()` | Activate only eligible pending IDs and join each order at its numeric FIFO tail. |
| `update_queue_ahead()` | Reduce queue ahead after historical fills or cancellations. |
| `apply_fills()` | Apply price crossing, queue, causal, transaction-budget, accounting, and partial-fill rules. |
| `_place_or_merge_flip()` | Create or add size to the paired opposite-side flip order. |
| `_timeline_record()` | Build one deterministic market, order, and financial state record. |
| `run_lp()` | Replay the interval, collect stage timings and counters, and return the complete summary plus retained timeline. |

Important LP rules:

- Initial orders start as pending.
- Activation reads the historical price queue and records all historical size ahead.
- Historical size behind a simulated order does not delay its fill.
- A Trade event cannot fill more simulated size than its remaining historical volume.
- A transaction-level participation budget is shared by all Trade events and all simulated orders in that transaction.
- A partial fill creates or updates one paired flip order.
- Initial SDK orders must meet `minSize` after integer rounding.
- The contract can create one internal paired flip below `minSize` from a small partial fill. Later partial fills add to the same paired order.
- Flip orders reserve the proceeds that became available from the causal fill.
- There is no periodic cancel and replace in Kuru LP mode.
- Product mode uses localized indexed updates and full start/end checks. Validation mode keeps full per-event invariant checks.

### `compact_replays.py`

This module converts complete backend records into at most 1,500 frontend frames.

- It preserves the first and final state.
- It preserves placements, activations, cancellations, fills, and financial changes.
- New LP records use one complete order snapshot followed by order deltas.
- It samples only market-only states.
- `validate_final_state()` requires the final compact cash, inventory, value, P&L, and hold P&L to equal the backend summary.
- `build_compact_replay()` supports historical JSONL artifacts for both Kuru LP and Advanced MM Bot result schemas.
- `build_compact_replay_from_records()` gives product runs the same output without temporary JSONL serialization.

### `frontend/app.js`

This file owns the consumer interface and canvas replay.

| Function group | Responsibility |
|---|---|
| `sdkInput()`, `generateDistribution()` | Convert relative or absolute range controls and build the exact SDK input and preview grid. |
| `renderDistributionPreview()`, `renderAmountStatus()` | Draw ascending price levels, mark invalid bars, and show amount and minimum-size checks. |
| `buildKuruRequest()` | Send market, first block, last block, visible LP configuration, and the serialized preview grid to the API. |
| `runKuruBacktest()` | Clear old state, create a job, poll progress, get the result, and surface errors. |
| `applyKuruResponse()` | Replace all metrics and replay frames with one returned run. |
| `clearResult()`, `markConfigurationChanged()` | Prevent an old result from appearing under edited settings. |
| `renderFrame()`, `drawPriceChart()`, `drawEquityChart()` | Rebuild order deltas and render client-side playback without rerunning Python. Static line layers are cached and only the visible prefix is drawn. |
| `loadDemoReplay()` | Load saved data only for the labeled Advanced MM Demo mode. |

`frontend/request_gate.js` gives each request a sequence and configuration revision. A response is accepted only if both still match.

Current frontend behavior:

- The request gate rejects an old response and the browser cancels its superseded job.
- Playback rates are frame rates. They are not multiples of historical elapsed time.
- Block timestamps give one time value for all logs in a block. Event order comes from transaction and log indexes.
- Compaction keeps each financial change in a frame, but the UI displays the final state of that frame.
- The token amount driver depends on the last edited token. It is included in the request as `amount_driver`.
- Inclusion latency, maker fee, fixed action cost, and impact threshold use fixed values in the current Kuru LP form. The participation cap is the editable advanced setting.
- The range chart uses Trade data only. It shows complete validated L3 coverage separately. Current validated coverage is continuous from block 109092028 through block 109142027. The UI disables a range outside local validated coverage.

### `frontend/index.html` and `frontend/styles.css`

`index.html` defines the two strategy modes, Kuru LP inputs, Advanced Simulation controls, result identity, charts, playback controls, metrics, and simulated-order list. It loads the generated SDK bundle, request gate, and application code in that order.

`styles.css` defines the responsive two-panel layout, Kuru LP preview, metric cards, warning states, playback controls, and mobile breakpoints. It has no strategy or accounting logic.

## Reconstruction modules

### `discover_envio_intervals.py`

This is the bounded HyperSync discovery tool.

- `DiscoveryCache` stores schema, query identity, cursor, events, and rate-limit state.
- `query_payload()` applies both target market address and allowed topic filters.
- `fetch_discovery_events()` performs one forward paginated pass and saves the next-block cursor.
- `page_timestamp_map()` joins unordered block metadata to logs by block number.
- `lookup_block_timestamp()` fetches only missing block metadata.
- `retry_delay_seconds()` honors server rate-limit headers or uses bounded backoff with jitter.
- `build_interval_stats()` calculates local sliding windows from Trade events.
- `select_intervals()` chooses quiet, active, and directional non-overlapping candidates.
- `fetch_candidate_order_events()` fetches dense order topics only for selected candidates.

### `rebuild_from_chain.py`

This is the long-range RPC reconstruction tool.

- `RpcClient` provides bounded JSON-RPC requests, batch calls, log reads, and timestamps.
- `decode_log()` maps raw topics and data to named Kuru events.
- SQLite stores metadata, active orders, aggregate levels, and vault state.
- `add_order()`, `set_order_size()`, and `cancel_order()` update explicit L3 state.
- `update_vault_from_params()` and `update_vault_from_trade()` update AMM vault state.
- `make_snapshot()` writes explicit, vault, and combined top-of-book values.
- `fetch_logs_adaptive()` reduces an RPC range when a provider rejects a large result.
- `process_chunk()` applies one bounded chunk and writes event and snapshot Parquet files atomically.
- `validate_against_contract()` checks the completed chunk before advancing the durable cursor.

### `l3_vertical_slice.py`

This is the verified in-memory explicit-order reconstruction used by validation and backtests.

| Symbol | Responsibility |
|---|---|
| `BookOrder` | One immutable historical order with previous and next links. |
| `PriceQueue` | Queue head, tail, and total size for one side and price. |
| `ReplayEvent` | Decoded event with exact chain position. |
| `ReplayCursor` | Deterministic bounded replay position. |
| `HyperSyncClient` | Filtered Envio event fetcher. |
| `L3Book.append()` | Add a historical order only at the verified queue tail. |
| `L3Book.remove()` | Relink both neighbors and update level totals. |
| `L3Book.set_size()` | Apply a head-only partial or full Trade fill. |
| `L3Book.add_size()` | Apply a flip-order size increase. |
| `L3Book.check_invariants()` | Walk all queues and verify reachability, links, sides, prices, tails, and totals. |
| `load_checkpoint()` | Rebuild every queue from historical contract reads at one block. |
| `decode_events()` | Decode and sort logs by chain position. |
| `apply_event()` | Apply one order lifecycle event to the explicit L3 book. |
| `compare_l2()` | Require exact aggregate equality with `getL2Book()`. |
| `parity_report()` | Require event-for-event Envio/RPC equality. |

`OrdersCanceled` is a summary event. It is audited but is not applied as a second set of individual cancellations.

### `reconstruction_validation_report.py`

This module produces the independent reconstruction report.

- It loads one named interval from `interval_manifest.json`.
- It compares Envio and RPC events.
- It audits summary and individual cancellation events.
- It replays the checkpoint through the interval.
- It compares seven distributed checkpoints with contract L2 data.
- It records price-level, aggregate-size, midpoint, and queue errors.
- It plots reconstructed bid, ask, midpoint, Trade prices, official checkpoint midpoint, and error.
- OHLC data, when used, comes only from Trade events.

## Strategy and evaluation modules

### `strategy_vertical_slice.py`

This is the preserved Advanced MM Bot engine.

- `StrategyConfig`, `StrategyOrder`, and `StrategyState` define inputs and mutable state.
- Reservation functions prevent reuse of cash or MON.
- Queue functions enforce tail placement and update size ahead.
- `apply_strategy_fills()` enforces causality, price crossing, queue state, partial fills, and transaction budgets.
- `cancel_and_place_quotes()` implements the optional cancel-and-replace policy.
- `build_transaction_impacts()` groups fills by transaction and measures displaced downstream maker volume.
- `maximum_drawdown()` uses the marked-equity high-water mark.
- `run_strategy()` produces the deterministic summary and complete timeline.

### `multi_regime_evaluation.py`

This runner applies unchanged default Advanced MM parameters to the quiet, active, and directional intervals. It runs uncapped, 25 percent, 10 percent, and 5 percent transaction-participation scenarios. It writes one comparison file and separate raw timelines.

### `build_lp_frontend.py`

This older build tool converts the validated LP vertical-slice artifact into a static frontend artifact. It is not in the live Kuru LP request path.

Treat this tool and `lp_catalog.json` as legacy demo-artifact support. A real Kuru LP run must use `POST /api/backtests` and must not fall back to these files.

## Data analysis tools

### `analyze_l2.py`

This tool downloads one published daily Parquet file, parses JSON bid and ask arrays, handles malformed or empty rows, computes best bid, best ask, midpoint, spread, and spread percent, prints five rows, and saves a chart.

### `discover_interval.py`

This small bounded RPC tool counts decoded market events and prints candidate checkpoints. The Envio discovery tool is the primary large-range workflow.

## Official SDK adapter

### `sdk_reference/browser_adapter.js`

This adapter calls `@kuru-labs/kuru-sdk` version `0.0.95`.

- `decimalToRaw()` and `rawToDecimal()` convert display units without floating-point math.
- `percentToPps()` and `ppsToPercent()` own the UI-to-SDK fee conversion.
- `percentagePriceBounds()` and `absolutePriceBounds()` apply deterministic tick rounding.
- `generateOrderGrid()` calls the SDK Spot, Curve, or Bid-Ask method.
- It joins bids and asks and sorts all levels by numeric price.
- `requiredAmounts()` derives MON from asks and USDC from bid notionals.
- `validateOrderGrid()` compares every final integer size with `minSize`.
- `findMinimumViablePosition()` doubles and binary-searches the selected raw token driver.
- `serializeOrderGrid()` makes a JSON-safe canonical payload.

`generate_grid_cli.js` is the backend command wrapper. `generate_fixtures.js` records pinned SDK fixtures. `test_browser_adapter.js` verifies all three shapes and unit conversions.

## Project configuration

### `pyproject.toml`

This file declares the Python package metadata, runtime dependencies, and top-level modules. The main runtime dependencies cover ABI decoding, HTTP requests, Parquet and pandas processing, plotting, and testing.

The module list includes each top-level first-party Python module. It does not package the frontend and SDK files. A source checkout is the reliable run form until the complete install artifact is built and tested.

### `sdk_reference/package.json`

This file pins `@kuru-labs/kuru-sdk` and the browser bundler. Its scripts generate fixtures, build the browser bundle, generate one grid from standard input, and run adapter tests.

### `interval_manifest.json`

This committed manifest names the verified quiet, active, and directional intervals. It separates broad Trade-only discovery from dense order-event enrichment. Backtest code must not change strategy parameters from this discovery metadata.

## Repository support files

### `.gitignore`

This file keeps environments, dependencies, caches, chain data, raw validation data, and raw replay timelines out of version control. It allows small summaries, manifests, reports, fixtures, and compact replay files. Do not remove the large-data rules when you add a new output directory.

### `envio_interval_smoke.json`

This is the recorded result of the bounded interval-discovery smoke run. It is evidence for candidate selection. It is not an engine input for replay.

### `sdk_reference/package-lock.json`

This generated lock file fixes the complete JavaScript dependency tree. Update it only through the package manager when the pinned SDK or build tool changes.

### Result and design documents

- `BACKTEST_REQUEST_PATH.md` records the live form-to-engine request path and two integration configurations.
- `SDK_REFERENCE.md` records the official SDK version and fixture method.
- `VERTICAL_SLICE_RESULTS.md`, `RELATIVELY_ACTIVE_RESULTS.md`, `STRATEGY_SLICE_RESULTS.md`, `KURU_LP_RESULTS.md`, and `MULTI_REGIME_RESULTS.md` record completed validation runs.
- `STRATEGY_ENGINE_AUDIT.md` records causal and accounting rules for the Advanced MM Bot.
- `PRODUCT_VERTICAL_SLICE.md` records the first consumer replay interface.
- `PLAN.md` records reconstruction design and deferred work.
- `PERFORMANCE_ACCURACY_AUDIT.md` records proposed corrections and optimizations. Its findings are not current engine behavior until code and parity tests implement them.

## Test ownership

| Test file | Main coverage |
|---|---|
| `tests/test_analyze_l2.py` | Malformed daily L2 rows, empty books, and metrics. |
| `tests/test_discover_envio_intervals.py` | Address/topic filters, pagination, timestamps, cache identity, resume, deduplication, 429 handling, and local ranking. |
| `tests/test_rebuild_from_chain.py` | Event decoding, explicit levels, cancellations, Trades, and vault changes. |
| `tests/test_l3_vertical_slice.py` | Queue links, heads, tails, invariants, cursor, parity, and passive order replay. |
| `tests/test_reconstruction_validation_report.py` | Numerical errors, report schema, deterministic outputs, and cancellation audit. |
| `tests/test_strategy_vertical_slice.py` | No look-ahead, queue behavior, capital, accounting, fees, drawdown, transaction impact, and replacement math. |
| `tests/test_multi_regime_evaluation.py` | Validation gates, scenario matrix, and deterministic comparison. |
| `tests/test_kuru_lp.py` | SDK parity, initial tail position, flips, partial fills, shared transaction budgets, capital, and action costs. |
| `tests/test_backtest_service.py` | Different live configurations, deterministic repeats, and grid changes from fee, deposit, or shape. |
| `tests/test_compact_replays.py` | Frame limits, preserved actions and financial changes, and final-state equality. |
| `tests/test_frontend_artifacts.py` | Required controls, no LP fallback import, compact artifacts, and frontend request path. |
| `tests/test_kuru_sdk_browser_adapter.py` | Official SDK fixtures, browser bundle, and latest-request guard. |

## Generated and local-only data

These paths can be large and must stay outside version control:

- `.cache/`
- `validation_output/` raw event inputs and charts, except small reports and summaries allowed by `.gitignore`
- `strategy_output/` raw timelines
- `multi_regime_output/timelines/`
- `lp_output/**/timeline.jsonl`

Small manifests, summaries, reports, SDK fixtures, and compact frontend files can be version-controlled.

## Inline comment backlog

No source comment was changed while a backtest was running. When source edits are safe, add short comments only where the reason is not clear from the code:

1. Explain raw token units versus Kuru precision units at each accounting boundary.
2. Explain why flip orders cannot fill in their causal transaction.
3. Explain why `OrdersCanceled` is audited but not replayed as individual cancels.
4. Explain the transaction-budget sharing rule at the fill loop.
5. Explain why compact replay keeps every financial change while sampling market-only records.
6. Add return-schema docstrings to `run_lp()`, `BacktestService.run()`, and `build_compact_replay()`.
7. Add a checkpoint-cache schema comment that lists every input that invalidates it.
8. State that all events in one block use the block timestamp.
9. Explain that `financial_changes` can contain several ordered states in one compact frame.
10. Explain the supported contract version and the rule for an unknown event topic.
11. Add input and output schema docstrings to the local HTTP boundary.

Do not add comments that only repeat a function name or one line of code.

## Review boundaries

The audit covers these first-party areas:

- daily L2 download and analysis;
- RPC and Envio discovery;
- durable chain reconstruction;
- in-memory L3 reconstruction and validation;
- Advanced MM Bot replay;
- Kuru LP replay and flip behavior;
- compact replay generation;
- local HTTP service;
- browser request, preview, result, and playback logic;
- official SDK adapters and fixtures;
- package settings, interval manifest, ignore rules, and tests.

The audit does not treat these files as hand-maintained source:

- `.venv/` and `sdk_reference/node_modules/`;
- caches and generated backtest output;
- `frontend/kuru_sdk_adapter.js`, which is built from `sdk_reference/browser_adapter.js`;
- recorded charts and large timelines.
