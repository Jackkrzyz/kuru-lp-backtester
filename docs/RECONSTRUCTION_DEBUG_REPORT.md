# Reconstruction and readiness checks

Report recorded on 2026-10-13. All tests used local canonical events.
Archive RPC calls checked historical Monad state. No Envio backfill was started.
Existing raw archives were retained.

## Failures found and changes

The queue stopped on `VaultParamsUpdated` at block **44,853,836**.
RPC confirmed nonzero vault sizes. This activity is outside the current
reconstruction profile. A later event disables the vault at **44,855,470**.
The partition remains unavailable; its activity is not silently ignored.

The validator now detects this unsupported activity before RPC work. The
saved-data queue records the gap and checks later partitions from independent
checkpoints. Corrupt files, event parity failures, and L2 failures still stop it.

Verified partitions were previously saved outside the LP index. Supported
partitions now receive additional checkpoint, full market-parameter, vault,
and boundary-hash checks before publication. The service reloads the index
when it changes. Previously checked saved partitions receive the readiness
check during queue planning. Older runtimes remain reconstruction-only.

The broad local chart index made repeated WSL/Windows metadata calls.
Directory entries now supply link and size metadata. State files are read once.
Coverage is merged into continuous ranges while preserving gaps.

The main chart now reads only approved backtest partitions. It checks saved
hashes and LP rules. Archive candles have a separate cache and cannot be used
as a ready-chart fallback. Coverage color bands are removed. Period selection,
UTC dates, and exact block fields remain. The data panel retains download and
verification coverage. Broad local Trade discovery is an explicit action.

Trade price statistics now decode only Trade events. Strategy replay still
receives every event. Its queue, fill, flip, and accounting logic is unchanged.

## Real bounded results

| Blocks | Result | Event parity | L2 checks | Errors |
| --- | --- | --- | --- | --- |
| 44,850,999–44,855,998 | Unsupported vault gap | Not certified | Not certified | No false readiness |
| 44,855,999–44,860,998 | Reconstruction verified | 2,883 / 2,883; missing 0 | 7 / 7 | Price, size, midpoint, queue failures: 0 |
| 109,142,028–109,147,027 | Reconstruction verified and LP ready | 28,019 / 28,019; missing 0 | 7 / 7 | Price, size, midpoint, queue failures: 0 |

Both passing partitions matched the full final RPC L3 checkpoint and passed
the individual/summary cancellation audit. Summary events were not applied
as duplicate cancellations.

The two-partition queue returned one verified partition and one unsupported
partition. Its explicit verified range was **44,855,999–44,860,998**, so the
unsupported gap was not shown as verified. The passing old runtime is not LP
ready because its LP simulation has not passed the current parity tests.

Current continuous LP-ready coverage: **109,092,028–109,147,027**.
An existing service instance reloaded the new 5,000-block partition locally,
read all 28,019 events, and accepted the exact checkpoint rules.

## Measured costs

- Vault rejection: **0.162 seconds**, **0 RPC requests** after local preflight.
- Supported 5,000-block validation: **97.09 seconds**, including **65 seconds**
  of HTTP 429 waits. RPC checked 3,276 event-block timestamps. There were
  122 transport calls. Provider quotas still count work inside batches.
- Readiness capture and local range load: **2.29 seconds**, eight HTTP calls.
- Bounded queue with a vault gap: **40.86 seconds**, including a **14-second**
  rate-limit wait. It completed one partition and retained one gap.
- Broad Trade-index baseline: **239.83 seconds** for 9,270 source entries.
  It contained **4,880,280 Trades** and **215 daily candles**.
- Merged broad chart response: approximately **64 KB**, compared with
  **509 KB** with 9,268 separate coverage ranges. Trade count was unchanged.
- After the directory-metadata changes, the broad local index took
  **169.64 seconds**, compared with **239.83 seconds** before. It retained
  the same **4,880,280 Trades** and **215 daily candles**.
- First ready-chart build: **7.85 seconds** for seven approved partitions.
  Its one daily candle contains **3,386 Trades**. Response size: **622 bytes**.
  This is a smaller approved scope, not a claim that the entire archive now
  reconstructs in eight seconds.
- Final local HTTP checks: ready-chart response **11.72 ms**; range metadata
  for the new 5,000-block partition **849.22 ms**, including all 28,019 events.

The broad profile measured 135.6 seconds in archive discovery, 119.6 seconds
in `stat`, and 46.4 seconds in path resolution. These costs overlap and must
not be added. Local replay CPU was not the main verification delay.

## Artifacts and commands

Local diagnostic commands used the existing WSL virtual environment:

```bash
wsl env PYTHONPATH=. .venv/bin/python .cache/debug_reconstruction.py inspect
wsl env PYTHONPATH=. .venv/bin/python .cache/debug_reconstruction.py validate
wsl env PYTHONPATH=. .venv/bin/python .cache/debug_reconstruction.py validate 109142028
wsl env PYTHONPATH=. .venv/bin/python .cache/debug_reconstruction.py ready 109142028
wsl env PYTHONPATH=. .venv/bin/python .cache/debug_reconstruction.py queue
wsl env PYTHONPATH=. .venv/bin/python .cache/debug_chart.py
```

The scripts and generated artifacts are local diagnostics, not committed data.
Passing reports, checkpoints, captured RPC responses, and candles are under
`.cache/debug_verification/`. The bounded queue report is under
`.cache/data_suite/verifications/8f47d029c1ea503b/`.

Supported partition hashes:

```text
events:     0de8ecc777efb4c7d92d513daa0e64807aabcc2810c69025236226d593f49446
checkpoint: 93c81cdcb977c0f9a1cc207c38e1750407bd5820bf0a6bda2457fe54b2054540
end state:  5e3e1e411ce79a1f7e8a095c8debe1f394089f74b75a8d9656d59c9ff62c6ef8
```

Run regression tests in WSL with `python -m pytest -q`. The final suite passed
**378 tests and 14 subtests in 92.84 seconds**. The three existing golden
reports and strategy results remain regression gates. Browser checks showed
the ready-only daily chart, exact block inputs, removed coverage bands, and no
console errors. The screenshot is `.cache/ui/backtest-ready-chart.png`.
