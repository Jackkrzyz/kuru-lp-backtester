# Kuru LP Backtester

Backtest a Kuru concentrated-liquidity position against locally validated Monad history. The app uses the official Kuru SDK grid, reconstructed FIFO queues, and contract flip rules. It has no wallet connection or live execution.

The supported market is MON / USDC, `0x065c9d28e428a0db40191a54d33d5b7c71a9c394`. Current continuous validated coverage is **109092028–109142027**, inclusive. Interactive backtests use local data. They do not download history.

## Setup and start

Use Python 3.11 or later and Node.js. Run these commands from this repository in WSL:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev,reports,contract-tests]'
npm ci --ignore-scripts --prefix sdk_reference
npm run build:browser --prefix sdk_reference
python -m kuru_backtester serve --host 127.0.0.1 --port 8000
```

Open the URL printed by the server. If Windows cannot reach the WSL localhost address, use `--host 0.0.0.0`. Open the complete access URL printed by that command. Stop the server with Ctrl+C.

The existing `python backtest_server.py` command still works. After an editable install, `kuru serve` also works. Restart a running server after code updates.

The base install, `python -m pip install -e .`, is sufficient for the app. The `reports` extra adds chart and Parquet libraries. The `dev` extra adds pytest. The `contract-tests` extra adds the local EVM test dependencies.

**Local history is required.** Git stores manifests, small reports, SDK fixtures, and code. It does not store the raw event files, local checkpoints, or contract bytecode cache. A new clone must prepare these files before it can run historical backtests. See [historical coverage](docs/HISTORICAL_SYSTEM.md) and [contract checks](docs/LP_CONTRACT_PARITY.md). A Trade chart alone is not backtest data.

## History and credentials

Copy `.env.example` to `.env`, then enter your Envio token in that file. Load it in the terminal that will run an indexing command:

```bash
set -a
source .env
set +a
```

The Python commands do not automatically read `.env`. Git ignores `.env` and its private variants. Envio credentials are needed for downloads, not local backtests or replay playback.

To inspect a full-history download plan without network calls:

```bash
python -m kuru_backtester history --to-finalized --download-only \
  --job-root .cache/full_history_archive
```

Adding `--execute` starts the download. Use the same command to resume it. Read [the full-history guide](docs/FULL_HISTORY.md) first: the deployment contract implementation is not yet validated. Download completion does not establish validated L3 coverage.

To display progress for that job:

```bash
python -m kuru_backtester monitor \
  --job-root .cache/full_history_archive --port 8002
```

The compatibility commands `python full_history.py` and `python full_history_monitor.py` still work.

## Project layout

```text
kuru_backtester/
  engine/         Verified queues, reconstruction, LP rules, and accounting
  history/        Download, coverage, checkpoints, validation, and promotion
  server/         HTTP API, job workers, SDK broker, and progress monitor
  tools/          Regression checks, benchmarks, chart export, contract checks
  legacy/         Early tools kept for regression and data reproduction
  compact_replays.py
frontend/         Current LP app and indexing monitor
sdk_reference/    Pinned official SDK adapter, fixtures, and browser tests
tests/            Python tests and small fixtures
data/reports/     Small recorded discovery and golden-regression reports
docs/             Current guides; old reports are in docs/archive/
```

The two manifests stay at the root. Their data paths are relative to the root. Local `.cache/`, `validation_output/`, `strategy_output/`, `lp_output/`, and `multi_regime_output/` paths stay in place so existing results and jobs remain usable. Large old demo replays now live under `tests/fixtures/legacy_replays/` and are ignored. The app does not serve or load them.

## Commands and checks

```bash
python -m kuru_backtester --help
python -m kuru_backtester index --help
python -m kuru_backtester validate --help
python -m kuru_backtester promote --help
python -m pytest -q
npm run test:adapter --prefix sdk_reference
```

With the validated local data installed, run all three LP golden comparisons:

```bash
python -m kuru_backtester regression
```

This writes `data/reports/lp_contract_regression_report.json`. Use the benchmark command with a bounded event prefix to measure a change:

```bash
python -m kuru_backtester benchmark --event-limit 10000 --product-mode
```

The full test suite uses the local validated history and contract cache. Two extra tests check archived demo replays when those optional local files exist. They skip on a clone without those large files. Core grid, queue, accounting, and request tests do not depend on demo replays.

## Documentation

- [Architecture and module guide](docs/ARCHITECTURE.md)
- [Form, job, engine, and replay path](docs/BACKTEST_REQUEST_PATH.md)
- [Local historical coverage and validation](docs/HISTORICAL_SYSTEM.md)
- [Full-history download and monitor](docs/FULL_HISTORY.md)
- [Official SDK reference](docs/SDK_REFERENCE.md)
- [LP contract behavior and limits](docs/LP_CONTRACT_PARITY.md)
- [Cleanup results and remaining work](docs/MAINTENANCE.md)

Historical results do not prove future fills or profit. Simulated orders do not change later historical taker behavior. The app reports transaction impact. Gas costs are excluded. See the contract guide for the exact tested scope.
