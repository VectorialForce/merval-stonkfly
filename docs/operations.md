# Running and stopping Stonkfly

Use a dedicated PPI account. Stonkfly is an experiment capable of losing its entire allocated balance. The funding cap is **ARS 100,000 at initialization**, an arbitrary starting point for the experiment, not a claim about what that buys or how it holds value over time.

## Installation and data

Use Python 3.11 and a C++17 compiler (`clang++`/`c++` on macOS, GCC or Clang on Linux). `python -m stonkfly prepare` downloads about 1.1 GB of upstream data, verifies it, and builds the full graph. Allow several additional GB for dependencies, derived data and two checkpoints. `python -m stonkfly verify` independently checks prepared inputs. Set `STONKFLY_DATA` to use another data location.

Existing DOOMFLY researchers can reuse verified local files with `python -m stonkfly prepare --reuse-doomfly /path/to/working-copy`. Stonkfly copies only the three required data artifacts, then checks the same locks. It does not import a Doom environment, run its website, or depend on that checkout afterward.

## Paper modes

```sh
# Synthetic offline market, no credentials needed.
python -m stonkfly run --fixture --fast --steps 10 --out runs/fixture

# Real PPI quotes; simulated fills only, never places an order. Requires .env.
python -m stonkfly run --real-data --steps 10

# Frozen-memory control, always in a separate run directory.
python -m stonkfly run --fixture --fast --frozen --steps 10 --out runs/frozen
```

`--fast` skips wall waits only in paper mode. It preserves the 0.1 ms neural timestep and the real 60-second execution cooldown, so an accelerated probe can have many rejected trades. This is a plumbing/neural test, not a backtest of achievable market returns. Paper fills use observed bid/ask plus the configured fee; they do not simulate depth, queue position or all market impact. `--fixture` never claims real market data. Plain `run` with no flags behaves like `--fixture`: PPI requires authenticated headers on every endpoint, including market data, so there is no unauthenticated "real prices, no key" default the way Coinbase's public order book allowed.

## PPI setup, performed by you

1. Get PPI API credentials (sandbox or production): `AuthorizedClient`, `ClientKey`, `ApiKey`, `ApiSecret`, your account number, and the base server URL — PPI's docs don't publish a fixed hostname, so get it from your dashboard/sandbox invitation alongside the credentials.
2. Copy `.env.example` to `.env` and fill in `PPI_BASE_URL`, `PPI_AUTHORIZED_CLIENT`, `PPI_CLIENT_KEY`, `PPI_API_KEY`, `PPI_API_SECRET`. This alone is enough for `--real-data` paper mode.
3. For `--live`, additionally set `PPI_ACCOUNT_NUMBER` and `STONKFLY_LIVE=I_ACCEPT_REAL_TRADES`. The CLI also requires `--live`; paper mode never submits an order even if the environment variable is present.
4. Run `python -m stonkfly run --live --preflight-only`. This reads balances/positions and open orders, and initializes the local ledger. **It does not submit orders.** Once you have reviewed the configuration, run `python -m stonkfly run --live` yourself.

Defaults trade `GGAL`; `YPF`, `PAMP`, `AAPL` and `KO` (CEDEARs and acciones líderes) are optional via `--products`. Only this closed allowlist is accepted.

### Known gaps before ever using `--live`

PPI's public API docs are considerably thinner than Coinbase's on exactly the details this program depends on for correctness. Concretely, unverified against a real sandbox account:

- **No documented order status enum.** A terminal order is inferred from its absence in `Order/ActiveOrders`, not from a status string.
- **No documented fill-vs-requested-quantity split.** `Order/Detail`'s `quantity`/`amount` fields are assumed to reflect the actual fill; a genuine partial fill cannot currently be distinguished from a full fill.
- **No documented commission field.** Fills are booked with zero fee. A real PPI account will charge a commission this ledger will not see, and `verify_balances()` is expected to trip on the resulting mismatch — that is the intended fail-safe, not a bug, but it means `--live` is not yet usable end-to-end.
- **No documented immediate-or-cancel/FOK order term.** A day limit order (`POR-EL-DÍA`) is submitted and force-cancelled at a 30-second deadline if still resting, to keep the same bounded-risk shape the cooldown/veto model assumes.
- **`BalancesAndPositions`' schema is inferred**, not documented, to mix cash and security holdings into one array by `simbol`.

Do not use `--live` until these are checked against real sandbox responses, ideally cross-referenced with `Account/Movements`.

## Execution guarantees and limits

- Maximum initial funding: ARS 100,000. Maximum buy commitment: ARS 10,000 including a 2% fee reserve. Sell quantity is capped by owned inventory and ARS 10,000 observed notional; a better execution price can yield slightly more proceeds. No borrowing, shorting, transfers or leverage actions are exposed. Instruments trade in whole shares only.
- At most 24 order attempts per UTC day and at least 60 seconds between attempts. Rejected previews count. Failed orders do not become new strategy choices.
- Price-bounded day limit orders use at most 0.5% slippage and 0.5% spread. Quotes must be no older than 15 seconds — outside BYMA session hours this staleness check is what naturally stops new trades (PPI's docs don't publish session hours, so there is no separate "market closed" schedule; a stale quote is treated the same whether the cause is an overnight gap or a genuine feed problem). A fresh book is fetched after neural integration; a move beyond the observation tolerance vetoes the trade. `Order/Budget` and account balances are checked before submission.
- At ARS 20,000 drawdown from starting equity, **stop new orders**. This is not a liquidation order or guaranteed maximum loss. Existing holdings remain exposed; price moves between observations can exceed the threshold. Decide separately how you want to manage those holdings.
- SQLite records a unique client order ID (sent to PPI as `externalID`) before submission. An uncertain response stays unresolved; the worker searches PPI for that same `externalID` instead of sending a new order. Missing/ambiguous results stop the worker for manual review. Final fills settle exactly once. Unexpected actual fees are booked, then further orders halt.
- The local process lock prevents two workers using one run directory. It does not coordinate multiple computers or copied ledgers. Run one worker for the dedicated account, and do not delete the live ledger to bypass checks.

## State, recovery and privacy

The worker must stay running on your computer/server. It is not a hosted service. Prevent laptop sleep if you want uninterrupted observations. Closing it preserves committed neural state and memory.

`runs/<name>/` holds a SQLite ledger, two alternating checkpoints, `events.jsonl`, `latest.json`, `latest-input.png`, and provenance with exact code, graph, stimulus and parameter hashes. Each intent binds to the preceding neural observation and checkpoint. The ledger is authoritative if a crash occurs before the human-readable log is written.

To stop: Ctrl-C, or `touch runs/live/STOP` (`runs/paper/STOP` for paper). This stops future decisions/submissions; an already submitted order may still finish or still be resting. Inspect any uncertain order in PPI before taking another action.

For an ordinary clean restart, use the same command and run directory. After reviewing a transient failure and reconciling account state, remove the STOP file if appropriate and pass `--resume-reviewed`. This cannot clear a drawdown or fee-overrun stop, bypass unresolved exchange outcomes, or accept changed source/configuration. A missing unknown order requires manual exchange investigation; do not assume it failed. Source changes require an explicitly reviewed state migration; use a fresh **paper** directory for development.

All runtime state, balances, account IDs, data, `.env` and the default key filenames are git-ignored. Keep custom credentials outside the repository. Tests use doubles and never submit real orders. No live account credentials or real balances are bundled.
