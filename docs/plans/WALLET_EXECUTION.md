# Wallet and Kuru LP execution plan

Date: 11 October 2026. Status: proposed only. No wallet connection, approval,
deposit, order, cancellation, or withdrawal was sent.

## Boundary

Keep historical simulation and live execution separate. A backtest button must
never request a wallet signature. Add a separate future “Create live position”
flow with a new review screen. The user's wallet signs each transaction. The
backend must not hold keys or seed phrases.

Kuru flip orders operate on-chain after placement. Do not run the historical
simulation loop to issue live replacement orders. Kuru describes automatic
opposite-side flips in its [LP guide](https://docs.kuru.io/liquidity/how-liquidity-provisioning-works).

## Confirmed source baseline

The installed SDK is `@kuru-labs/kuru-sdk@0.0.95`, npm git head
`c6a1e9eced12d634fa879ec46c52baff8a50f3c3`.

Inspected installed source:

- `src/concentratedLiquidity/provision.ts`: `PositionProvider` constructs
  `batchProvisionLiquidity(prices, flipPrices, sizes, isBuy, false)`. It adds
  bids first, then asks. The last argument is `provisionOrRevert`.
- `src/concentratedLiquidity/withdraw.ts`: `PositionWithdrawer` constructs
  `batchCancelFlipOrders(orderIds)`. This is order cancellation, not proof that
  assets have been transferred back to the wallet.

These methods are a starting point, not evidence that the current deployed
implementation matches the historical test bytecode. Pin and verify the live
implementation, ABI, market parameters, tokens, and SDK before enabling writes.
Use [Kuru's address directory](https://docs.kuru.io/contracts/Contract-addresses)
and [Monad network information](https://docs.monad.xyz/developer-essentials/network-information),
then check on-chain values. Mainnet and Testnet have different market addresses.

## Stages

### 1. Read-only wallet support

Use [EIP-6963 discovery](https://eips.ethereum.org/EIPS/eip-6963) and an
[EIP-1193 provider](https://eips.ethereum.org/EIPS/eip-1193). Request accounts
only after a user click. Handle rejected requests, account changes, chain
changes, and disconnects. Clear unsigned transaction previews on each change.
Do not trust wallet names or icons as HTML.

Show wallet balances, gas balance, existing margin balances, and owned live
orders. Verify chain ID through the provider. Disable writes on a wrong or
unsupported chain. Historical backtests remain available without a wallet.

### 2. Live position preview

Read one recent chain snapshot. Use its market parameters and book state with
the canonical SDK adapter. Do not reuse historical prices, historical token
addresses, or cached historical funding rules without live parity checks.

Display absolute bounds, exact grid, flip prices, required amounts, current
balances, gas estimate, target contracts, and possible skipped orders. Give the
user a choice of absolute bounds or a fresh live-relative range; never move
historical bounds silently. Keep MON gas separate from MON liquidity.

Hash the reviewed chain/account/configuration/grid into an unsigned intent.
Apply a short validity window and recheck before signing. If parameters, code,
account, or required grid changes, show a new preview and require approval.
The existing SDK uses `provisionOrRevert=false`; explain that crossed orders can
be skipped. Do not claim the requested order count will always be placed.

### 3. Funding and provision

Verify the actual market token type first. MON in the UI must not be assumed
to mean native MON or WMON without an on-chain check. Kuru's
[MarginAccount documentation](https://docs.kuru.io/contracts/MarginAccount)
defines separate native-value and ERC-20 deposit paths and separate withdrawal.

Calculate the margin shortfall after existing free balance. For ERC-20 assets,
request only the required allowance to the verified spender. Explain each
approval and deposit. For native assets, set the verified transaction value and
retain gas funds. Do not treat gas-estimation state overrides as real deposits.

Construct calldata through the pinned SDK. Decode it back and compare all
prices, flip prices, sizes, sides, recipient, chain, and value with the reviewed
intent. Simulate the real funded call and estimate gas before wallet submission.
Do not add a custom contract or assume deposits and provision are atomic.
If multiple transactions are required, show their separate failure states.

### 4. Receipts and position management

Track intent, chain, account, nonce, transaction hash, replacement hash, and
receipt. A submitted transaction is not a completed position. Handle rejection,
revert, dropped transactions, repricing, restart, and chain reorganization.
Use chain-confirmed receipts and authoritative reads to determine placed orders,
skipped orders, linked flips, remaining balances, and fees.

Envio can update the position view; show indexer delay and confirm recent writes
with RPC. Never resubmit a provision call automatically because a receipt lookup
timed out. Match the existing nonce/hash first.

Allow the user to cancel their exact flip-order IDs with the SDK method. Re-read
live state because fills can occur before cancellation is included. Reconcile
both paired orders and free margin. Make withdrawal to the wallet a separate,
explicit action. Never interpret order cancellation alone as wallet withdrawal.

## Required tests before Mainnet writes

1. SDK calldata fixtures for Spot, Curve, and Bid-Ask, with exact raw amounts.
2. A local fork with the intended deployed implementation: allowance, native
   and ERC-20 deposit, provision, partial fills, paired flips, cancellation, and
   withdrawal. Include skipped orders and the largest allowed batch.
3. Rejected signatures, changed wallet/chain, stale preview, insufficient gas,
   insufficient margin, a failed second transaction, duplicate clicks, nonce
   replacement, missing receipt, and a replaced chain block.
4. Testnet end-to-end checks against its separately verified contracts. Testnet
   success does not prove Mainnet implementation parity.
5. An independent security review, then a separately authorized small Mainnet
   test with a stated spending limit and tested exit path. This plan does not
   authorize that test or the use of any funds.

Do not add unattended trading, automatic token swaps, custody, or delegated
signing in this first execution stage. Backtest parity is useful evidence, but
it cannot prove future queue position, taker behavior, gas cost, or live profit.
