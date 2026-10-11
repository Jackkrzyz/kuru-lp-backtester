# Full L3 Kuru Backtesting Plan

> Historical development record. Some settings, commands, and results describe earlier engines. See the [current project guide](../../README.md) and [LP contract checks](../LP_CONTRACT_PARITY.md) before using this document.

## Objective

Build a deterministic, verified L3 replay system for liquidity-providing strategy backtests on the Kuru MON-USDC market.

The first supported market is:

```text
0x065c9d28e428a0db40191a54d33d5b7c71a9c394
```

The system must reconstruct every active explicit order, FIFO queue position, price level, and Kuru AMM vault state from market deployment to a selected end block. It must let a backtest insert simulated orders and calculate queue-based partial fills without future information.

Envio HyperSync will be the primary event source. Historical Monad RPC calls will be an independent verification source.

## Current project state

### Implemented

- `analyze_l2.py` downloads a published Kuru daily L2 Parquet file.
- The daily parser handles empty or malformed books safely.
- The daily tool calculates best bid, best ask, midpoint, spread, and spread percentage.
- The daily tool saves a midpoint and spread chart.
- `rebuild_from_chain.py` starts from the verified market deployment block `43,660,999`.
- The chain tool decodes order, flip-order, cancellation, trade, vault, and market-state events.
- The chain tool applies explicit order changes and Kuru AMM vault formulas.
- SQLite stores the current active orders, aggregate price levels, vault state, metadata, and a resume checkpoint.
- Parquet stores raw and decoded event rows and event-block top-of-book rows.
- Each completed chunk checks the reconstructed best bid and ask against historical `bestBidAsk()` contract state.
- The fetcher retries network errors and reduces rejected RPC block ranges.
- `discover_interval.py` found a compact active range at blocks `44,853,800` through `44,856,100`.
- `l3_vertical_slice.py` has an Envio HyperSync HTTP adapter with explicit pagination. Envio is the default event source.
- The same runner keeps RPC as the raw event-parity and historical contract-state source.
- The vertical-slice L3 book stores exact previous and next links, queue heads and tails, and level totals.
- Queue creation, cancellation, partial fill, full fill, and flip-order size updates run in exact log order.
- The runner checks queue links, cycles, reachability, total sizes, sides, and prices.
- A replay cursor records the last applied block, transaction index, and log index.
- A passive-order overlay places one order at a real queue tail and calculates a queue-aware counterfactual fill and gross marked P&L.
- `reconstruction_validation_report.py` produces separate price and error charts, numerical validation results, a final summary, and a deterministic timestamped timeline.
- `strategy_vertical_slice.py` provides the first parameterized strategy consumer without changing the reconstruction engine.
- The strategy supports starting cash and inventory, bid and ask offsets, order size, inventory limits, cancel/replace thresholds, maker fees, and fixed order-action fees.
- The recorded default strategy run produced 744 deterministic timeline records and 400 MON of queue-aware fills on the verified interval.
- There are 31 passing unit tests.

### Local data state

- The saved checkpoint is block `43,662,001`.
- The local chain dataset contains two deployment or proxy events.
- It contains one event-block snapshot.
- It contains zero active orders because the seed run ends soon after deployment.
- The full historical backfill has not run.
- The Envio-backed vertical slice loads a separate historical checkpoint directly from contract storage at block `44,853,799`.
- That checkpoint has 265 active orders, 82 bid levels, and 162 ask levels.
- Envio and RPC each returned the same 598 ordered events, with zero events missing from either source.
- The Envio-backed replay matched `getL2Book()` at nine blocks with zero midpoint, price-level, and aggregate-size error.

### Important limits

- Envio/RPC event parity is proved for blocks `44,853,800` through `44,856,100`. This proof does not yet cover other intervals.
- The exact queue model is in the vertical-slice runner. The older full-history SQLite schema still does not store previous and next links or queue heads and tails.
- The replay cursor is an in-memory vertical-slice API. It does not yet load periodic local checkpoints.
- Snapshot Parquet files are top-of-book data. They are not L3 snapshots.
- Current verification covers top-of-book only. It does not prove every deep level or queue.
- Contract upgrade events are preserved as unknown raw events, but implementation versions are not applied to versioned reconstruction rules.
- The strategy vertical slice has configurable maker and action fees, inventory limits, and cancel/replace behavior. It does not yet model latency, gas, failed transactions, vault quotes, more than one quote per side, or a broad performance study.

## Target architecture

### 1. Envio ingestion layer

