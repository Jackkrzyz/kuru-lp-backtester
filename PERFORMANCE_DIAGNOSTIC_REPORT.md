# Kuru LP runtime diagnostic and optimization report

## Result

The preserved large-grid workload now completes through the warm HTTP job API in 3.18 seconds. The strategy simulation uses 1.91 seconds. It processes 98,178 historical events, 2,500 initial LP orders, and 444 simulated fills. Peak RSS is 328.0 MB. The run is under the 60-second target.

The exact fill sequence and economic result did not change. The final timeline SHA-256 is `8d0dd5bd54d30c24cdfa462198d6f50cc6b40d699be8a702a1bfe15b37840cf4` before and after the changes.

The ordered fill SHA-256 also stays `59a267d39cef462b9ac7d6e0d7c0e9afda8cecf4e47018ea777b2a150ccb5888`. Engine v14 changes the result schema and run identity, but it does not change reconstruction or fill decisions.

## Follow-up performance upgrades: engine v14

This pass implements the remaining low-risk speed work from the audit:

- A versioned SQLite cache stores decoded events. Its identity includes the exact source-event SHA-256 and the decoder source hashes. It is data only and does not use Python pickle.
- The cache uses the native temporary file system. This avoids repeated JSONL parsing and slow Windows-mounted-path reads in each isolated worker.
- One authenticated localhost broker keeps the official Kuru SDK Node.js adapter process warm. Isolated Python strategy workers still keep engine state separate.
- The SDK broker restarts after a broken pipe, invalid response, or stopped Node.js process. A worker can use the original one-shot adapter path if the broker is not available.
- The LP replay groups the already sorted event stream with `groupby()`. It does not build a second block-to-event dictionary.
- Compact replay generation consumes retained records directly. It does not write and read a temporary timeline JSONL file.
- The main result does not include the complete fill list. Detailed fills have a separate owner-scoped endpoint and a separate hash-checked artifact.
- Canonical JSON output now rejects `NaN` and infinity.

The measured warm large-grid API run is:

| Metric | Engine v13 | Engine v14 |
|---|---:|---:|
| End-to-end job time | 10.72 s | 3.18 s |
| Event load and decode | 4.38 s | 0.93 s |
| SDK grid | cached or first-use dependent | 0.03 s, warm worker |
| Strategy simulation | 1.85 s | 1.91 s |
| Replay compaction | included in total | 0.016 s |
| Result payload | 4.05 MB | 3.80 MB |
| Separate detailed fills | inline | 0.25 MB |
| Peak RSS | 286.6 MB | 328.0 MB |

The SQLite cache reduces warm event loading by 79 percent. End-to-end time is 70 percent lower than the earlier 10.72-second API result. Peak RSS is 41.4 MB higher because the decoded cache and worker process use more resident memory. This is an accepted speed-for-memory trade for the 15 GB host.

The first browser run after a server start took 7.47 seconds. It included 5.40 seconds to start and load the SDK worker. The next distinct SDK input took 0.012 seconds for grid generation and completed its full job in 2.08 seconds. An identical cached repeat completed in 1.01 seconds.

## 1. Sleeping-process diagnosis

The original PID `156976` was no longer present when this audit started. Thus, its exact kernel wait channel and Python stack could not be recovered. The original API did not save the request body, job record, heartbeat, exception, or terminal state. There was no worker process to inspect.

The available evidence is:

- The captured process used 0.0 percent CPU and had 524,784 KB RSS. Memory and swap were not under pressure.
- The old server ran the backtest inside one synchronous HTTP request.
- One global lock covered the complete service run. An abandoned request could block all later requests.
- The browser request gate ignored an old response, but it did not cancel or track the old backend work.
- The UI had no durable job record. It could stay in the running state after a lost HTTP request.
- During the audit, PID `156976` did not exist. Port 8000 still had an old `wslrelay.exe` process, and `/api/intervals` timed out. This is direct evidence of a frontend or relay that remained after the Python backend was gone.

