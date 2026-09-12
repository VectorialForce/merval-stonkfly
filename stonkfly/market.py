"""PPI (BYMA) observations. Synthetic fixtures are explicit test inputs."""

import math
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from .config import D


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
        self.history = {i.ticker: [] for i in instruments}

    def snapshot(self):
        result = {}
        now = time.time()
        for instrument in self.instruments:
            ticker = instrument.ticker
            if not self.history[ticker]:
                end = datetime.now(timezone.utc)
                start = end - timedelta(hours=2)
                # The vendor SDK formats dateFrom/dateTo with plain str() of
                # whatever it's given; pass datetime objects, not strings.
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
                if not past:
                    raise RuntimeError("No historical bars available")
                self.history[ticker] = [float(D(b["price"])) for b in past]
                if any(not math.isfinite(v) or v <= 0 for v in self.history[ticker]):
                    raise RuntimeError("Invalid historical price")
            book = self.client.book(ticker, instrument.instrument_type, instrument.settlement)
            bids = book.get("bids") or []
            offers = book.get("offers") or []
            if bids and offers:
                bid, ask = D(bids[0]["price"]), D(offers[0]["price"])
                stamp = utc_timestamp(book["date"]) if book.get("date") else now
            else:
                # Empty book is expected outside BYMA session hours. Fall back
                # to last price with zero synthetic spread; Current's own
                # timestamp is what lets the existing staleness veto in
                # Guard.check keep the worker from trading a closed market,
                # rather than inventing a separate session-hours system.
                current = self.client.current(
                    ticker, instrument.instrument_type, instrument.settlement
                )
                if not current.get("price") or not current.get("date"):
                    raise RuntimeError("No book or current price available")
                bid = ask = D(current["price"])
                stamp = utc_timestamp(current["date"])
            quote = Quote(
                ticker,
                bid,
                ask,
                stamp,
                D("0.01"),
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
        self.tick = 0
        self.history = {i.ticker: [] for i in instruments}

    def snapshot(self):
        base = {"GGAL": 5000, "YPF": 40000, "PAMP": 2000, "AAPL": 30000, "KO": 9000}
        quotes = {}
        for j, instrument in enumerate(self.instruments):
            ticker = instrument.ticker
            price = D(base[ticker]) * D(1 + 0.025 * math.sin(self.tick * 0.6 + j))
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
                    float(D(base[ticker]) * D(1 + 0.01 * math.sin(i * 0.4 + j)))
                    for i in range(80)
                ]
        self.tick += 1
        return quotes

    record = PPIMarket.record
