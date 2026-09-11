import hashlib
import json
import math
from dataclasses import asdict, dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal


@dataclass(frozen=True)
class Instrument:
    ticker: str
    instrument_type: str  # "ACCIONES" | "CEDEARS"
    settlement: str = "INMEDIATA"
    market: str = "BYMA"


# Closed allowlist: a small mix of acciones lideres and CEDEARs, mirroring the
# spirit of the previous 3-pair USDC crypto allowlist. Settlement/tick-size
# conventions are not published by PPI's docs; INMEDIATA (CI) is the simplest,
# most liquid default and should be reconciled against Configuration/Settlements
# and SearchInstrument before any live use.
INSTRUMENTS = {
    i.ticker: i
    for i in [
        Instrument("GGAL", "ACCIONES"),
        Instrument("YPF", "ACCIONES"),
        Instrument("PAMP", "ACCIONES"),
        Instrument("AAPL", "CEDEARS"),
        Instrument("KO", "CEDEARS"),
    ]
}


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
    max_quote_age: float = 15
    neural_ms: float = 500
    neural_bin_ms: float = 10
    pulse_ms: float = 200
    pulse_current: float = 20
    reward_deadband: str = "10"
    decoder_threshold_hz: float = 2
    paper_fee: str = "0.006"
    learning: bool = True

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