The exact wait object of PID `156976` is unknown because the process exited before inspection. The product failure is known: the synchronous request architecture had no durable owner-scoped job and no guaranteed terminal-state transition. The UI could outlive the backend request. Increasing a timeout would not correct this failure.

The new architecture gives each execution one of these terminal states: `completed`, `failed`, `canceled`, or `stalled`. A job with no heartbeat for 120 seconds becomes `stalled`. A stalled job can be canceled.

## 2. Preserved large-grid workload

The committed manifest is `benchmarks/large_grid_workload.json`.

| Field | Value |
|---|---:|
| Market | `0x065c9d28e428a0db40191a54d33d5b7c71a9c394` |
| Interval | `relatively_active`, blocks 109097028–109107027 |
| Historical events | 98,178 |
| Shape | Spot |
| Price range | 0.025–0.030 USDC |
| Fee / flip spread | 0.01% |
| Deposit | 600,000 MON and 18,000 USDC |
| Initial grid | 1,070 bids and 1,430 asks |
| Inclusion delay | 1 block |
| Transaction participation cap | 10% |
| Maker fee | 0 bps |
| Fixed action cost | 0 USDC |
| Impact threshold | 25% |
| Event-file SHA-256 | `04bc2d4e2f59206586f6b78e95a9700820bcff7a554f1eb060486409f331809d` |
| Checkpoint SHA-256 | `8cd3d48f75e07c2edb8e59fd09ab1c1ae85b5fc7f707e4bb63ea816985462fb1` |

The old POST body was not saved, so it could not be recovered. This replacement uses the same verified active interval and a valid large official-SDK grid. Old local cache and job artifacts were not deleted. An output without `complete.json` is not a successful job.

## 3. Baseline measurements

The baseline used the same 2,500-order grid. The run stopped at 10,000 events because RSS was already 1.00 GB and serialized output was 170.29 MB. A full baseline was not safe or useful on the 15 GB host.

| Events | Wall | CPU | Wait | Simulation | Peak RSS | Serialized output |
|---:|---:|---:|---:|---:|---:|---:|
| 1,000 | 19.79 s | 6.93 s | 12.86 s | 4.04 s | 539.4 MB | 21.69 MB |
| 5,000 | 37.06 s | 24.11 s | 12.95 s | 20.66 s | 723.8 MB | 94.47 MB |
| 10,000 | 57.75 s | 44.77 s | 12.99 s | 40.74 s | 1,001.5 MB | 170.29 MB |

The simulation cost was approximately 4 ms per event. Memory and output grew with copied order snapshots. This was a superlinear product risk when active order count also increased.

Measured causes were:

- full active-order snapshots in timeline records;
- timeline record construction before sampling;
- full book and capital invariant checks after every event;
- scans of all simulated orders for activation, queue updates, and fills;
- repeated price sorting and whole-book best-price scans;
- Node.js SDK process startup for an uncached grid;
- one cancel-file `stat` call on the Windows-mounted path for every event.

## 4. Optimized prefix scaling

Product mode keeps full validation at the start and end. Test and validation mode keeps full per-event invariant checks.

| Events | Wall | Simulation | Events/s | Peak RSS | Serialized output |
|---:|---:|---:|---:|---:|---:|
| 1,000 | 4.42 s | 0.049 s | 226 | 303.5 MB | 4.30 MB |
| 5,000 | 4.52 s | 0.130 s | 1,106 | 304.9 MB | 4.45 MB |
| 10,000 | 4.63 s | 0.211 s | 2,162 | 305.2 MB | 4.57 MB |
| 20,000 | 4.78 s | 0.392 s | 4,184 | 305.5 MB | 4.76 MB |
| 50,000 | 5.45 s | 1.128 s | 9,169 | 309.3 MB | 5.63 MB |
| 98,178 | 6.09 s | 1.663 s | 16,123 | 315.1 MB | 6.95 MB |

