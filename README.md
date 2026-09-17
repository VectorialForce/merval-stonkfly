# Stonkfly

A fly-connectome simulation that can operate a MERVAL trading account. Actual neural output, actual PPI (Portfolio Personal Inversiones) integration. Profitable learning has not been demonstrated.

**How it works:** BYMA prices become an RGB chart. It stimulates 3,335 brightness inputs and 811 R8 color inputs in the retained **MaleCNS v1.0 graph: 166,700 neurons, 25.6 million connections**. A fixed neural readout proposes buy, sell or hold. A guard checks limits and places orders through PPI's REST API.

Positive portfolio P&L stimulates 15 identified PAM11 dopamine cells; negative P&L stimulates two PPL101 aversive dopamine cells. A candidate memory rule changes existing KC-to-MBON connections. These are engineered reinforcement signals, **not modeled pain receptors**. Synaptic changes do not establish that it learns to trade profitably. [Model and evidence](docs/model.md).

## Run it

Python 3.11, a C++17 compiler, macOS/Linux. Allow several GB for the dataset and dependencies; 16 GB RAM recommended.

```sh
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e '.[test]'
python -m stonkfly prepare
python -m stonkfly run
```

Default: **paper trades, synthetic offline prices, ARS 100,000 simulated balance**. No key needed. Unlike Coinbase's public crypto book, PPI requires authenticated headers on every endpoint including market data, so there is no unauthenticated "real prices, no key" mode — the zero-setup default trades a synthetic fixture instead. Local logs, sensory images and resumable brain state go in `runs/paper/`. Ctrl-C stops it; the same command resumes.

To paper-trade against real PPI market quotes (still simulated fills, no orders ever sent), get sandbox or production API credentials from PPI, copy `.env.example` to `.env` and fill in the `PPI_*` variables, then:

```sh
python -m stonkfly run --real-data
```

For real orders, get a PPI account and API key, fill in `.env` completely (including `PPI_ACCOUNT_NUMBER`), then run these commands yourself:

```sh
python -m stonkfly run --live --preflight-only
python -m stonkfly run --live
```

Defaults: ARS 10,000 maximum order including reserved fees, 24 attempts/day, no shorts or leverage. An ARS 20,000 drawdown stops new orders; **it does not liquidate holdings or cap further losses**. [Operation and recovery](docs/operations.md).

```sh
python -m stonkfly status
python -m pytest -q
```

The repo does not come funded or connected to anyone's account. Live execution needs your local credentials and explicit opt-in, and — because PPI's public docs leave several exchange behaviors undocumented (order status values, fill accounting, exact tick sizes) — is unverified against a real account. Read [operations](docs/operations.md) before ever using `--live`.