Use the Envio Python `hypersync` client with the Monad endpoint:

```text
https://monad.hypersync.xyz
```

Read the token from `ENVIO_API_TOKEN`. Never save the token in source files or output data.

The Envio adapter will request only the required market logs and block or transaction fields:

- block number, hash, parent hash, and timestamp;
- transaction hash and transaction index;
- log index;
- contract address;
- topics and data.

It will page with Envio's returned next block value. It will write canonical raw batches before reconstruction. The existing RPC fetcher will remain available as a fallback and test reference.

### 2. Canonical event model

Use one stable event identity:

```text
chain_id:block_number:transaction_index:log_index
```

Store raw log data and decoded fields. Order all events by block number, transaction index, and log index. Reject duplicates with different content.

Required records:

- block metadata;
- raw event;
- decoded order event;
- order delta;
- price-level delta;
- vault delta;
- contract implementation interval;
- validation result;
- data-part manifest with a content hash.

Use integers for all contract prices and sizes in the reconstruction core. Convert to decimal values only at an analysis boundary.

### 3. Exact FIFO L3 state

Extend each explicit order with:

- order ID;
- owner;
- remaining size;
- price;
- side;
- previous order ID;
- next order ID;
- flip order ID;
- flip price;
- creation event identity;
- last update event identity;
- status and removal reason.

Extend each price queue with:

- side and price;
- head order ID;
- tail order ID;
- order count;
- total remaining size.

Apply creation at the queue tail. Apply a cancellation by linking its previous and next orders. Apply fills only to the queue head. Preserve partial fills at the head. Apply flip-order creation and updates in exact log order.

Run these invariants after each event in tests and at configured intervals in production:

- no negative size;
- no duplicate active order ID;
- head has no previous order;
- tail has no next order;
- every forward link has the correct reverse link;
- a queue has no cycle;
- order count and total size match a queue traversal;
- empty queues have null head and tail;
- every order belongs to exactly one queue;
- best explicit prices match the set of nonempty queues.

### 4. Kuru AMM state

Keep the AMM vault separate from explicit L3 orders. Store its exact integer state after every vault change or vault trade.

Track:

- best bid and ask;
- bid and ask order sizes;
- bid and ask partial-fill sizes;
- spread constant;
- the implementation version used for each transition.

Test full and partial fills in both directions, multiple vault levels in one transaction, and explicit-book or vault price ties.

### 5. Contract upgrade handling

Index proxy upgrade events from deployment onward. Resolve the implementation address and block interval for each version. Save the bytecode hash and, when available, the verified source revision.

Reconstruction logic that depends on contract code must be versioned. A new implementation must not use old AMM or flip-order assumptions without a compatibility test.

### 6. Historical storage and replay

Do not save a complete copy of every active order after every event. That format would be too large.

Use:

- append-only event and delta Parquet files;
- periodic complete L3 checkpoints;
- a manifest with block bounds, row counts, hashes, and schema version;
- a replay reader that loads the closest checkpoint and applies later deltas.

A replay cursor must expose the book before and after each event. It must also expose the final state of each block.

### 7. Verification system

Use several independent checks.

#### Source parity

- Compare Envio and archive RPC event counts for fixed ranges.
- Compare every event identity, topic, data field, transaction hash, and block hash.
- Test empty, sparse, and active ranges.

#### Top-of-book parity

- Keep the current `bestBidAsk()` check at every completed chunk.

#### Full L2 parity

- Call historical `getL2Book()` at deterministic sample blocks.
- Compare every explicit price and aggregate size.
- Compare combined top-of-book after adding the vault.
- Compare available dates with Kuru's published L2 snapshots.

#### Full L3 parity

- Read the on-chain price-point head and tail from `s_buyPricePoints` and `s_sellPricePoints`.
- Traverse each queue through the public `s_orders(orderId)` getter.
- Compare order owner, size, price, side, previous ID, next ID, flip ID, and flip price.
- Compare every active local queue at selected historical blocks.
- Compare `s_orderIdCounter` and test a sample of inactive order IDs.

#### Vault parity

- Compare all fields returned by historical `getVaultParams()`.

#### Determinism

- Rebuild the same fixed range twice in clean directories.
- Require identical manifests and content hashes.
- Stop before checkpoint commit if any check fails.

### 8. Backtest engine for liquidity provision

The backtester will consume the L3 replay cursor. It will not query future events.

The first strategy interface will support:

