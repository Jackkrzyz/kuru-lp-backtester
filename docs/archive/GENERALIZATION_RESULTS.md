# Generalization results

> Historical development record. Some settings, commands, and results describe earlier engines. See the [current project guide](../../README.md) and [LP contract checks](../LP_CONTRACT_PARITY.md) before using this document.

## Result

The product backtest path no longer requires a named regime. It accepts:

- market address;
- first block;
- last block;
- Kuru LP configuration and exact SDK grid.

Named quiet, active, and directional values remain as optional UI presets and golden tests. The preserved validation and old vertical-slice scripts are still present.

## Golden regression

`python golden_regression.py` compared each named wrapper with the generalized block-range path. The comparison included initial order counts, starting and final balances, fills, fill hash, flip count, turnover, P&L, drawdown, fees, result metrics, and semantic timeline hash.

| Fixture | Blocks | Fills | Net P&L | Semantic timeline SHA-256 | Status |
|---|---|---:|---:|---|---|
| relatively_quiet | 109132028–109142027 | 3 | -1.7207856192552258 | `9160f080ee5596f05ee8498c018cfd7b400960ecb836b6ac79c2803bc3eb2a45` | Pass |
| relatively_active | 109097028–109107027 | 11 | +2.299544797153841 | `f17361247d8e84907d86225240681787007b9b89db1f6ae86d66dc24cc155dd2` | Pass |
| directional | 109117028–109127027 | 15 | -5.047358612795431 | `949372799d8cc773f320b36ecd53fccbaccc932e2333107926e7d894f5822a87` | Pass |

All comparison difference lists are empty. The complete machine-readable report is `golden_regression_report.json`.

The generalized run ID and summary hash differ from the named wrapper because the normalized identity records `from_block`, `to_block`, and a block-range label. The timeline hash and semantic timeline hash remain equal. The old named-wrapper hashes remain unchanged.

## Arbitrary-range browser check

The browser submitted blocks 109100000 through 109100200 inside the active partition. The backend reported 1,490 local events and checkpoint block 109097027. It returned:

- run ID `lp-af43b0a25a4c51a17630`;
- 1,492 compact replay frames;
- position P&L `-0.0632` USDC in the UI;
- normalized first and last blocks equal to the submitted values;
- no browser console warning or error.

The UI also rejected a range that crossed a local L3 coverage gap. **Run backtest** stayed disabled.

## Test results

- Python: 144 tests passed and 12 subtests passed.
- Node.js: 2 test files passed.
- Python total time: 51.23 seconds on the final run.

The tests include checkpoint-boundary starts, starts between checkpoints, overlapping arbitrary ranges, each complete indexed range, unavailable ranges, repeated deterministic runs, golden content hashes, generalized engine parity, chart downsampling, index pagination identity, resume, and deduplication.

## Current coverage

The Trade chart and complete validated L3 coverage now both span blocks 109092028 through 109142027. Six adjacent partitions provide continuous coverage across all 50,000 blocks. Any interval inside these inclusive bounds is backtestable.

The three partitions that closed the gaps contain 49,175, 76,871, and 41,973 events. Each partition has exact Envio/RPC parity, seven passing L2 checkpoints, zero reconstruction error, zero queue-invariant failures, and a passing cancellation audit.

## Backfill estimate

From market deployment block 43,660,999 through local chart block 109,142,027, the observed sample density gives this estimate:

- mean: approximately 577 million events and 395 GB of canonical JSONL;
- observed range: 456 million to 643 million events;
- observed storage range: 310 GB to 440 GB.

This estimate excludes later blocks, checkpoints, indexes, validation output, and temporary storage. The complete lifetime backfill was not started.
