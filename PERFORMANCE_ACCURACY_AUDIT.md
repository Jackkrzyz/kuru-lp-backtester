# Performance and accuracy audit

> This file is the pre-change static audit. The measured implementation and final results are in `PERFORMANCE_DIAGNOSTIC_REPORT.md`.

## Audit conditions

This is a static code audit. No backtest, benchmark, server restart, or source-code change was made during the audit because a backtest was already running.

The second pass checked all hand-maintained Python, JavaScript, HTML, CSS, tests, package settings, manifests, and ignore rules. It did not review third-party files under `node_modules/` or `.venv/`. It treated `frontend/kuru_sdk_adapter.js` as generated output and checked its source adapter instead.

The findings below are proposed work. They are not implemented. Preserve the verified reconstruction and current deterministic hashes until each change has parity tests.

## Executive result

The largest likely runtime cost is not the market event application itself. `run_lp()` makes a full timeline record for every historical event. That record serializes every live simulated order, even when the record is later removed by the stride rule. The engine also walks the complete historical L3 book for an invariant check after every event and repeatedly scans or sorts all simulated orders.

The highest accuracy risk is reproducibility of the local input. The current run identity hashes the validation summary, but the validation summary does not contain the SHA-256 of `source_events.jsonl`. A changed event file can therefore keep the same summary identity.

## Priority table

| Priority | Area | Finding | Expected effect | Required proof |
|---|---|---|---|---|
| P0 | Reproducibility | Hash the exact event file and checkpoint payload. Include both hashes in validation, cache identity, and run identity. | Prevent an altered local input from producing a trusted old identity. | Tamper test, stale-cache test, deterministic hash test. |
| P0 | Accuracy | Define whether portfolio marks use explicit L2 only or combined explicit plus vault liquidity. Current LP midpoint comes from `L3Book.aggregate()`. | Make hold P&L, drawdown, time in range, and final value match the intended Kuru market view. | Historical `bestBidAsk()` checks and written mark policy. |
| P0 | Accuracy | Keep cash, token balances, fees, and notional in raw integer token units through accounting. Convert only for reports. | Match contract rounding and remove long decimal fractions. | Fill-by-fill fixture against contract integer math. |
| P0 | Accuracy | Make time in range and capital utilization time-weighted. Current values are event-weighted, and fill records can add a second sample at one timestamp. | Report actual elapsed-time exposure instead of activity-weighted exposure. | Synthetic irregular-timestamp test and fixed-interval regression. |
| P0 | Queue accuracy | Give simulated orders an integer placement sequence and exact same-price FIFO links. Current candidate order tie-break uses string order IDs. | Prevent `lp-10` from sorting before `lp-2` and define precedence when flip prices converge. | Same-price initial and flip-order tests. |
| P0 | Input trust | Resolve the saved event path and require it to stay inside the named validation directory. | Prevent a malformed summary from loading an unrelated file. | Path traversal and absolute-path rejection tests. |
| P0 | Chain accuracy | Add a finality lag and block-hash continuity checks to long-range reconstruction. | Detect a reorganization instead of treating `latest` as final history. | Reorg, resume, and changed-block-hash tests. |
| P0 | Event safety | Fail on an unknown target-market event unless its topic is on a reviewed ignore list. | Prevent a contract upgrade or new state-changing event from being silently skipped. | Unknown-topic and version-boundary tests. |
| P1 | Speed | Do not build full `market_record` order payloads for events that the stride will discard. Update metrics from state directly. | Likely large CPU and allocation reduction. | Same summary and compact hashes for a fixed fixture. |
| P1 | Speed | Replace repeated full simulated-order scans with indexes by status, eligible block, side, price, and historical order ID ahead. | Reduce activation, queue update, and fill work. | Exact queue/fill parity tests. |
| P1 | Speed | Replace full `L3Book.check_invariants()` after every event with incremental checks in product mode. Keep full checks in validation/audit mode. | Remove an O(active orders) walk per event. | Periodic full-check comparison and fault-injection tests. |
| P1 | Load speed | Stream the event JSONL file. Do not use `read_text().splitlines()`, a full log list, a full position list, and a second sorted decoded list. | Lower first-run time and peak memory. | Record count, ordering, parity, and hash equality. |
| P1 | Repeat speed | Read an existing result only when engine version, complete normalized configuration, event hash, checkpoint hash, and SDK grid hash match. | Make an identical second click nearly immediate. | Cache-hit identity test and invalidation matrix. |
| P1 | SDK speed | Keep a small persistent Node SDK worker or cache canonical grids by normalized SDK input. | Avoid a new Node process for each run. | Grid byte equality for all shapes and fees. |
| P1 | Output speed | Let compact replay consume timeline records directly instead of writing and reading temporary JSONL. | Remove serialization and temporary disk I/O. | Byte-identical compact replay test. |
| P1 | Input bounds | Limit price-range width, fee-derived grid count, and result size before SDK generation. | Prevent an accidental grid with very many levels from using excessive CPU or memory. | Boundary and rejected-large-grid tests. |
| P1 | Output safety | Write summaries, timelines, checkpoints, and manifests with temporary files and atomic replacement. Write the manifest last. | Prevent a stopped process from leaving a valid-looking partial result. | Interrupted-write and recovery tests. |
| P1 | Packaging | Add every imported first-party module and required frontend or SDK asset to the install artifact, or state that the project is source-tree only. | Prevent an installed wheel from missing `compact_replays` and other tools. | Build and inspect a wheel, then start the installed API. |
| P2 | Replay speed | Process already sorted events as one stream with `itertools.groupby()` instead of building `by_block`. | Reduce memory and one full pass. | Event-order and block-boundary tests. |
| P2 | Pricing speed | Maintain best bid and ask indexes instead of rebuilding aggregate dictionaries for each midpoint. | Reduce repeated work in marks and charts. | Best-price parity after every event in test fixtures. |
| P2 | Throughput | Limit the service lock to interval-cache initialization. Use process workers for independent runs if concurrent runs are needed. | Allow parallel users and parameter sweeps. | Isolation, deterministic output, and memory-limit tests. |
| P2 | Browser speed | Precompute chart series and scales. Draw only the changed range or use cached paths. | Avoid complete prefix redraw on every animation frame. | Browser frame-time test with 1,500 frames. |

