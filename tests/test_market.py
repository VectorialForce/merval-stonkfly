from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from stonkfly.config import D, Instrument
from stonkfly.display import market_frame
from stonkfly.market import PPIMarket, utc_timestamp

GGAL = Instrument("GGAL", "ACCIONES")


def iso(dt):
    return dt.isoformat()


class FakePPIClient:
    def __init__(self):
        self.bars = []
        self._book = {"date": None, "bids": [{"price": "100"}], "offers": [{"price": "100.1"}]}
        self._current = {}

    def search(self, ticker, instrument_type, settlement, date_from, date_to):
        return self.bars

    def book(self, ticker, instrument_type, settlement):
        return self._book

    def current(self, ticker, instrument_type, settlement):
        return self._current


def test_completed_history_and_observation_clock():
    client = FakePPIClient()
    now = datetime.now(timezone.utc)
    client.bars = [
        {"date": iso(now + timedelta(minutes=1)), "price": "999999"},
        {"date": iso(now - timedelta(minutes=1)), "price": "99"},
        {"date": iso(now - timedelta(minutes=2)), "price": "101"},
    ]
    client._book["date"] = iso(now)
    m = PPIMarket((GGAL,), client)
    quotes = m.snapshot(["GGAL"])
    assert m.history["GGAL"] == [101.0, 99.0]
    m.record(quotes)
    assert m.history["GGAL"] == [101.0, 99.0, 100.05]
    m.snapshot(["GGAL"])  # Execution quote refresh must not add another neural observation.
    assert len(m.history["GGAL"]) == 3
    q = quotes["GGAL"]
    frame = market_frame(q.product, m.history[q.product], q.bid, q.ask)
    assert frame.shape == (180, 320, 3) and frame.dtype == np.uint8
    other = market_frame(q.product, list(reversed(m.history[q.product])), q.bid, q.ask)
    assert not np.array_equal(frame, other)


def test_empty_book_falls_back_to_current_price():
    client = FakePPIClient()
    now = datetime.now(timezone.utc)
    client.bars = [{"date": iso(now - timedelta(hours=1)), "price": "100"}]
    client._book = {"date": None, "bids": [], "offers": []}
    client._current = {"date": iso(now), "price": "150"}
    m = PPIMarket((GGAL,), client)
    q = m.snapshot(["GGAL"])["GGAL"]
    assert q.bid == q.ask == D("150")
    assert q.timestamp == pytest.approx(now.timestamp())


def test_no_book_and_no_current_is_a_hard_error():
    client = FakePPIClient()
    now = datetime.now(timezone.utc)
    client.bars = [{"date": iso(now - timedelta(hours=1)), "price": "100"}]
    client._book = {"date": None, "bids": [], "offers": []}
    client._current = {}
    m = PPIMarket((GGAL,), client)
    with pytest.raises(RuntimeError):
        m.snapshot(["GGAL"])


def test_no_historical_bars_bootstraps_from_current_quote():
    # A thinly traded Panel General ticker can go hours without a trade;
    # that's expected, not a reason to crash the whole worker (confirmed
    # against real BYMA data with --products ALL).
    client = FakePPIClient()
    client.bars = []
    m = PPIMarket((GGAL,), client)
    q = m.snapshot(["GGAL"])["GGAL"]
    assert m.history["GGAL"] == [float((q.bid + q.ask) / 2)]


def test_explicit_timestamp_zone():
    with pytest.raises(ValueError):
        utc_timestamp("2026-01-01T00:00:00")
    assert utc_timestamp("2026-01-01T00:00:00Z") == 1767225600
