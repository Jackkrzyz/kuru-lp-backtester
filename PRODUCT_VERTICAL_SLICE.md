# Product Vertical Slice

Run date: 2026-10-06

## Frozen inputs

The product layer does not change reconstruction, queue rules, fill decisions, accounting, or strategy parameters. It reads the existing deterministic summaries and timelines.

There is no wallet connection and no live order execution.

## Generated-data safety

The `.gitignore` file contains these rules:

```text
.cache/
validation_output/**
!validation_output/**/
!validation_output/**/summary.json
!validation_output/**/manifest.json
!validation_output/**/report.md
strategy_output/**
!strategy_output/**/
!strategy_output/**/summary.json
!strategy_output/**/manifest.json
multi_regime_output/timelines/**/timeline.jsonl
```

The local raw files were not deleted.

`git check-ignore` was run through a temporary audit repository because this workspace has no `.git` directory. It confirmed the raw-data rules and their small-file exceptions. It did not ignore these small files:

```text
multi_regime_output/comparison.json
multi_regime_output/manifest.json
multi_regime_output/timelines/<interval>/<scenario>/summary.json
multi_regime_output/timelines/<interval>/<scenario>/manifest.json
```

`git status --short` cannot run in the workspace because the workspace is not a Git worktree. The command returns `fatal: not a git repository`. No repository was initialized as part of this task.

The size audit found 14 generated files at or above 100,000,000 bytes. `git check-ignore` matched all 14 files.

## Compact replay command

```bash
python compact_replays.py \
  --comparison multi_regime_output/comparison.json \
  --interval-manifest interval_manifest.json \
  --output-dir frontend/public/data \
  --max-frames 1500
```

The generator preserves every placement, activation, cancellation, fill, and financial-state change. It stores these records inside compact frames. It samples only market-state records that have no strategy action. Each replay contains the first and final state.

The generator validates the final cash, inventory, marked value, strategy P&L, and hold P&L against the raw summary.

## Raw and compact sizes

| Interval | Scenario | Raw records | Frames | Raw bytes | Compact bytes | Reduction |
|---|---|---:|---:|---:|---:|---:|
| Quiet | uncapped | 76,399 | 1,500 | 116,439,352 | 6,586,843 | 94.3431% |
| Quiet | 25% | 76,401 | 1,500 | 118,615,841 | 6,628,786 | 94.4116% |
| Quiet | 10% | 76,401 | 1,500 | 118,592,070 | 6,626,855 | 94.4121% |
| Quiet | 5% | 76,402 | 1,500 | 121,335,888 | 6,701,576 | 94.4768% |
| Active | uncapped | 105,527 | 1,500 | 146,641,196 | 7,821,102 | 94.6665% |
| Active | 25% | 105,530 | 1,500 | 146,821,459 | 7,827,833 | 94.6685% |
| Active | 10% | 105,530 | 1,500 | 146,744,621 | 7,809,848 | 94.6779% |
| Active | 5% | 105,530 | 1,500 | 146,754,448 | 7,806,320 | 94.6807% |
| Directional | uncapped | 103,898 | 1,500 | 169,584,616 | 8,702,768 | 94.8682% |
| Directional | 25% | 103,904 | 1,500 | 165,886,973 | 8,596,383 | 94.8179% |
| Directional | 10% | 103,908 | 1,500 | 167,582,917 | 8,643,394 | 94.8423% |
| Directional | 5% | 103,910 | 1,500 | 165,069,779 | 8,579,846 | 94.8023% |

Totals:

```text
raw records:       1,143,340
compact frames:       18,000
raw bytes:      1,730,069,160
compact bytes:     92,331,554
```

Artifact hashes are in `frontend/public/data/manifest.json`.

## Data scopes

Broad Trade discovery and order enrichment are separate.

Broad Trade discovery:

- Range: 109092028-109142027.
- Purpose: rank local windows.
- Event filter: Trade only.
- The two-million-block scan was not run.

Candidate order enrichment:

- Quiet: 109132028-109142027, with 69,375 order events.
- Active: 109097028-109107027, with 97,016 order events.
- Directional: 109117028-109127027, with 95,763 order events.
- Purpose: fetch dense order events only for the three final candidate intervals.

This separation also appears in `frontend/public/data/catalog.json`.

## Frontend

The static frontend is in `frontend/`. It provides:

- the required strategy inputs and deterministic scenario selection;
- reconstructed bid, ask, and midpoint charts;
- simulated quote lines and fill markers;
- strategy equity and hold-baseline charts;
- play, pause, scrub, 1x, 10x, 50x, and 100x controls;
- final strategy, hold, risk, fill, action, and inventory results;
- clear impact warnings for uncapped and flagged runs.

The default is `relatively_active` with the `max_10_percent` scenario. The uncapped scenario has a clear warning.

Playback uses `requestAnimationFrame` in the browser. It does not call the backtest engine.

## Verification

```text
compact generator tests: pass
all 12 artifact contract checks: pass
JavaScript syntax check: pass
browser load: pass
play and pause: pass
scenario switch: pass
uncapped warning: pass
complete test suite: 97 tests passed
compact artifact hash mismatches: 0
catalog SHA-256: 0e15ba71ebd25cdce38a9789e34933af6481935e41d71c9168f33ee1bf460147
```