## Detailed speed findings

### 1. Full timeline records are built before sampling

`kuru_lp.run_lp()` calls `_timeline_record()` for every historical event. `_timeline_record()` serializes all pending and active simulated orders. Only after this work does the stride condition decide whether to retain the record.

Recommended design:

1. Update equity, utilization, range, and drawdown accumulators from raw state.
2. Decide whether the event needs a retained frame.
3. Build the full JSON record only for retained market states, activations, fills, first state, and final state.
4. Keep a streaming drawdown accumulator instead of an equity list.

This change must not alter any fill decision or final financial value.

### 2. Complete invariant checks run after every event

`L3Book.check_invariants()` walks every price queue and every active historical order. `assert_lp_invariants()` also sums all live simulated-order reservations. Both run inside the event loop.

Keep two modes:

- **Audit mode:** full checks after every event. Use this for reconstruction validation and CI fixtures.
- **Product mode:** check only the touched order, its two neighbors, its level endpoints and total, and changed reservations. Run a full check at the start, end, every configured checkpoint, and after any unexpected condition.

Do not remove full validation. Move it out of the hottest path only after incremental checks have fault-injection tests.

### 3. Simulated orders are repeatedly scanned and sorted

- `activate_orders()` scans all strategy orders.
- `update_queue_ahead()` scans all strategy orders and performs list membership checks.
- `apply_fills()` sorts all strategy orders for each Trade event.

Recommended indexes:

- pending orders in a heap or dictionary keyed by eligible block;
- active orders keyed by side and price;
- creation sequence at each simulated price level;
- reverse mapping from historical order ID to simulated orders that have it ahead;
- direct pair mapping for flip updates.

The current dictionary can remain the source of truth. Indexes can be derived state with invariant checks.

### 4. Event loading makes several full copies

`load_verified_source_events()` reads the complete JSONL file into one string, splits it into lines, stores decoded JSON logs, stores positions, then calls a decoder that builds and sorts another list.

Recommended first step:

- read the file line by line;
- validate monotonic chain positions against the prior position;
- decode each row once;
- append only the final `ReplayEvent` and timestamp entry;
- compute SHA-256 during the same read.

A later optional cache can store a compact MessagePack or Arrow representation with a schema version and source SHA. Do not use an unsafe pickle cache.

### 5. Exact repeat results are written but not read

