# Kuru LP request path

## Scope

The Kuru LP form now runs a new local strategy simulation. It does not load the saved LP summary or timeline. The verified reconstruction code and validated source event files are unchanged.

## Request path

1. `frontend/app.js` submits the selected `from_block` and `to_block`. It gets the local reconstructed midpoint before `from_block` and converts the lower and upper range percentages to outward tick-rounded absolute limits. The range can also use direct absolute limits.
2. The browser build of the canonical Kuru SDK adapter generates the exact position. It validates every final integer order size against the market `minSize`.
3. The same grid supplies the preview, required token amounts, validation details, and `order_grid` in `POST /api/backtests`.
4. `backtest_server.py` creates an isolated job and returns HTTP 202 with a job ID and owner token.
5. The browser polls `GET /api/backtests/{job_id}`. It shows the current stage, event count, percentage, elapsed time, and estimate.
6. `backtest_worker.py` runs `BacktestService.run()` in a separate process.
7. `backtest_service.py` calculates the reference and tick-rounded limits again and normalizes every submitted field. A private local broker keeps one official SDK `0.0.95` Node.js adapter process warm across isolated jobs. If the broker stops, the worker uses the same adapter in a one-shot process.
8. The backend rejects a reference-price mismatch, range mismatch, SDK-grid mismatch, or final order below `minSize`. A minimum-size failure has a structured code and order details.
9. `HistoricalEventStore`, `CheckpointStore`, and `ReconstructionEngine` load the requested covered range from local storage. They reconstruct the state before `from_block`, then `ReplayCursor` streams through `to_block`. The run does not call Envio or RPC. The verified JSONL files stay authoritative.
10. `kuru_lp.run_lp()` applies contract admission checks to initial grid orders and places accepted orders at the queue tail. Initial orders use the pre-interval state and enter at the first selected block. It applies queue priority, partial fills, exact token rounding, capital reservations, maker rebates, and repeated flip orders. Internal flips can trade after creation in the same transaction. There is no artificial participation cap.
11. The worker compacts replay records directly in memory. It does not write and read a temporary timeline JSONL file. It writes an atomic result, a separate detailed-fill file, and a completion marker. The browser gets the compact result from `GET /api/backtests/{job_id}/result`. An optional client gets detailed fills from `GET /api/backtests/{job_id}/fills`.
12. The browser replaces all metrics and playback frames with that result.

The job can be canceled with `DELETE /api/backtests/{job_id}`. A saved result is reused only when the complete normalized configuration, exact SDK grid, event hash, checkpoint hash, market parameters, engine version, and replay schema version match. The cache requires atomic completion metadata.

## Start the product slice

```bash
python -m kuru_backtester.server.backtest_server --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000`.

If the browser runs on Windows and the server runs in WSL, bind the server to all WSL interfaces:

```bash
python -m kuru_backtester.server.backtest_server --host 0.0.0.0 --port 8000
```

Open the complete access URL printed by the server. It includes the detected WSL IP address and sets the browser access cookie. The server allows two active jobs by default and stops its workers when it closes.

## Integration configurations

Both configurations use `relatively_active`, initial inclusion at its first block, no artificial participation cap, the market's historical maker rebate (zero here), and no gas estimate. Older hashes below record the former engine. See `LP_CONTRACT_PARITY.md` for the current engine checks.

| Field | A | B |
|---|---:|---:|
| Shape | Spot | Bid-Ask |
| Range | 0.02710–0.02718 | 0.025–0.030 |
| Fee / flip spread | 0.01% | 1.00% |
| MON amount | 6,000 | 16,000 |
| USDC amount | 180 | 450 |
| Initial bids / asks | 20 / 20 | 9 / 10 |
| Fills | 217 | 5 |
| Flip orders created | 170 | 4 |
| Net P&L | +0.392862000283354 USDC | +8.22509965377009 USDC |
| Run ID | `lp-67fd1ef5c7fc93d966da` | `lp-61736a8486152a928b41` |
| Summary SHA-256 | `8a4e042b240db13721514c2e4d682d790bb753c5d331811bd06273f3f32e560f` | `2b6ada084f27d2b9b48325dc827123e49c1059eef5d18f1d6ce86a45dbd7fd1d` |
| Timeline SHA-256 | `299e48c2340c20db484815714fa19b1425590e9b35dc2c4e5ff747903f6bce20` | `e68475f2c98d16a1b3f8501a235836b5baef4cef06e499dfa91e32d1f0f77f4d` |
| Semantic timeline SHA-256 | `47d5b1f07bafef9fbbd53c032ef2591de3bdbc1de87ad675ddf477c480e1661c` | `1c509e6e3b87faa5a57070b301918989247b57bb885e7cc8061c4781247d2133` |

The integration test also runs A a second time. The run ID, summary hash, timeline hash, and compact timeline are identical.

## UI result safety

Any Kuru LP field change clears the visible result and marks it stale. A request token includes the configuration revision. The browser ignores an old response after a newer request, field edit, or mode change. Backend errors stay visible. There is no LP demo fallback.

An edited form also cancels its current job. The UI cannot use an old result for a new configuration. A local job has one of these states: `queued`, `running`, `completed`, `failed`, `canceled`, or `stalled`.

The product contains only Kuru LP. The old cancel-and-replace research artifacts are no longer loaded by the frontend.

## Position validity

The UI checks the exact SDK grid after a shape, range, fee, token amount, or interval change. It marks each below-minimum preview bar and disables **Run backtest**. It also shows the smallest order, market minimum, invalid count, first invalid side and price, and deposit shortages.

**Auto-fill amounts** keeps the selected token amount and calculates the paired token ratio. **Use minimum valid amounts** searches the selected driver's raw integer amount. It doubles until the SDK grid passes, then binary-searches for the smallest passing amount. It regenerates the final result and checks that one raw unit less fails.

The contract checks `minSize` for each initial user-provided flip order. The contract's internal flip path does not repeat this check. A small partial fill can create one contract-valid internal paired order below `minSize`. Later partial fills add to that same order. The simulator records this state as an internal flip and never treats it as an invalid initial order.

See `PERFORMANCE_DIAGNOSTIC_REPORT.md` for the large-grid benchmark, parity hashes, and browser verification.
