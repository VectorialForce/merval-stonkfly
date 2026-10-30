import hashlib
import json
import math
import os
import sys
from dataclasses import asdict, dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from pathlib import Path


@dataclass(frozen=True)
class Instrument:
    ticker: str
    instrument_type: str  # "ACCIONES" | "CEDEARS"
    settlement: str = "INMEDIATA"
    market: str = "BYMA"


# Hard cap on what the bot may ever trade: the S&P MERVAL panel (acciones
# lideres). Everything else -- the built-in default, the refreshed cache and
# `--products ALL` -- is filtered down to this set, so a fresh
# `instruments --refresh` can never widen it. Settlement/tick-size conventions
# are not published by PPI's docs; INMEDIATA (CI) is the simplest, most liquid
# default and should be reconciled against Configuration/Settlements and
# SearchInstrument before any live use.
MERVAL_PANEL = (
    "ALUA", "BBAR", "BMA", "BYMA", "CEPU", "COME", "CRES", "ECOG", "EDN", "GGAL",
    "LOMA", "METR", "PAMP", "SUPV", "TGNO4", "TGSU2", "TRAN", "TXAR", "VALO",
    "YPFD",  # YPF's peso-settlement ticker; PPI has no bare "YPF"
)
_DEFAULT_INSTRUMENTS = [Instrument(t, "ACCIONES") for t in MERVAL_PANEL]

# `stonkfly instruments --refresh` fetches BYMA ACCIONES listings via PPI's
# SearchInstrument endpoint and caches the MERVAL_PANEL tickers with a live
# market here, replacing the built-in default above. Kept outside the package
# (alongside the other fetched/verified datasets in data/) rather than in the
# neural dataset's own sources.lock.json family, since this is a live listing
# that changes over time, not a hash-verified fixed dataset.
_CACHE_PATH = Path(__file__).resolve().parent.parent / "data" / "instruments.json"


def _load_instruments():
    if _CACHE_PATH.exists():
        rows = json.loads(_CACHE_PATH.read_text())
        instruments = [
            Instrument(**row) for row in rows if row["ticker"] in MERVAL_PANEL
        ]
        if not instruments:
            raise RuntimeError(
                f"{_CACHE_PATH} has no MERVAL panel tickers; delete it or refresh it"
            )
        return instruments
    return list(_DEFAULT_INSTRUMENTS)


INSTRUMENTS = {i.ticker: i for i in _load_instruments()}


_SWEEP_TERMS = "abcdefghijklmnopqrstuvwxyz0123456789"


def _has_live_market(client, instrument):
    try:
        book = client.book(instrument.ticker, instrument.instrument_type, instrument.settlement)
    except Exception:
        book = {}
    if (book.get("bids") or []) and (book.get("offers") or []):
        return True
    try:
        current = client.current(
            instrument.ticker, instrument.instrument_type, instrument.settlement
        )
    except Exception:
        current = {}
    return bool(current.get("price") and current.get("date"))


def refresh_instruments(env=None):
    """Fetch the MERVAL panel's BYMA ACCIONES listings via PPI SearchInstrument and cache them.

    SearchInstrument is a substring search, not a listing endpoint: PPI
    rejects a blank ticker/name ("Field 'Ticker' is required"), so there is
    no single call that returns everything. Every BYMA ticker/description
    contains at least one letter or digit, so sweeping single-character
    queries across the alphabet and digits and taking the union of results
    (by ticker) recovers the full panel. Requires PPI credentials; not used
    by paper/fixture runs.
    """
    from .ppi import PPIClient

    client = PPIClient.from_env(env if env is not None else os.environ)
    seen = {}
    for term in _SWEEP_TERMS:
        for row in client.search_instrument(term, term, "BYMA", "ACCIONES"):
            ticker = row.get("ticker")
            if not ticker:
                raise RuntimeError(f"SearchInstrument row has no ticker field: {row}")
            if row.get("type") != "ACCIONES" or row.get("market") != "BYMA":
                continue
            if ticker not in MERVAL_PANEL:
                continue
            seen[ticker] = Instrument(ticker, "ACCIONES", market="BYMA")
    if not seen:
        raise RuntimeError("PPI SearchInstrument returned no MERVAL panel instruments")
    ordered = sorted(seen.values(), key=lambda i: i.ticker)
    # SearchInstrument lists every ticker BYMA ever registered, including
    # ones with no live market at all (suspended/delisted names, confirmed
    # against a real one: no book AND no current price, ever) — PPIMarket
    # requires every configured product to have a fresh quote every tick, so
    # a single such ticker would make the whole worker skip forever. Probe
    # each ticker once here rather than discovering this live.
    live = []
    for n, i in enumerate(ordered, 1):
        print(f"\rprobing {n}/{len(ordered)}: {i.ticker}    ", end="", file=sys.stderr)
        if _has_live_market(client, i):
            live.append(i)
    print(file=sys.stderr)
    if not live:
        raise RuntimeError("None of the fetched BYMA instruments have live market data")
    ordered = live
    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CACHE_PATH.write_text(json.dumps([asdict(i) for i in ordered], indent=2) + "\n")
    global INSTRUMENTS
    INSTRUMENTS = {i.ticker: i for i in ordered}
    return len(INSTRUMENTS)