The service saves `.cache/backtest_runs/<run_id>/summary.json` and `timeline.json`, but it always reruns the strategy.

A safe cache hit requires equality of:

- engine version;
- normalized configuration;
- market and interval bounds;
- exact event-file SHA-256;
- exact checkpoint SHA-256;
- SDK package version and git commit;
- canonical initial grid hash;
- timeline and summary schema versions.

Validate both saved hashes before returning a cached result.

### 6. One service lock covers the complete run

The server can accept concurrent HTTP requests, but `BacktestService.run()` holds one lock through event loading, SDK work, replay, compaction, hashing, and writes.

This is safe but serial. First reduce single-run cost. If concurrent demand appears, use a bounded process pool. A process boundary avoids shared mutable engine state and bypasses the Python global interpreter lock for CPU-heavy work.

### 7. The browser redraw cost grows with playback length

The price and equity charts rebuild arrays and draw every frame from the start through the current frame. The total work for one playback therefore grows close to the square of the frame count. Each draw also resizes and clears the canvas.

Precompute numeric chart arrays and fixed scales when a result arrives. Cache static grid lines and paths. During playback, draw only the required prefix or use an off-screen canvas. Measure frame time at the 1,500-frame limit.

### 8. The API response includes data that the current UI does not use

The response includes the full `fill_records` list and the compact timeline. A high-fill run can make JSON encoding, transfer, and browser parsing slow. Return the compact result in the main response. Put detailed fills in a separate local artifact or optional endpoint.

### 9. Stale browser requests still consume backend work

The request gate prevents an old response from replacing a new result. It does not stop the old HTTP request or Python replay. A long old run keeps the global service lock and delays the next run.

Add an `AbortController` in the browser. For real cancellation, use a job ID and a worker process that the service can stop safely. Do not interrupt shared in-process state.

### 10. Discovery resume still loads its complete cache

The discovery cache is append-only, uses `fsync`, and saves its cursor. On resume, it reads and sorts the complete JSONL cache to build the identity set. This can become slow for a large bounded range.

Stream the cache once and store a compact identity index or per-page manifest. Keep the JSONL file as the durable source. Retain the existing deduplication and cursor rules.

## Detailed accuracy findings

### 1. Event input does not have a recorded content hash

`validation_output/<interval>/summary.json` records the event path and record count, but not the event-file SHA-256. `BacktestService` uses the validation-summary SHA as `source_sha256`.

Add these fields:

```json
{
  "event_input": {
    "path": "source_events.jsonl",
    "record_count": 98178,
    "sha256": "...",
    "ordering": "block_number,transaction_index,log_index"
  }
}
```

Also hash the serialized checkpoint book and market parameters. This makes each run traceable to exact local inputs.

### 2. Mark-price policy needs an explicit product decision

The LP engine calculates midpoint from the explicit reconstructed L3 levels. The long-range reconstruction code also models Kuru vault liquidity and combined best bid and ask.

Choose and document one policy:

- explicit-book midpoint;
- combined contract `bestBidAsk()` midpoint;
- conservative liquidation mark: bid for long inventory and ask for short inventory;
- one reporting mark plus separate liquidation sensitivity.

Then validate that mark at distributed blocks. This affects P&L and risk metrics, not fill decisions.

### 3. Accounting should use raw integers until reporting

Prices and order sizes start as integers, but fills convert to `Decimal` display units before cash and inventory accounting. The contract can round token transfers at integer boundaries.

Use raw MON and USDC token units for:

- balances;
- reservations;
- fill notional;
- maker fees and rebates;
- fixed action costs.

Record the rounding direction for each operation from the contract or official SDK. Add fixtures for minimum size, partial fills, very small fees, and negative maker fees.

### 4. Exposure metrics are event-weighted

`time_in_range_percent` counts market events in range. `capital_utilization_percent` averages timeline records. A busy minute therefore has more weight than a quiet minute. A fill can add another sample at the same block timestamp.

Use elapsed seconds between consecutive block timestamps. State after event N should apply until the next known timestamp. State the final-interval convention. Keep event-weighted values only as separate diagnostics if useful.

### 5. Same-price simulated FIFO is not explicit

Initial SDK grids usually have unique prices. Flip prices can still converge. The fill sort uses price and string order ID. String order IDs do not preserve numeric creation order after 9.

