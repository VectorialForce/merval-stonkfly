"""PPI (BYMA) observations. Synthetic fixtures are explicit test inputs."""

import hashlib
import math
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from .config import D


def _synthetic_base_price(ticker):
    """Deterministic per-ticker base price, so the fixture works for any
    instrument (including a large fetched allowlist) without a hand-maintained
    price table."""
    seed = int(hashlib.sha256(ticker.encode()).hexdigest(), 16)
    return 500 + (seed % 49500)


def _priced_levels(levels):
    """Some real book levels carry a price of 0 — confirmed against several
    real BYMA tickers (e.g. a resting level with no limit price) — which
    isn't a valid tradeable price and corrupts both "best bid/ask" (a 0 ask
    looks like the book is giving shares away) and the tick-size inference
    below. Filter those out; "best" is still whichever positive-priced level
    sorts first, since the book's own ordering already reflects priority."""
    return [D(level["price"]) for level in levels if D(level["price"]) > 0]


def _infer_price_increment(book):
    """BYMA's real tick size isn't exposed anywhere in PPI's API responses
    (SearchInstrument, Configuration/*, Book, Current all lack it), and it is
    coarser than the D("0.01") this code used to hardcode: a real GGAL book
    only ever showed prices on a 5-peso grid (6700, 6705, 6710, ...), so a
    guard-computed limit price like 6743.55 was never actually tradeable and
    every live order was cancelled unfilled regardless of how long it rested.

    Infer the increment from the book's own price levels instead of guessing
    a constant: the GCD of the differences between observed price levels is
    guaranteed to be an exact multiple of the true tick size (never smaller),
    so rounding a limit price to a multiple of this GCD always lands on a
    valid, tradeable price even if sparse data makes the GCD coarser than the
    real tick. Returns None if the book has too few distinct levels to infer
    anything (e.g. a single level per side).
    """
    levels = sorted(
        {p for side in ("bids", "offers") for p in _priced_levels(book.get(side) or [])}
    )
    diffs = [b - a for a, b in zip(levels, levels[1:])]
    if not diffs:
        return None
    increment = diffs[0]
    for d in diffs[1:]:
        a, b = increment, d
        while b:
            a, b = b, a % b
        increment = a
    return increment


@dataclass(frozen=True)
class Quote:
    product: str
    bid: Decimal
    ask: Decimal
    timestamp: float
    price_increment: Decimal
    minimum_quote: Decimal
    lot_size: Decimal

    def __post_init__(self):
        if (
            not self.bid.is_finite()
            or not self.ask.is_finite()
            or not 0 < self.bid <= self.ask
            or not math.isfinite(self.timestamp)
        ):
            raise ValueError("Invalid quote")
        if not self.price_increment.is_finite() or self.price_increment <= 0:
            raise ValueError("Invalid market increment")
        if not self.minimum_quote.is_finite() or self.minimum_quote < 0:
            raise ValueError("Invalid minimum notional")
        if (
            not self.lot_size.is_finite()
            or self.lot_size <= 0
            or self.lot_size != self.lot_size.to_integral_value()
        ):
            raise ValueError("Invalid lot size; MERVAL instruments trade in whole shares")

    def json(self):
        return {
            k: str(v) if isinstance(v, Decimal) else v for k, v in asdict(self).items()
        }


def utc_timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Exchange timestamp requires a timezone")
    return parsed.timestamp()


