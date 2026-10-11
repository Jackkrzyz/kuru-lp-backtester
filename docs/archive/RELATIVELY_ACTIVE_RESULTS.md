# Relatively Active Interval Results

> Historical development record. Some settings, commands, and results describe earlier engines. See the [current project guide](../../README.md) and [LP contract checks](../LP_CONTRACT_PARITY.md) before using this document.

This report covers only the named `relatively_active` interval. The two-million-block scan was not run.

## Interval

- Market: `0x065c9d28e428a0db40191a54d33d5b7c71a9c394`
- Blocks: `109097028` through `109107027`
- Checkpoint: `109097027`
- Historical Trade events: `1162`
- Historical order events: `97016`
- Historical executed volume: `2997622.9559281976 MON`
- Historical executed volume raw: `29976229559281976`
- Trade-derived price movement: `+1.9674430623%`

## Commands

```bash
python reconstruction_validation_report.py \
  --interval relatively_active \
  --validation-points 7

python strategy_vertical_slice.py \
  --validation-summary validation_output/relatively_active/summary.json \
  --output-dir strategy_output/relatively_active

python -m unittest discover -s tests -v
```

The strategy command has no strategy parameter overrides. It uses the current defaults.

## Reconstruction validation

- Event source: cached Envio HyperSync payloads, verified against RPC
- Replay order: block number, transaction index, log index
- Envio events: `98178`
- RPC events: `98178`
- Missing from Envio: `0`
- Missing from RPC: `0`
- Checkpoint active orders: `43`
- Checkpoint bid levels: `14`
- Checkpoint ask levels: `23`
- Checkpoint price error raw: `0`
- Checkpoint size error raw: `0`
- Maximum price-level error raw: `0`
- Maximum aggregate-size error raw: `0`
- Maximum midpoint error: `0`
- Queue-invariant checks: `98179`
- Queue-invariant failures: `0`

Validated blocks:

```text
109097028
109098694
109100361
109102027
109103694
109105360
109107027
```

Cancellation summary validation passed:

- `OrdersCanceled` summary events: `23166`
- Nonzero order IDs in summaries: `36849`
- Individual `OrderCanceled` events: `36849`
- Missing individual cancellations: `0`
- Duplicate individual cancellations: `0`
- Summary order IDs applied to the book: `0`
- Zero sentinel IDs ignored: `109`

## Default strategy result

Default configuration:

- Starting cash: `1000 USDC`
- Starting inventory: `1000 MON`
- Bid offset: `10 bps`
- Ask offset: `10 bps`
- Order size: `200 MON`
- Inventory limits: `0` to `2000 MON`
- Replace threshold: `1 bp`
- Inclusion delay: `1 block`
- Maker fee: `0 bps`
- Fixed action fee: `0`

Result:

- Fill records: `15`
- Filled size: `1690.2421648709 MON`
- Turnover: `46.02937790193998 USDC`
- Final cash: `977.2556255519878 USDC`
- Final inventory: `1838.2742947639 MON`
- Final portfolio value: `1028.0260041619247 USDC`
- Net P&L: `+0.9145041619246229 USDC`
- Maximum drawdown: `0.4194581017089 USDC`
- Maker fees: `0 USDC`
- Action fees: `0 USDC`
- Placements: `2012`
- Cancellations: `2004`
- Strategy timeline records: `105527`

## Counterfactual impact

The run is flagged for material counterfactual impact.

- Transactions with simulated fills: `15`
- Material-impact transactions: `4`
- Maximum transaction-level simulated share: `100%`
- Material threshold: `25%`

The historical taker flow is fixed. A simulated order does not change later historical behavior. Results from material-impact transactions need careful interpretation.

## Deterministic artifacts

Validation outputs are in `validation_output/relatively_active/`.

Strategy outputs are in `strategy_output/relatively_active/`.

Strategy hashes were equal on two consecutive runs:

```text
summary_sha256=59889f9072cd8829601a08bd9255ee96690edcaed856b014acc0c835bb6df108
timeline_sha256=9e15e542411e40e1d8433af7d7fa830a3b368bf7a7922be42a36d9c30346931d
```

## Test result

```text
Ran 84 tests in 0.055s
OK
```