Add an integer `placement_sequence`. Maintain simulated queue heads and tails by side and price. Include earlier simulated orders in the queue ahead of later simulated orders. This gives exact behavior when several simulated orders share one price.

### 6. Flip causality needs one contract trace fixture

The engine creates a flip after a causal fill and blocks it from the same transaction. This matches the current documented assumption. Add one real transaction fixture that proves:

- emitted event order;
- flip order ID and price;
- size conversion and rounding;
- queue position;
- earliest transaction in which the flip can fill.

This is more useful than more synthetic flip tests.

### 7. Counterfactual impact remains a limitation

The engine caps simulated participation and reports displaced downstream maker volume. It still assumes the historical taker order and all later events remain unchanged.

For larger positions, add sensitivity reports:

- 1, 5, 10, and 25 percent participation;
- position size as a percentage of visible same-side depth;
- number of transactions with displaced historical maker volume;
- P&L excluding flagged fills.

Do not present the last value as a corrected P&L. It is only a sensitivity bound.

### 8. Validation file paths are trusted too early

`load_verified_source_events()` joins the path from `summary.json` to the validation directory. It does not check the resolved result. A malformed or edited summary can point outside that directory.

Resolve both paths. Reject an absolute path and any resolved file that is not below the named validation directory. Then verify its content hash before parsing it.

### 9. Long-range reconstruction has no reorganization proof

`rebuild_from_chain.py` resolves `latest` once and rejects logs with `removed=true`. It does not keep a safety lag, store block hashes, or confirm that an earlier completed block still belongs to the canonical chain.

Add a configurable finality lag. Store the block hash for every completed chunk boundary. On resume, compare a small overlap of saved block hashes with RPC results before new events are applied. Stop on a mismatch and require an explicit recovery process.

### 10. Unknown market events can be ignored

`decode_log()` returns `Unknown` for an unrecognized topic. The long-range replay can then keep going without changing state. This is unsafe if a contract upgrade adds a new state-changing event.

Use a reviewed topic allow list for each known contract version. Fail closed on another topic from the target market. Record the contract bytecode hash or version boundary with each reconstruction segment.

### 11. Canonical result numbers need stricter rules

Some report values become Python `float` values before JSON hashing. Standard `json.dumps()` also permits `NaN` unless told not to. This weakens cross-version reproducibility and can create JSON that another parser rejects.

Use raw integers or canonical decimal strings in identity and accounting fields. Set `allow_nan=False`. Reject every non-finite value before output. Display-only float values can stay outside the identity.

### 12. Input size needs an early limit

The backend validates decimal precision but does not set a practical maximum for amounts, range width, or SDK level count. `Decimal.quantize()` can also fail for a value outside the current decimal context. A very wide range with a very small fee can create a very large grid before the engine starts.

Set documented amount and price bounds. Estimate or limit the SDK level count. Catch decimal range errors and return a clear input error. Check the generated order count again before replay.

### 13. Discovery ranking uses binary floating-point prices

Trade prices are stored as `float` for discovery movement calculations. This does not affect the verified engine, but it can change a close interval ranking.

Keep the raw Trade price integer or a `Decimal`. Calculate first-to-last movement from those exact values. Keep display rounding only in the report.

### 14. Replay time has block resolution

All logs in one block share the same block timestamp. The timeline does not contain transaction-level time. State this in each replay schema. Do not infer order inside a block from time; use block, transaction, and log indexes.

### 15. Playback speed is frame speed, not historical time speed

The labels 1x, 10x, 50x, and 100x currently select frame rates. Compact frames have unequal historical time gaps. The labels therefore do not mean one, ten, fifty, or one hundred times chain time.

Use timestamp differences for playback, with a maximum pause for quiet periods. If frame-rate playback remains, rename the control so that its unit is clear.

### 16. Financial changes can be hidden inside one displayed frame

Compaction preserves each financial change in `financial_changes`. The UI renders the frame's final top-level state and does not step through the inner financial snapshots. Several financial changes can therefore appear as one visual change.

Expand these subrecords during playback, or define one display step for each financial change. Preserve their sequence order within a frame.

### 17. The amount driver is hidden state

The last edited token selects `base` or `quote` as the SDK amount driver. Two forms with the same visible MON and USDC values can produce a different grid if the edit order differs. The request does include the driver, but the choice is not an explicit form field.

