"""No tests send orders to PPI. Client calls here are in-memory doubles."""

import dataclasses
import time

import pytest
from pydantic import ValidationError

from stonkfly.actions import StonkflyActions
from stonkfly.broker import PaperBroker, PPIBroker, UnresolvedOrder
from stonkfly.cli import _account_check
from stonkfly.config import D, Settings
from stonkfly.ledger import Ledger
from stonkfly.market import Quote
from stonkfly.ppi import PPIError
from stonkfly.reinforcement import reinforcement
from stonkfly.risk import Guard, Veto


def quote(**changes):
    q = Quote(
        "GGAL",
        D("100"),
        D("100.1"),
        time.time(),
        D(".01"),
        D("0"),
        D("1"),
    )
    return dataclasses.replace(q, **changes)


@pytest.fixture
def env(tmp_path):
    s = Settings()
    ledger = Ledger(tmp_path / "ledger.sqlite", s, "paper")
    guard = Guard(s, ledger, tmp_path / "STOP")
    yield s, ledger, guard
    ledger.close()


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", True])
def test_nonfinite_money(value):
    with pytest.raises(ValueError):
        D(value)


@pytest.mark.parametrize(
    "changes",
    [
        dict(capital="100001"),
        dict(order_limit="10001"),
        dict(neural_bin_ms=0.01),
        dict(reward_deadband="0"),
        dict(interval_seconds=float("nan")),
        dict(daily_orders=1.5),
        dict(products=("AAPL",)),
        dict(products=("GGAL", "KO")),
    ],
)
def test_configuration_bounds(changes):
    with pytest.raises(ValueError):
        Settings(**changes)


@pytest.mark.parametrize(
    "changes",
    [
        dict(timestamp=time.time() - 100),
        dict(timestamp=time.time() + 100),
        dict(ask=D("102")),
        dict(product="YPF"),
    ],
)
def test_quote_veto(env, changes):
    _, _, g = env
    with pytest.raises(Veto):
        g.plan("GGAL", "BUY", {"GGAL": quote(**changes)})


def test_money_limits_and_no_short(env):
    _, l, g = env
    quotes = {"GGAL": quote()}
    p = g.plan("GGAL", "BUY", quotes)
    assert D(p["base_size"]) * D(p["limit_price"]) + D(p["fee_ceiling"]) <= D("10000")
    with pytest.raises(Veto):
        g.plan("GGAL", "SELL", quotes)
    l.put("cash", ".1")
    with pytest.raises(Veto):
        g.plan("GGAL", "BUY", quotes)


def test_stop_loss_and_external_stop(env):
    _, l, g = env
    g.stop_file.touch()
    with pytest.raises(Veto):
        g.plan("GGAL", "BUY", {"GGAL": quote()})
    g.stop_file.unlink()
    l.put("cash", "79000")
    with pytest.raises(Veto):
        g.check({"GGAL": quote()}, time.time(), {"GGAL"})
    assert "Loss stop" in l.get("halted")


def test_paper_accounting_and_cooldown(env):
    s, l, g = env
    provider = StonkflyActions(g, PaperBroker(s, l))
    provider.quotes = {"GGAL": quote()}
    r = provider.invoke({"product": "GGAL", "side": "BUY"})
    assert r["status"] == "FILLED"
    assert l.cash == D("100000") - D(r["quote"]) - D(r["fee"])
    assert l.positions["GGAL"] == D(r["base"])
    assert reinforcement(l.equity(provider.quotes), "100000", "10")[0] == "aversive"
    with pytest.raises(Veto):
        provider.invoke({"product": "GGAL", "side": "BUY"})
    with pytest.raises(ValidationError):
        provider.invoke({"product": "GGAL", "side": "BUY", "size": 99})


def test_daily_attempt_limit(env):
    _, l, g = env
    l.attempts_today = lambda now: 24
    with pytest.raises(Veto, match="Daily"):
        g.plan("GGAL", "BUY", {"GGAL": quote()})


def test_crash_recovery_and_exactly_once_paper(env):
    s, l, g = env
    p = l.reserve(g.plan("GGAL", "BUY", {"GGAL": quote()}), time.time())
    path = l.path
    other = Ledger(path, s, "paper")
    broker = PaperBroker(s, other)
    broker.reconcile()
    cash = other.cash
    broker.reconcile()
    assert other.cash == cash
    with pytest.raises(RuntimeError, match="changed"):
        other.settle(p["client_order_id"], "0", "0", "0")
    other.close()


