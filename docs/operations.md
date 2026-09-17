# Running and stopping Stonkfly

Use a dedicated PPI account. Stonkfly is an experiment capable of losing its entire allocated balance. The funding cap is **ARS 100,000 at initialization**, an arbitrary starting point for the experiment, not a claim about what that buys or how it holds value over time. The account must start ARS-only: any non-ARS balance, and any existing instrument position, blocks initialization — except USD residue up to 1.00, tolerated as unconvertible funding dust (e.g. leftover cents from a USD→ARS conversion) rather than a real position to reconcile against. The same dust tolerance applies to `verify_balances()` on every tick, not just at initialization.

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

Execution goes through PPI's official `ppi-client` Python SDK (PyPI: `ppi-client`), not a hand-rolled HTTP client — it's the vendor's own maintained library, and it's authoritative on base URLs and request details that PPI's public docs leave ambiguous or get subtly wrong. `AuthorizedClient`/`ClientKey` are internal constants the SDK supplies itself for both sandbox and production; you never need or set them.

1. Get your PPI API key/secret (`Gestiones -> Gestión de servicio API` in your account for production; sandbox testing credentials arrive by email) and your account number.
2. Copy `.env.example` to `.env` and fill in `PPI_API_KEY`, `PPI_API_SECRET` (`PPI_ENV` defaults to `sandbox`; set it to `production` for a real account). This alone is enough for `--real-data` paper mode.
3. For `--live`, additionally set `PPI_ACCOUNT_NUMBER` and `STONKFLY_LIVE=I_ACCEPT_REAL_TRADES`. The CLI also requires `--live`; paper mode never submits an order even if the environment variable is present.
4. Run `python -m stonkfly run --live --preflight-only`. This reads balances/positions and open orders, and initializes the local ledger. **It does not submit orders.** Once you have reviewed the configuration, run `python -m stonkfly run --live` yourself.

Defaults trade `GGAL`; `YPFD`, `PAMP`, `AAPL` and `KO` (CEDEARs and acciones líderes) are optional via `--products`. Only tickers in the current allowlist (`python -m stonkfly instruments`) are accepted; run `python -m stonkfly instruments --refresh` to fetch every BYMA ACCIONES listing via PPI SearchInstrument and pass `--products ALL` to trade all of them.

Trading the full BYMA panel means the Guard needs a fresh quote for every one of those tickers on every tick (it marks the whole portfolio to market for the loss-stop check), so `--live`/`--real-data` issue one PPI request per instrument per tick — with hundreds of tickers this can make each tick slow and risks tripping PPI's rate limits. `--products ALL` is safe and fast in paper/fixture mode (synthetic prices, no network calls); for `--live` or `--real-data`, prefer a curated subset unless you've confirmed PPI tolerates the request volume.

### Known gaps before ever using `--live`

Even with the official SDK doing the request plumbing, PPI's public docs are considerably thinner than Coinbase's on some details this program depends on for correctness. Concretely, unverified against a real account:

- **No documented order status enum.** A terminal order is inferred from its absence in `Order/ActiveOrders`, not from a status string.
- **No documented fill-vs-requested-quantity split.** `Order/Detail`'s `quantity`/`amount` fields are assumed to reflect the actual fill; a genuine partial fill cannot currently be distinguished from a full fill.
- **No documented commission field.** Fills are booked with zero fee. A real PPI account will charge a commission this ledger will not see, and `verify_balances()` is expected to trip on the resulting mismatch — that is the intended fail-safe, not a bug, but it means `--live` is not yet usable end-to-end.
- **No documented immediate-or-cancel/FOK order term.** A day limit order (`PRECIO-LIMITE` / `POR-EL-DÍA`) is submitted and force-cancelled at a 30-second deadline if still resting, to keep the same bounded-risk shape the cooldown/veto model assumes.
- **`BalancesAndPositions`' actual shape** (`groupedAvailability` for cash, `groupedInstruments` for holdings) came from the vendor SDK's own example code, not PPI's REST docs page — that page shows a flat array that doesn't match what the API actually returns.

`Order/Confirm` also requires each disclaimer `Order/Budget` returns to be individually accepted (`{"code", "accepted": true}`); `execute()` does this automatically by accepting every disclaimer Budget returns, without showing you their text first — that's an experiment-automation tradeoff, not something PPI lets you skip.

Do not use `--live` until the still-unverified items above are checked against a real account, ideally cross-referenced with `Account/Movements`.

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

On a halt, the console prints a redacted stop reason by default; `--debug` prints the real exception and traceback to stderr instead, which can include account/request details — only use it in a terminal you trust, and never paste that output elsewhere.

For an ordinary clean restart, use the same command and run directory. After reviewing a transient failure and reconciling account state, remove the STOP file if appropriate and pass `--resume-reviewed`. This cannot clear a drawdown or fee-overrun stop, bypass unresolved exchange outcomes, or accept changed source/configuration. A missing unknown order requires manual exchange investigation; do not assume it failed. Source changes require an explicitly reviewed state migration; use a fresh **paper** directory for development.

All runtime state, balances, account IDs, data, `.env` and the default key filenames are git-ignored. Keep custom credentials outside the repository. Tests use doubles and never submit real orders. No live account credentials or real balances are bundled.