- observation at a defined event or block boundary;
- place bid or ask limit order;
- cancel an active simulated order;
- configurable decision and chain latency;
- configurable post-only behavior;
- multiple quote levels;
- inventory-based quote skew.

The fill simulator will:

- insert a passive simulated order at the queue tail;
- track volume ahead by exact order IDs and sizes;
- apply cancellations ahead without treating them as trades;
- apply partial and complete maker fills;
- handle a strategy cancel that is pending during new trades;
- model Kuru explicit-book and AMM price priority;
- use the historical fee parameters for the applicable block;
- record gas as a separate cost input;
- never grant fills from hidden future data.

Initial reports will include:

- gross and net PnL;
- realized and unrealized PnL;
- inventory and inventory risk;
- maker fill count and volume;
- queue waiting time;
- captured spread;
- adverse selection after a fill;
- cancel-to-fill and order-to-fill ratios;
- fees and gas costs;
- maximum drawdown.

## Work phases and acceptance criteria

### Phase 1: Envio source adapter

Deliverables:

- Envio dependency and configuration;
- asynchronous HyperSync adapter;
- canonical raw event schema;
- pagination and resume tests;
- Envio-versus-RPC parity test on a known active range;
- no API token in logs or files.

Acceptance:

- the active sample range returns the same ordered events from Envio and RPC;
- a resumed run produces no duplicate or missing event;
- current unit tests still pass.

### Phase 2: FIFO reconstruction core

Deliverables:

- queue-aware schema;
- migration or clean-rebuild procedure for the small seed database;
- exact linked-list mutation logic;
- invariant checks and focused flip-order tests.

Acceptance:

- every synthetic queue test passes;
- a create, partial fill, full fill, cancel, flip, and paired-liquidity sequence produces the expected links and totals.

### Phase 3: Checkpoints and replay API

Deliverables:

- versioned Parquet delta schemas;
- periodic L3 checkpoint format;
- manifest and content hashes;
- replay cursor for event and block boundaries.

Acceptance:

- replay from the start and replay from a checkpoint produce identical state hashes.

### Phase 4: Independent verifier

Deliverables:

- Envio/RPC parity checker;
- historical L2 contract checker;
- historical L3 queue traversal checker;
- vault checker;
- published L2 comparison report.

Acceptance:

- all selected checkpoints pass with no order, level, queue, vault, or block-hash difference.

### Phase 5: Full historical backfill

Deliverables:

- full event and delta dataset from block `43,660,999` to a fixed end block;
- checkpoint files;
- validation report;
- run manifest with versions and hashes.

Acceptance:

- no missing block interval;
- no failed invariant;
- all configured verification checkpoints pass;
- a clean replay reaches the same final state hash.

### Phase 6: Liquidity-provider backtester

Deliverables:

- strategy interface;
- exact queue-position fill simulator;
- latency, fee, gas, and inventory accounting;
- one simple symmetric quoting strategy;
- deterministic result report.

Acceptance:

- hand-built scenarios produce exact expected fills and PnL;
- changing latency or queue position changes fills in the expected direction;
- repeated runs with the same inputs produce identical results.

## Definition of full L3 version 1

The dataset is full L3 version 1 only when all these conditions are true:

- it contains every canonical market event from deployment to the selected end block;
- every active explicit order and FIFO link can be reconstructed at any event boundary;
- all explicit price queues and aggregate sizes are correct;
- Kuru AMM state is exact;
- contract implementation changes are accounted for;
- replay is deterministic and checkpoint-safe;
- Envio and archive RPC samples agree;
- historical top-of-book, L2, L3, and vault verification checks pass;
- the backtester can insert a simulated passive order and calculate queue-based fills without future information.

## Next authorized work block

Work starts only after a direct user instruction in this chat.

1. Re-read this plan and inspect the project without changing existing seed data.
2. Check for `ENVIO_API_TOKEN` without printing its value.
3. Add the Envio `hypersync` client and an ingestion interface shared by Envio and RPC.
4. Add a canonical raw event model with block hash and parent hash.
5. Fetch a small deployment range and a known active range with Envio.
6. Compare the Envio result with the existing RPC result event by event.
7. Add automated pagination, ordering, duplicate, and resume tests.
8. Update this plan and the README with verified results and any Envio limits found.

If no Envio token is available, complete the adapter and offline tests, then report that the live Envio parity check needs `ENVIO_API_TOKEN`.

## Out of scope for these phases

- a frontend;
- live trade execution;
- private-key management;
- production market-making deployment;
- non-Kuru markets;
- statistical strategy optimization before data correctness is verified.