def D(value):
    if isinstance(value, bool):
        raise ValueError("Boolean is not money")
    x = Decimal(str(value))
    if not x.is_finite():
        raise ValueError("Nonfinite quantity")
    return x


def down(value, step):
    return (D(value) / D(step)).to_integral_value(rounding=ROUND_DOWN) * D(step)


def up(value, step):
    return (D(value) / D(step)).to_integral_value(rounding=ROUND_UP) * D(step)


@dataclass(frozen=True)
class Settings:
    products: tuple[str, ...] = ("GGAL",)
    capital: str = "100000"
    order_limit: str = "10000"
    loss_stop: str = "20000"
    fee_reserve: str = "0.02"
    slippage: str = "0.005"
    spread_limit: str = "0.005"
    daily_orders: int = 24
    interval_seconds: float = 60
    max_quote_age: float = 30
    neural_ms: float = 500
    neural_bin_ms: float = 10
    pulse_ms: float = 200
    pulse_current: float = 20
    reward_deadband: str = "10"
    decoder_threshold_hz: float = 2
    paper_fee: str = "0.006"
    learning: bool = True
    sensory_noise_std: float = 0.0

    def __post_init__(self):
        if (
            not self.products
            or len(set(self.products)) != len(self.products)
            or not set(self.products) <= set(INSTRUMENTS)
        ):
            raise ValueError("Only allowlisted MERVAL instruments")
        if not 0 < D(self.capital) <= 100000 or not 0 < D(self.order_limit) <= min(
            D(self.capital), D(10000)
        ):
            raise ValueError("Maximum capital ARS 100,000; maximum order ARS 10,000")
        if not 0 < D(self.loss_stop) <= D(self.capital):
            raise ValueError("Invalid loss stop")
        if (
            not D(0) < D(self.fee_reserve) <= D(".05")
            or not 0 <= D(self.slippage) <= D(".01")
            or not 0 < D(self.spread_limit) <= D(".01")
        ):
            raise ValueError("Invalid fee/spread/slippage bounds")
        if not 0 <= D(self.paper_fee) <= D(self.fee_reserve):
            raise ValueError("Invalid paper fee")
        if (
            type(self.daily_orders) is not int
            or not 1 <= self.daily_orders <= 100
            or not math.isfinite(self.interval_seconds)
            or self.interval_seconds < 60
        ):
            raise ValueError("Rate limit: >=60 s between orders, <=100 orders/day")
        if D(self.reward_deadband) <= 0:
            raise ValueError("Positive reinforcement deadband required")
        for x in [
            self.max_quote_age,
            self.neural_ms,
            self.neural_bin_ms,
            self.pulse_ms,
            self.pulse_current,
            self.decoder_threshold_hz,
        ]:
            if not math.isfinite(x) or x <= 0:
                raise ValueError("Positive finite parameter required")
        if not math.isfinite(self.sensory_noise_std) or not 0 <= self.sensory_noise_std <= 1:
            raise ValueError("sensory_noise_std must be in [0, 1] (luminance units)")
        if self.neural_bin_ms > 10 or self.pulse_ms > self.neural_ms:
            raise ValueError(
                "Use <=10 ms neural bins; pulse must fit a decision window"
            )
        if any(
            abs(x * 10 - round(x * 10)) > 1e-7
            for x in [self.neural_ms, self.neural_bin_ms, self.pulse_ms]
        ):
            raise ValueError("Neural intervals must be multiples of 0.1 ms")

    def instrument(self, ticker):
        return INSTRUMENTS[ticker]

    def signature(self):
        return hashlib.sha256(
            json.dumps(asdict(self), sort_keys=True).encode()
        ).hexdigest()