Add a clear `Lock MON`, `Lock USDC`, or `Use both amounts` control. Show the selected driver in the normalized result.

### 18. Advanced LP settings are not configurable in the current form

The Advanced Simulation section shows inclusion latency, action cost, and impact threshold as read-only values. The request also fixes latency to 1, maker fee to 0, action cost to 0, and impact threshold to 25. Only the participation cap is editable.

This is safe for the first product slice, but the labels must not imply that all advanced settings are active inputs. Make them editable only when the backend validation and result identity cover them.

### 19. Browser number formatting can lose precision

`formatRaw()` converts an exact decimal string to `Number` for display. Price chart coordinates also convert raw integer values to `Number`. Current market values fit safely, but larger token values can lose integer precision.

Format large raw values as strings. For chart positions, subtract an exact local origin first and convert the smaller difference, or use a decimal library.

### 20. Output writes are not consistently atomic

Backtest results and validation reports write their final files directly. A stopped process can leave a partial JSON file. The discovery cursor is stronger because it uses append, `fsync`, and an atomic state update.

Use a file in the destination directory, flush it, and replace the final file. Write a manifest last. A cache reader must reject a directory without a complete manifest.

### 21. The install configuration is incomplete

`pyproject.toml` does not list all first-party modules that other packaged modules import. For example, `backtest_service.py` imports `compact_replays`, but `compact_replays` is not in `py-modules`. The frontend and SDK adapter assets are also outside the Python package model.

Choose one supported install form:

- a source checkout with a documented working directory; or
- a real package that includes all modules and required assets.

Build a wheel in CI and test the installed API from a clean directory.

## Test gaps found in the second pass

The current tests give strong coverage to queue rules, accounting, Envio pagination, SDK grid parity, and deterministic engine output. Add these tests before the related changes:

1. HTTP route tests for body limits, malformed JSON, input errors, engine errors, and response headers.
2. A browser or DOM test that edits a field, clears a stale result, sends two requests, and displays only the latest response.
3. Event-file and checkpoint tamper tests with exact SHA-256 checks.
4. Validation path traversal and absolute-path rejection tests.
5. Same-price simulated LP FIFO tests with more than ten orders and converging flips.
6. Time-weighted range and utilization tests with irregular block timestamps.
7. A real Kuru flip transaction trace fixture.
8. Finality, reorganization, contract-version, and unknown-topic tests.
9. Large-grid, extreme-decimal, non-finite JSON, and response-size tests.
10. Atomic-write interruption and incomplete-cache recovery tests.
11. A build test that installs the wheel and starts the API from outside the source tree.
12. A fixed performance test that reports each benchmark phase and fails on a material regression.

The frontend artifact tests are mainly static file and string checks. The service integration test calls `BacktestService` directly. These tests do not yet prove the complete browser-to-HTTP-to-engine path.

## Benchmark plan

Do not optimize from total wall time alone. Add timers for:

1. checkpoint JSON load and book restore;
2. event JSONL read and decode;
3. SDK process start and grid generation;
4. book copy and transaction-budget build;
5. event application;
6. simulated activation, queue updates, and fills;
7. invariant checks;
8. timeline record construction;
9. compaction;
10. hashing and disk writes.

Use the same two fixed configurations from `BACKTEST_REQUEST_PATH.md`. Run one cold process and at least three warm repeats. Report median, minimum, maximum, peak resident memory, event count, simulated order count, fill count, and retained frame count.

## Safe implementation order

1. Add exact event and checkpoint hashes. This changes identity, not fills.
2. Add timing instrumentation with no engine-state changes.
3. Avoid construction of discarded timeline records.
4. Stream event loading and remove the duplicate sort.
5. Remove temporary JSONL compaction I/O.
6. Add exact-result cache reads with the full identity.
7. Add simulated-order indexes without changing the dictionary source of truth.
8. Add incremental invariants while keeping audit mode.
9. Move accounting to raw integer token units.
10. Decide and validate mark policy and time-weighted metrics.
11. Add finality, version-boundary, and unknown-event rules before a larger historical backfill.
12. Optimize browser replay only after the returned engine data stays byte-stable.

After each step, require unchanged fill records for the verified intervals unless the step intentionally fixes an identified accuracy rule. If fills change, publish the first divergent block, transaction, event, queue state, and accounting state.
