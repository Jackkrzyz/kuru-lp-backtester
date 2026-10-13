# Early-runtime reconstruction corrections

## Cause and scope

The failed job started at block `43660999`. Its implementation was not in the
supported list. Its runtime SHA-256 is
`fb70240e4197152779b6195ad0388f9d972a6c1f1945946f70b8165e6940c86b`.

The implementation address is `0xea2cc8769fb04ff1893ed11cf517b7f040c823cd`.
Its [verified OrderBook source](https://monadscan.com/address/0xea2cc8769fb04ff1893ed11cf517b7f040c823cd#code)
uses Solidity 0.8.30, 1,000 optimizer runs, and the Prague EVM target.

The first audit also found missing flip-link metadata. In
`_handleFlipOrderUpdate`, a partial fill can set the original order's
`flippedId`. A full fill can clear the surviving paired order's `flippedId`.
These assignments have no separate event. Price levels matched without the
assignments, but the final complete L3 checkpoint did not.

`history/reconstruction_profiles.py` now records these assignments only for
this runtime. All queue mutations still use the unchanged L3 engine. Local
reconstruction between checkpoints selects the profile from the validated
partition report. The later runtime path is unchanged. Strategy fills,
deposits, SDK grids, accounting, and LP contract gates are unchanged.

The deployment transaction also emits `Upgraded` and `Initialized`. The
downloader does not request these topics. Verification now checks their exact
known market, block, transaction, implementation address, and initialization
version. It saves both logs with a content hash in a separate audit. Market
event parity excludes only these two audited logs. Unknown topics and later
upgrade logs still fail parity. Missing or duplicate deployment logs fail.

## Real-chain checks on 2026-10-11

The checks used existing Envio files from download job `9c7c8bdf3da71999` and
independent public Monad archive RPC responses. No HyperSync request was made.
The source files were copied, not changed. No user download was stopped.

| Blocks, inclusive | Envio / RPC market events | Trades | Flip creation / internal creation / update | L2 checks |
| --- | ---: | ---: | ---: | ---: |
| 43660999–43665998 | 0 / 0 | 0 | 0 / 0 / 0 | 7 |
| 43920999–43925998 | 2101 / 2101 | 41 | 3 / 0 / 0 | 7 |
| 43930999–43935998 | 88 / 88 | 19 | 21 / 3 / 3 | 7 |

Results for all three ranges:

- Missing market events: **0** from either source.
- Maximum price-level, aggregate-size, and midpoint errors: **0**.
- Queue invariant failures: **0** across **21** checks.
- Final full L3 checkpoint matches: **3 of 3**.
- Cancellation-summary and timestamp checks: **pass**.
- Deployment administrative logs: **2**, audited separately.

The first and last block and five evenly spaced intermediate blocks were
checked in each range. The deployment range has an empty book, not a
trade-derived midpoint. Its zero error means no book difference.

The real verification CLI also passed on the flip interval:

```bash
python -m kuru_backtester verify-download \
  --source-job .cache/data_suite/jobs/9c7c8bdf3da71999 \
  --from-block 43930999 --to-block 43935998 \
  --job-root .cache/legacy_reconstruction_audit/verified_flip \
  --manifest full_history_verify_legacy_flip_manifest.json
```

Reports are under `.cache/legacy_reconstruction_audit/<first>-<last>/`.
The separate CLI output is under `.cache/legacy_reconstruction_audit/verified_flip/`.
These files do not replace the app's historical index or golden results.

## Repeat the tests

`tests/fixtures/legacy_reconstruction.json.gz` contains the bounded Envio input,
expected reports, and captured RPC responses. Block headers retain only number,
hash, and timestamp. Unrelated transaction lists are omitted. Tests fail if a
required response is absent; they cannot fall back to a network request.

```bash
python -m pytest -q tests/test_legacy_reconstruction.py
python -m pytest -q
```

Tests cover real event replay, exact final order metadata, repeatable reports,
partial and full flip fills, the return flip leg, arbitrary starting blocks,
unknown runtimes, active vault rejection, and strict deployment-log handling.

Complete suite result: **279 passed, 12 subtests passed**, in 72.84 seconds.
The existing three golden interval event and checkpoint hash tests pass.

## Retry the failed job

Use **Start / resume verification** for the same source and bounds. A new worker
loads the corrected Python modules. Do not restart the server or its download
only to load this correction. The worker keeps the original source files.

Alternatively, use `verify-download` with the failed job's original output
directory, manifest, and range. Do not change an existing job's identity.

This correction does **not** prove the complete `43660999–65000000` range.
Later unsupported upgrades, active vaults, missing events, or checkpoint
differences must still stop the job. It also does not enable old-runtime LP
backtests: those require their own contract-rule validation and coverage setup.

## Intermediate runtime correction — 2026-10-12

The next saved failure was verification job `ca3405377c9acfe5`. Its preflight
at block `69999999` found an unsupported implementation:

- Address: `0x4c0ba1ba8404076e2b09c7a5d014ec999c3a27e2`.
- Runtime SHA-256: `982bd98e31124ec14180f3eb44c51b1aae151af6674ba68e80b57a5ace428eb6`.

The explorer did not supply verified source for this version. The tests
therefore execute its actual captured runtime bytes in an isolated EVM. No
wallet transaction is sent. The runtime has the same reciprocal flip-link
assignments as the deployment version. Both exact hashes now use that adapter.
The current verified implementation still calls the unchanged event engine.
Unknown runtime hashes and active vault liquidity still stop validation.

The runtime test also found stale head predecessor storage. After a full fill,
the contract moves the price-point head but can retain the new head's old
`prev` slot. Cancelling that head can copy the stale value to its successor.
This inactive predecessor is not an order ahead in the live FIFO queue.

The adapter saves these raw slots in optional `book.contract_head_prev`
checkpoint metadata. Live queues retain strict head, tail, interior-link,
cycle, and aggregate-size checks. Only queue heads may have this metadata.
The predecessor must be older and absent from the live book. Checkpoint reads,
serialization, local reconstruction, and final RPC comparison preserve it.
Checkpoints without this metadata keep their existing bytes and hashes. Fully
filled normal orders can retain old mapping slots; reconstruction tracks
reachable active queues, not every historical storage slot.

Actual-runtime tests cover partial fills below the initial minimum, repeated
flip-size increases, complete fills, return-leg tail placement, stale head
cancellation, paired cancellations, exact active order fields, and checkpoint
save/load. A small compressed fixture also supplies real Envio events, RPC
responses, and runtime bytes for offline regressions. No test can silently
fetch a missing fixture response.

### Bounded chain validation

The checks read download job `a375573001adddfb`. Source files stayed unchanged.
No Envio request or full-range verification job was started.

| Blocks, inclusive | Envio / RPC events | Trades | L2 checks | Final active L3 match |
| --- | ---: | ---: | ---: | --- |
| 70000000–70004999 | 35659 / 35659 | 375 | 7 | pass |
| 79995000–79999999 | 30299 / 30299 | 1350 | 7 | pass |
| 70000000–70000199, offline fixture | 1766 / 1766 | 29 | 7 | pass |

Missing events, maximum price-level error, aggregate-size error, midpoint
error, and queue-invariant failures are all **zero**. Timestamp parity and
cancellation-summary audits pass. The two 5,000-block checks do not prove the
whole `70000000–80000000` download.

The real verification command also passed, using fresh RPC reads:

```bash
python -m kuru_backtester verify-download \
  --source-job .cache/data_suite/jobs/a375573001adddfb \
  --from-block 70000000 --to-block 70004999 \
  --job-root .cache/reconstruction_20261012/verified_70000000 \
  --manifest full_history_verify_intermediate_smoke_manifest.json
```

Artifacts are under `.cache/reconstruction_20261012/`. The manifest above is
separate from the app index. This correction does not approve intermediate
runtime LP accounting or automatically expand backtest coverage.

RPC rate limits occurred during the checks. The client now honors numeric and
HTTP-date `Retry-After` headers. Missing or invalid headers use bounded
exponential waits. Failed retries do not bypass parity or promote a partition.
Completed verification resumes now report the reconstructed block and saved
check count correctly.

To retry the user's failed full-range verification, use **Start / resume
verification** with its original source and bounds. The new worker loads the
fix. No existing download needs to be stopped or deleted.

### Final regression results

`python -m pytest -q` passed: **301 tests and 12 subtests**, in 72.61 seconds.
`git diff --check` also passed. The new compressed fixture is 251,344 bytes.
Raw diagnostic events and the separate verification manifest remain ignored.

The named-versus-generalized golden comparison was saved to
`.cache/reconstruction_20261012/golden_report.json`. Its entire parsed report
equals the existing `data/reports/lp_contract_regression_report.json`, including
all summary and replay hashes. The tracked report was not overwritten.

| Golden interval | Fills | Net P&L, USDC | Result and hashes |
| --- | ---: | ---: | --- |
| relatively_quiet | 3 | -1.7249738019380478 | unchanged |
| relatively_active | 11 | 2.292635767594539 | unchanged |
| directional | 14 | -5.088984279593537 | unchanged |