class PPIMarket:
    """PPI requires authenticated headers on every endpoint, including market
    data; there is no unauthenticated real-data mode as there was for
    Coinbase's public order book."""

    def __init__(self, instruments, client):
        self.client = client
        self.instruments = instruments
        self._by_ticker = {i.ticker: i for i in instruments}
        self.history = {i.ticker: [] for i in instruments}
        self.price_increment = {}

    def snapshot(self, tickers):
        # Only fetch what's actually needed this tick (the product being
        # decided, plus any currently held position) rather than every
        # configured product: with a large, uneven universe (--products
        # ALL), requiring a fresh valid quote for all of them every tick
        # means any single flaky ticker (confirmed: missing trade bars,
        # degenerate zero-priced book levels, outright malformed API
        # responses — all seen on real BYMA data) halts the whole worker,
        # and it multiplies PPI round-trip latency by however many products
        # are configured.
        result = {}
        now = time.time()
        for ticker in tickers:
            instrument = self._by_ticker[ticker]
            book = self.client.book(ticker, instrument.instrument_type, instrument.settlement)
            bid_prices = _priced_levels(book.get("bids") or [])
            ask_prices = _priced_levels(book.get("offers") or [])
            if bid_prices and ask_prices and bid_prices[0] <= ask_prices[0]:
                bid, ask = bid_prices[0], ask_prices[0]
                stamp = utc_timestamp(book["date"]) if book.get("date") else now
                # Fall back to the last inferred increment, then a bare guess,
                # only when this book has too few distinct levels to infer
                # from (e.g. a single level per side).
                increment = (
                    _infer_price_increment(book)
                    or self.price_increment.get(ticker)
                    or D("0.01")
                )
                self.price_increment[ticker] = increment
            else:
                # No usable book: empty (expected outside BYMA session
                # hours), or every level's price was 0/crossed (confirmed on
                # several real thinly traded tickers). Fall back to last
                # price with zero synthetic spread; Current's own timestamp
                # is what lets the existing staleness veto in Guard.check
                # keep the worker from trading a closed or dead market,
                # rather than inventing a separate session-hours system.
                current = self.client.current(
                    ticker, instrument.instrument_type, instrument.settlement
                )
                price = D(current["price"]) if current.get("price") else D(0)
                if not (price.is_finite() and price > 0) or not current.get("date"):
                    raise RuntimeError("No book or current price available")
                bid = ask = price
                stamp = utc_timestamp(current["date"])
                # No book levels to infer from right now; reuse whatever this
                # ticker's increment last looked like, since a closed/empty
                # book gives no better information.
                increment = self.price_increment.get(ticker, D("0.01"))
            if not self.history[ticker]:
                # The vendor SDK formats dateFrom/dateTo with plain str() of
                # whatever it's given, then never URL-encodes the result. A
                # tz-aware datetime's str() ends in "+00:00", and PPI decodes
                # a literal "+" in the query string as a space (the standard
                # application/x-www-form-urlencoded convention), so the
                # offset silently mangles into "2026-09-18 14:03:49 00:00"
                # server-side and PPI rejects it as an invalid date. Pass
                # naive UTC datetimes (same instant, no "+00:00" to mangle)
                # instead of timezone-aware ones.
                end = datetime.now(timezone.utc).replace(tzinfo=None)
                start = end - timedelta(hours=2)
                bars = self.client.search(
                    ticker,
                    instrument.instrument_type,
                    instrument.settlement,
                    start,
                    end,
                )
                past = sorted(
                    (b for b in bars if utc_timestamp(b["date"]) < now),
                    key=lambda b: utc_timestamp(b["date"]),
                )
                if past:
                    self.history[ticker] = [float(D(b["price"])) for b in past]
                    if any(not math.isfinite(v) or v <= 0 for v in self.history[ticker]):
                        raise RuntimeError("Invalid historical price")
                else:
                    # No trade in the last 2 hours: expected for a thinly
                    # traded Panel General name, not just a closed market —
                    # this is not the rare exception FixtureMarket's history
                    # or a liquid blue-chip like GGAL was built assuming.
                    # Bootstrap from the current quote instead of hard
                    # failing, since that would otherwise crash the worker
                    # on the very first tick that ever touches this ticker,
                    # forever (history stays empty, so it never stops
                    # recurring on retry).
                    self.history[ticker] = [float((bid + ask) / 2)]
            quote = Quote(
                ticker,
                bid,
                ask,
                stamp,
                increment,
                D("0"),
                D("1"),
            )
            result[ticker] = quote
        return result

    def record(self, quotes):
        for p, q in quotes.items():
            self.history[p].append(float((q.bid + q.ask) / 2))
            self.history[p] = self.history[p][-120:]


class FixtureMarket:
    """Deterministic prices for offline verification, never used in live mode."""

    def __init__(self, instruments):
        self.instruments = instruments
        self._index = {i.ticker: idx for idx, i in enumerate(instruments)}
        self.tick = 0
        self.history = {i.ticker: [] for i in instruments}

    def snapshot(self, tickers):
        quotes = {}
        for ticker in tickers:
            j = self._index[ticker]
            base = _synthetic_base_price(ticker)
            price = D(base) * D(1 + 0.025 * math.sin(self.tick * 0.6 + j))
            quotes[ticker] = Quote(
                ticker,
                price * D(".9995"),
                price * D("1.0005"),
                time.time(),
                D(".01"),
                D("0"),
                D("1"),
            )
            if not self.history[ticker]:
                self.history[ticker] = [
                    float(D(base) * D(1 + 0.01 * math.sin(i * 0.4 + j)))
                    for i in range(80)
                ]
        self.tick += 1
        return quotes

    record = PPIMarket.record