def test_invalid_fill_and_fee_overrun(env):
    _, l, g = env
    p = l.reserve(g.plan("GGAL", "BUY", {"GGAL": quote()}), time.time())
    with pytest.raises(ValueError):
        l.settle(p["client_order_id"], 0, 1, 0)
    with pytest.raises(RuntimeError):
        # More shares than were ever requested: "Fill exceeds requested quantity".
        oversized = D(p["base_size"]) + 1
        l.settle(p["client_order_id"], oversized, oversized * 100, 0)
    size = D(p["base_size"])
    l.settle(p["client_order_id"], size, size * 100, D(p["fee_ceiling"]) + D(".01"))
    assert "fee exceeded" in l.get("halted")
    assert not l.pending()  # Actual fill remains accounted for, despite the halt.


class FakePPIClient:
    """Models only the fields this repo's PPIBroker reads; PPI's docs do not
    publish a status enum or a fill/requested quantity split, so a
    newly-confirmed order is immediately absent from ActiveOrders here,
    mirroring the FOK-like instant resolution the risk guard is built around.
    """

    def __init__(self):
        self.cash = D(100000)
        self.real_fee = D(0)
        self.ggal = D(0)
        self.orders = {}
        self.next_id = 1
        self.submissions = 0
        self.budget_calls = []
        self.budget_should_raise = False
        self.after_budget = lambda: None
        self.uncertain = False
        self.open_external_orders = []
        self.stuck_orders = False

    def balances_and_positions(self, account_number):
        return {
            "groupedAvailability": [
                {
                    "availability": [
                        {"name": "Disponible", "symbol": "ARS", "amount": str(self.cash), "settlement": "INMEDIATA"}
                    ]
                }
            ],
            "groupedInstruments": [
                {
                    "name": "Acciones",
                    # Real PPI: "quantity" is the share count, "amount" is
                    # market value (price * quantity) — code must read
                    # "quantity" against self.l.positions' share counts.
                    "instruments": [
                        {"ticker": "GGAL", "price": "0", "quantity": str(self.ggal), "amount": "0"}
                    ],
                }
            ],
        }

    def active_orders(self, account_number):
        return [{"id": o["id"]} for o in self.orders.values() if o["active"]] + list(
            self.open_external_orders
        )

    def budget(self, **kw):
        self.budget_calls.append(kw)
        self.after_budget()
        if self.budget_should_raise:
            raise PPIError("rejected")
        return {
            "amount": kw["quantity"] * kw["price"],
            "disclaimers": [{"code": "D1", "accepted": False}],
        }

    def confirm(self, **kw):
        self.submissions += 1
        oid = self.next_id
        self.next_id += 1
        quantity = D(kw["quantity"])
        amount = quantity * D(str(kw["price"]))
        # A real commission PPI charges on the account but never reports
        # back through Order/Detail's "amount" field (see broker.py).
        if kw["operation"] == "Compra":
            self.cash -= amount + self.real_fee
        else:
            self.cash += amount - self.real_fee
        self.ggal += quantity if kw["operation"] == "Compra" else -quantity
        self.orders[oid] = {
            "id": oid,
            "externalID": kw["externalID"],
            "ticker": kw["ticker"],
            # Real PPI Detail responses report this upper case ("COMPRA"/
            # "VENTA") regardless of the title-case value submitted.
            "operation": kw["operation"].upper(),
            "quantity": str(quantity),
            "amount": str(amount),
            "active": self.stuck_orders,
        }
        if self.uncertain:
            raise TimeoutError("Response lost after accepted order")
        return {"id": oid}

    def detail(self, account_number, order_id=None, external_id=None):
        if order_id is not None:
            o = self.orders.get(order_id)
        else:
            o = next(
                (o for o in self.orders.values() if o["externalID"] == external_id), None
            )
        if o is None:
            return {}
        return {k: o[k] for k in ["id", "externalID", "ticker", "operation", "quantity", "amount"]}

    def cancel(self, account_number, order_id=None, external_id=None):
        if self.stuck_orders:
            return  # Simulates a cancel that does not clear the resting order.
        if order_id is not None:
            self.orders[order_id]["active"] = False
        else:
            for o in self.orders.values():
                if o["externalID"] == external_id:
                    o["active"] = False