The fixed cost is local event loading. Simulation time now scales approximately with historical events and affected order operations. The full direct benchmark is 9.5 times faster than the 10,000-event baseline, while it processes 9.8 times more events.

## 5. Final API profile

The large-grid browser API run reported:

| Metric | Value |
|---|---:|
| End-to-end job and result time | 10.72 s |
| Engine wall time | 1.851 s |
| Event load and parse | 4.377 s |
| Capital accounting | 0.0084 s |
| Flip creation | 0.0041 s |
| HTTP JSON encoding | 0.0257 s |
| HTTP socket write | 0.0017 s |
| Peak RSS | 286.6 MB |
| Compact replay frames | 1,469 |
| Result payload | 4.05 MB |

One earlier instrumented run took 27.38 seconds. The strategy used only 7.10 CPU seconds and waited for 12.72 seconds. The wait came from checking a cancel marker on `/mnt/c` for every event and from live status writes on that path. Cancellation checks now occur every 256 events. Live heartbeat files use the native WSL temporary file system. Requests, final status, results, and completion markers remain in the durable project cache.

## 6. Cross-regime and grid-size checks

Each run uses the complete verified interval. `small`, `normal`, and `large` use 1.00%, 0.30%, and 0.01% grids in the same 0.025–0.030 range.

| Interval | Grid | Events | Orders | Fills | Total | Simulation | Peak RSS |
|---|---|---:|---:|---:|---:|---:|---:|
| quiet | small | 69,672 | 19 | 1 | 16.34 s | 0.54 s | 228.7 MB |
| quiet | normal | 69,672 | 62 | 13 | 16.48 s | 0.59 s | 229.8 MB |
| quiet | large | 69,672 | 2,500 | 141 | 16.70 s | 0.85 s | 248.0 MB |
| active | small | 98,178 | 19 | 5 | 17.93 s | 0.81 s | 278.7 MB |
| active | normal | 98,178 | 62 | 18 | 17.91 s | 0.87 s | 279.3 MB |
| active | large | 98,178 | 2,500 | 444 | 6.15 s | 1.74 s | 300.5 MB |
| directional | small | 96,656 | 19 | 4 | 17.73 s | 0.79 s | 275.9 MB |
| directional | normal | 96,656 | 62 | 32 | 17.92 s | 0.84 s | 276.6 MB |
| directional | large | 96,656 | 2,500 | 608 | 18.56 s | 1.54 s | 299.7 MB |

The first use of a new SDK input pays about 12.6 seconds to start Node.js and run the official SDK adapter. A repeated exact grid uses the grid cache and removes this cost. All runs stay under 60 seconds.

## 7. Semantic parity

The optimized large run matches the medium golden prefix and the earlier full result:

- canonical grid SHA-256: `0b434e00d9537178ccc73294add59edcb6a38ba8ce5c8ee6a706e3fb54636de1`;
- ordered fill SHA-256: `59a267d39cef462b9ac7d6e0d7c0e9afda8cecf4e47018ea777b2a150ccb5888`;
- timeline SHA-256: `8d0dd5bd54d30c24cdfa462198d6f50cc6b40d699be8a702a1bfe15b37840cf4`;
- semantic timeline SHA-256: `88b6df10c901964135e9aed899e78bd4943e5f7abdb7417de1a3c9fa22e18458`;
- fills: 444;
- flip creates: 314;
- turnover: 4,101.54358623438 USDC;
- net P&L: 280.477564115129 USDC;
- maximum drawdown: 132.38088916747 USDC;
- final inventory: 510,102.554869771 MON;
- final cash: 20,459.1101524444 USDC.

Tests also cover numeric FIFO order when more than ten simulated orders share one price. This prevents text order such as `lp-10` from moving ahead of `lp-2`.

## 8. Architecture changes

