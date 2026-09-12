"""No tests send orders to PPI. Client calls here are in-memory doubles."""

import dataclasses
import time

import pytest
from pydantic import ValidationError

from stonkfly.actions import StonkflyActions
from stonkfly.broker import PaperBroker, PPIBroker, UnresolvedOrder
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
        g.check({"GGAL": quote()}, time.time())
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
                {"name": "Acciones", "instruments": [{"ticker": "GGAL", "price": "0", "amount": str(self.ggal)}]}
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
        self.cash -= amount
        self.ggal += quantity
        self.orders[oid] = {
            "id": oid,
            "externalID": kw["externalID"],
            "ticker": kw["ticker"],
            "operation": kw["operation"],
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


@pytest.mark.parametrize("failure", ["budget_rejected", "stop", "stale"])
def test_preview_cannot_bypass_final_guard(env, failure):
    client, broker, p = live_double(env)
    if failure == "budget_rejected":
        client.budget_should_raise = True
    if failure == "stop":
        client.after_budget = env[2].stop_file.touch
    if failure == "stale":
        p.quotes = {"GGAL": quote(timestamp=time.time() - 20)}
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