def live_double(env):
    s, l, g = env
    client = FakePPIClient()
    broker = PPIBroker(s, l, client, "test-account")
    broker.preflight()
    provider = StonkflyActions(g, broker)
    provider.quotes = {"GGAL": quote()}
    return client, broker, provider


def test_ppi_day_limit_fill_and_reconciled_balances(env):
    client, broker, p = live_double(env)
    assert p.invoke({"product": "GGAL", "side": "BUY"})["status"] == "SETTLED"
    assert client.submissions == 1
    broker.verify_balances()


def test_real_commission_is_derived_from_balance_not_assumed_zero(env):
    # PPI's Order/Detail has no commission field (see broker.py), so a real
    # fee only shows up as a gap between the account's real cash and what
    # the ledger would expect if it assumed zero fee. Confirmed against a
    # real live fill during development: the fee must be recovered from that
    # gap, or verify_balances() halts on every single future trade forever.
    client, broker, p = live_double(env)
    client.real_fee = D("17.61")
    result = p.invoke({"product": "GGAL", "side": "BUY"})
    assert result["status"] == "SETTLED"
    l = env[1]
    assert l.cash == client.cash
    broker.verify_balances()  # must not raise: fee was captured, not assumed zero


def test_fee_exceeding_ceiling_halts_further_orders(env):
    client, broker, p = live_double(env)
    client.real_fee = D("500")  # far beyond this order's reserved fee_ceiling (~195)
    result = p.invoke({"product": "GGAL", "side": "BUY"})
    assert result["status"] == "SETTLED"
    l = env[1]
    assert "fee exceeded" in l.get("halted")


@pytest.mark.parametrize("failure", ["budget_rejected", "stop", "stale"])
def test_preview_cannot_bypass_final_guard(env, failure):
    client, broker, p = live_double(env)
    if failure == "budget_rejected":
        client.budget_should_raise = True
    if failure == "stop":
        client.after_budget = env[2].stop_file.touch
    if failure == "stale":
        p.quotes = {"GGAL": quote(timestamp=time.time() - 40)}
    with pytest.raises(Veto):
        p.invoke({"product": "GGAL", "side": "BUY"})
    assert client.submissions == 0
    assert not env[1].pending()


def test_ambiguous_submission_reconciles_without_duplicate(env):
    client, broker, p = live_double(env)
    client.uncertain = True
    with pytest.raises(UnresolvedOrder):
        p.invoke({"product": "GGAL", "side": "BUY"})
    assert env[1].pending()[0]["status"] == "UNKNOWN"
    with pytest.raises(Veto):
        p.invoke({"product": "GGAL", "side": "BUY"})
    broker.reconcile()
    broker.verify_balances()
    broker.reconcile()
    assert client.submissions == 1 and not env[1].pending()


def test_unfound_submission_remains_stopped(env):
    client, broker, p = live_double(env)
    client.uncertain = True
    with pytest.raises(UnresolvedOrder):
        p.invoke({"product": "GGAL", "side": "BUY"})
    client.orders = {}
    with pytest.raises(UnresolvedOrder):
        broker.reconcile()
    assert client.submissions == 1 and env[1].pending()


def test_order_never_leaves_active_orders_is_cancelled_then_unresolved(env, monkeypatch):
    s, l, g = env
    client = FakePPIClient()
    client.stuck_orders = True  # PPI's (simulated) cancel never clears it.
    broker = PPIBroker(s, l, client, "test-account")
    broker.preflight()
    provider = StonkflyActions(g, broker)
    provider.quotes = {"GGAL": quote()}
    plan = g.plan("GGAL", "BUY", provider.quotes)
    plan["neural_observation"] = None
    plan["checkpoint"] = None
    plan = l.reserve(plan, time.time())
    import stonkfly.broker as broker_module

    monkeypatch.setattr(broker_module, "TERMINAL_WAIT_SECONDS", 0)
    with pytest.raises(UnresolvedOrder, match="cancel attempt"):
        broker.execute(plan, g.before_submit)
    assert client.submissions == 1
    assert client.orders[1]["active"] is True  # The (simulated) cancel never took effect.