- `POST /api/backtests` returns an owner-scoped job ID with HTTP 202.
- `GET /api/backtests/{job_id}` returns status and progress.
- `GET /api/backtests/{job_id}/result` returns only a completed, hash-verified result.
- `DELETE /api/backtests/{job_id}` cancels a queued, running, or stalled job.
- Each run uses an isolated worker process. There is no global run lock.
- Final files use atomic replacement and completion metadata.
- Running heartbeats include process CPU, wait time, thread states, stage, counts, and lock declarations.
- The engine uses order, price, pending-block, and queue-follower indexes.
- Best bid and ask are maintained incrementally.
- Timeline sampling happens before expensive record construction.
- The timeline uses one initial order snapshot and ordered deltas.
- A separate versioned semantic timeline hash covers ordered market, action, fill, order-delta, and financial meaning. It does not depend on unknown storage-only fields.
- The compact replay has at most 1,500 frames and keeps all strategy actions.
- Browser charts cache complete static line layers and reveal a prefix. They do not redraw all earlier points for each animation frame.
- Exact result cache identity includes normalized configuration, SDK grid, event-file hash, checkpoint hash, market parameters, engine version, and replay schema version.

## 9. Progress, cancellation, and browser verification

The API showed the stages `queued`, `loading_historical_events`, `generating_sdk_grid`, `simulation`, `compacting_replay`, `writing_cache`, `writing_result`, and `completed`. A direct cancellation test changed a running job to `canceled` in less than one second.

The browser test used the real page and local API. It showed progress, loaded result `lp-1a0c7c18ed0cd5eef278`, displayed 1,246 replay frames and +0.7693 USDC P&L, played frames at 100x, reconstructed active order deltas, and showed no browser warning or error. A second browser run was canceled and returned the button to its ready state.

## 10. Commands

```bash
python benchmark_lp.py --event-limit 1000 --product-mode
python benchmark_lp.py --event-limit 5000 --product-mode
python benchmark_lp.py --event-limit 10000 --product-mode
python benchmark_lp.py --event-limit 20000 --product-mode
python benchmark_lp.py --event-limit 50000 --product-mode
python benchmark_lp.py --event-limit 999999 --product-mode
python backtest_server.py --host 127.0.0.1 --port 8000
python -m pytest -q
```

Use the `--interval`, `--fee-percent`, `--mon-amount`, and `--usdc-amount` benchmark options for the cross-regime checks.

## 11. Remaining limits

- The original sleeping process exited before its Python stack and kernel wait channel were collected. The exact historical wait object is not recoverable.
- The first SDK request after a server start still pays process and module startup. Later distinct grids use the warm worker.
- The decoded-event cache is temporary. A reboot or temporary-directory cleanup causes one verified JSONL decode and cache rebuild.
- Result JSON is still 3.80 MB for the 2,500-order grid because the initial grid and compact replay are required by the UI. Detailed fills now use a separate endpoint.
- Product mode performs full invariant checks at the start and end. Validation mode still checks after each event.
- Block timestamps do not give transaction time inside a block. Ordering uses block, transaction, and log indexes.
- Simulated liquidity is counterfactual. It does not change later historical taker behavior.

## 12. Follow-up verification

- Complete suite: `128 passed, 12 subtests passed`.
- Direct fixed benchmark, warm decoded cache: 98,178 events in 2.80 seconds, including 0.92 seconds to load events and 1.78 seconds to simulate and build the retained timeline.
- Browser flow: form edit, POST, job polling, compact result fetch, and result rendering passed on the local server.
- Optional fill route: returned 13 records for a 13-fill run; the main result had no inline `fill_records` field.
- Large-grid parity: 444 fills, ordered fill hash `59a267d39cef462b9ac7d6e0d7c0e9afda8cecf4e47018ea777b2a150ccb5888`, and compact timeline hash `8d0dd5bd54d30c24cdfa462198d6f50cc6b40d699be8a702a1bfe15b37840cf4`.