@pytest.mark.parametrize(
    "change", ["open_order", "existing_position", "overfunded", "external_deposit"]
)
def test_account_boundaries(env, change):
    s, l, _ = env
    client = FakePPIClient()
    broker = PPIBroker(s, l, client, "test-account")
    if change == "open_order":
        client.open_external_orders = [{"id": "external-1"}]
    if change == "existing_position":
        client.ggal = D(5)
    if change == "overfunded":
        client.cash = D(100001)
    if change == "external_deposit":
        broker.preflight()
        client.cash += 1
    with pytest.raises(RuntimeError):
        broker.preflight()
    assert client.submissions == 0


class FakeMarket:
    def __init__(self, **changes):
        self.changes = changes

    def snapshot(self, tickers):
        return {t: quote(**self.changes) for t in tickers}


def test_adopt_balances_starts_from_real_holdings_without_cash(env):
    s, l, _ = env
    client = FakePPIClient()
    client.cash = D(0)
    client.ggal = D(2)
    broker = PPIBroker(s, l, client, "test-account")
    broker.preflight(FakeMarket())
    equity = 2 * quote().bid
    assert l.cash == 0 and l.positions == {"GGAL": D(2)}
    assert D(l.get("initial_cash")) == D(l.get("anchor")) == equity
    broker.verify_balances()
    with pytest.raises(RuntimeError):
        broker.preflight(FakeMarket())


@pytest.mark.parametrize(
    "case", ["outside_products", "over_cap", "stale_quote", "used_ledger"]
)
def test_adopt_balances_boundaries(env, case):
    s, l, _ = env
    client = FakePPIClient()
    client.cash = D(0)
    client.ggal = D(2)
    settings = Settings(products=("PAMP",)) if case == "outside_products" else s
    broker = PPIBroker(settings, l, client, "test-account")
    market = FakeMarket()
    if case == "over_cap":
        client.ggal = D(1000000)
    if case == "stale_quote":
        market = FakeMarket(timestamp=time.time() - 3600)
    if case == "used_ledger":
        l.put("tick", 1)
    with pytest.raises(RuntimeError):
        broker.preflight(market)
    assert not l.get("live_initialized") and l.positions == {}


def split_settlement_balances(immediate, next_day):
    return {
        "groupedAvailability": [
            {
                "availability": [
                    {"symbol": "ARS", "amount": immediate, "settlement": "INMEDIATA"},
                    {"symbol": "ARS", "amount": next_day, "settlement": "A-24HS"},
                ]
            }
        ],
        "groupedInstruments": [],
    }


def test_pending_settlement_debit_nets_against_immediate_cash(env):
    s, l, _ = env
    client = FakePPIClient()
    client.balances_and_positions = lambda _: split_settlement_balances(56422.35, -45828.7)
    assert PPIBroker(s, l, client, "test-account").balances()["ARS"] == D("10593.65")


def test_net_negative_balance_still_stops(env):
    s, l, _ = env
    client = FakePPIClient()
    client.balances_and_positions = lambda _: split_settlement_balances(100, -100.01)
    with pytest.raises(RuntimeError, match="Negative external balance"):
        PPIBroker(s, l, client, "test-account").balances()


class FlakyBroker:
    def __init__(self, failure=None):
        self.failure = failure

    def reconcile(self):
        pass

    def verify_balances(self):
        if self.failure:
            raise self.failure


class Transient(RuntimeError):
    pass


def test_failed_account_read_skips_only_without_pending_orders(env):
    s, l, g = env
    assert _account_check(FlakyBroker(), l, Transient) is None
    broker = FlakyBroker(Transient("empty response"))
    assert _account_check(broker, l, Transient) == "empty response"
    l.reserve(g.plan("GGAL", "BUY", {"GGAL": quote()}), time.time())
    with pytest.raises(Transient):
        _account_check(broker, l, Transient)


def test_balance_mismatch_is_never_a_skip(env):
    _, l, _ = env
    with pytest.raises(RuntimeError, match="External balance change"):
        _account_check(
            FlakyBroker(RuntimeError("External balance change")), l, Transient
        )


@pytest.mark.parametrize(
    "changes, reason",
    [(dict(timestamp=0), "Stale or future quote: GGAL"), (dict(ask=D(1000)), "Spread limit: GGAL")],
)
def test_snapshot_veto_names_the_ticker(env, changes, reason):
    _, _, g = env
    with pytest.raises(Veto, match=reason):
        g.check({"GGAL": quote(**changes)}, time.time(), {"GGAL"})
