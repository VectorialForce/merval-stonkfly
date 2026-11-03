"""PPI (Portfolio Personal Inversiones) execution, and the paper double.

PPI documents no immediate-or-cancel/FOK order term, so live execution submits
a day limit order and force-cancels it at a bounded deadline if still resting,
keeping the same bounded-risk shape the Guard's cooldown model assumes for
crypto FOK orders. Intent is persisted before the request. An ambiguous result
is never retried as a new order.

Known documentation gaps, confirmed against real live orders:
PPI's Order/Detail "quantity"/"amount" reflect the originally *requested*
size even when nothing filled, not a filled-vs-requested split, so a
cancelled/rejected order is identified by its "status" field ("CANCELADA"
confirmed; "RECHAZADA" inferred, unconfirmed) rather than by quantity==0.
There is also no commission field on Order/Detail, so a real fill's true fee
is recovered empirically, from the gap between the account's real cash and
what the ledger would expect assuming zero fee (see _observed_fee) — the
existing fee_ceiling check still halts further orders if that gap is ever
larger than what was reserved for the trade.
"""

import os
import time

from .config import D
from .risk import Veto

TERMINAL_WAIT_SECONDS = 120


class UnresolvedOrder(RuntimeError):
    pass


class PaperBroker:
    mode = "paper"

    def __init__(self, settings, ledger):
        self.s = settings
        self.l = ledger

    def preflight(self):
        return {"mode": "paper", "network_execution": False}

    def verify_balances(self):
        pass

    def reconcile(self):
        # Paper requests never leave the process; the immutable plan contains
        # the execution quote, so an interrupted fill can settle exactly once.
        for row in self.l.pending():
            self._fill(row["plan"])

    def execute(self, plan, before_submit):
        try:
            before_submit(plan)
        except Exception:
            self.l.mark(plan["client_order_id"], "REJECTED")
            raise
        return self._fill(plan)

    def _fill(self, p):
        size = D(p["base_size"])
        price = D(p["observed_ask"] if p["side"] == "BUY" else p["observed_bid"])
        value = size * price
        fee = value * D(self.s.paper_fee)
        self.l.settle(p["client_order_id"], size, value, fee)
        return {
            "mode": "paper",
            "status": "FILLED",
            "base": str(size),
            "quote": str(value),
            "fee": str(fee),
        }


class PPIBroker:
    mode = "live"
    # Currency residue below these amounts is ignored as unconvertible funding
    # dust (e.g. leftover cents from a USD->ARS conversion), not a real
    # external position the strategy needs to reconcile against.
    DUST_TOLERANCE = {"USD": D("1.00")}

    def __init__(self, settings, ledger, client, account_number):
        if not account_number:
            raise ValueError("PPI account number required")
        self.s = settings
        self.l = ledger
        self.client = client
        self.account_number = account_number

    @classmethod
    def from_env(cls, settings, ledger):
        if os.environ.get("STONKFLY_LIVE") != "I_ACCEPT_REAL_TRADES":
            raise RuntimeError("Live opt-in missing")
        account_number = os.environ.get("PPI_ACCOUNT_NUMBER")
        if not account_number:
            raise RuntimeError("Set PPI_ACCOUNT_NUMBER locally")
        # Imported here, not at module load: paper/fixture runs construct
        # PaperBroker and must never require httpx or any PPI-specific code.
        from .ppi import PPIClient

        return cls(settings, ledger, PPIClient.from_env(os.environ), account_number)

    def balances(self):
        # BalancesAndPositions' response is an object with "groupedAvailability"
        # (cash, keyed by "symbol") and "groupedInstruments" (holdings, keyed
        # by "ticker") — the REST docs' page shows a flat array instead, but
        # the vendor SDK's own example code (which is what actually runs)
        # confirms this nested shape.
        data = self.client.balances_and_positions(self.account_number)
        result = {}
        for group in data.get("groupedAvailability", []):
            for item in group.get("availability", []):
                symbol = item.get("symbol")
                # One settlement term can be negative while a purchase awaits
                # settlement (real A-24HS debit covered by INMEDIATA cash), so
                # only the net per symbol, checked below, means an overdraft.
                result[symbol] = result.get(symbol, D(0)) + D(item.get("amount", 0))
        if any(amount < 0 for amount in result.values()):
            raise RuntimeError("Negative external balance")
        for group in data.get("groupedInstruments", []):
            for item in group.get("instruments", []):
                ticker = item.get("ticker")
                # Confirmed against a real held position: "amount" here is
                # the position's market VALUE (price * quantity, e.g. 6690
                # for 1 GGAL share), not a share count — unlike
                # groupedAvailability's "amount", which really is a plain
                # cash amount. self.l.positions is a share count, so this
                # must read "quantity" or every held position permanently
                # mismatches (6690 vs 1) the moment it's actually held.
                quantity = D(item.get("quantity", 0))
                if quantity < 0:
                    raise RuntimeError("Negative external holding")
                result[ticker] = result.get(ticker, D(0)) + quantity
        return result

    def preflight(self, adopt_market=None):
        """With `adopt_market`, a fresh ledger starts from the account's real
        cash and holdings instead of requiring an ARS-only account."""
        self.reconcile()
        balances = self.balances()
        if adopt_market is not None and self.l.get("live_initialized"):
            raise RuntimeError(
                "--adopt-balances only initializes a fresh run directory; this one is in use"
            )
        if not self.l.get("live_initialized"):
            if (
                self.l.get("tick")
                or self.l.db.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
            ):
                raise RuntimeError("Uninitialized live ledger already has activity")
            other = {
                k: v
                for k, v in balances.items()
                if k != "ARS" and v > self.DUST_TOLERANCE.get(k, D(0))
            }
            cash = balances.get("ARS", D(0))
            if adopt_market is not None:
                self._adopt(cash, other, adopt_market)
            else:
                if other:
                    raise RuntimeError(
                        f"Start with only ARS cash, no existing positions; saw "
                        f"{ {k: str(v) for k, v in other.items()} }"
                    )
                if not 0 < cash <= D(self.s.capital):
                    raise RuntimeError(
                        f"Fund the account with 0 < ARS <= configured cap; saw balances {dict((k, str(v)) for k, v in balances.items())}"
                    )
                with self.l.transaction():
                    for k in ["cash", "initial_cash", "anchor"]:
                        self.l.put(k, str(cash))
                    self.l.put("live_initialized", True)
        self.verify_balances()
        return {"mode": "live", "account_scoped": True}

    def _adopt(self, cash, positions, market):
        # Pre-existing holdings are manual, not the fly's trades: valuing them
        # into initial_cash and anchor keeps them out of reinforcement and the
        # loss stop's baseline. The funding cap covers their value too.
        foreign = sorted(set(positions) - set(self.s.products))
        if foreign:
            raise RuntimeError(f"Holdings outside --products: {foreign}")
        quotes = market.snapshot(set(positions))
        now = time.time()
        if any(
            not -0.5 <= now - q.timestamp <= self.s.max_quote_age
            for q in quotes.values()
        ):
            raise RuntimeError("Stale quote; adopt balances during market hours")
        equity = cash + sum((v * quotes[k].bid for k, v in positions.items()), D(0))
        if not 0 < equity <= D(self.s.capital):
            raise RuntimeError(f"Adopted equity {equity} outside 0 < ARS <= configured cap")
        with self.l.transaction():
            self.l.put("cash", str(cash))
            self.l.put("positions", {k: str(v) for k, v in positions.items()})
            self.l.put("initial_cash", str(equity))
            self.l.put("anchor", str(equity))
            self.l.put("live_initialized", True)

    def verify_balances(self):
        actual = self.balances()
        expected = {"ARS": self.l.cash}
        for p, amount in self.l.positions.items():
            expected[p] = amount
        for symbol in set(actual) | set(expected):
            tolerance = (
                D(".02") if symbol == "ARS" else self.DUST_TOLERANCE.get(symbol, D(0))
            )
            if abs(actual.get(symbol, D(0)) - expected.get(symbol, D(0))) > tolerance:
                raise RuntimeError(
                    "External balance change; stop and reconcile rather than treat deposits as profit"
                )
        if self._active_ids():
            raise RuntimeError("External/open order on account")

    def _active_ids(self):
        return {str(o.get("id")) for o in self.client.active_orders(self.account_number)}

    def execute(self, p, before_submit):
        cid = p["client_order_id"]
        instrument = self.s.instrument(p["product"])
        side = p["side"]
        common = dict(
            accountNumber=self.account_number,
            ticker=instrument.ticker,
            instrumentType=instrument.instrument_type,
            settlement=instrument.settlement,
            quantity=int(D(p["base_size"])),
            price=float(D(p["limit_price"])),
            quantityType="Papeles",
            operation="Compra" if side == "BUY" else "Venta",
            operationType=p["operation_type"],
            operationTerm=p["operation_term"],
        )
        try:
            try:
                # Budget is PPI's pre-trade check, analogous to a preview: it
                # raises PPIError on rejection since no documented error
                # schema exists to inspect for a soft-reject field. Treated
                # as a Veto (skip this tick), not a halt, mirroring how a
                # Coinbase preview rejection was handled. Its response also
                # carries the disclaimers Confirm requires to be individually
                # accepted; there is no way to know them ahead of time.
                budget = self.client.budget(**common)
            except Veto:
                raise
            except Exception as e:
                raise Veto(f"PPI budget rejected: {e}") from e
            disclaimers = [
                {"code": d["code"], "accepted": True} for d in budget.get("disclaimers") or []
            ]
            if time.time() - p["quote_timestamp"] > self.s.max_quote_age:
                raise Veto("Quote expired during preview")
            self.verify_balances()
            before_submit(p)
        except Exception:
            self.l.mark(cid, "REJECTED")
            raise
        # This durable transition precedes any request that can place an order.
        self.l.mark(cid, "UNKNOWN")
        try:
            r = self.client.confirm(**common, disclaimers=disclaimers, externalID=cid)
        except Exception as e:
            raise UnresolvedOrder(
                "Submission outcome unknown; reconcile before any further trade"
            ) from e
        oid = r.get("id")
        if not oid:
            raise UnresolvedOrder("PPI response lacks an unambiguous order ID")
        self.l.mark(cid, "ACCEPTED", str(oid))
        return self._await_terminal(cid, oid, p)

    def _await_terminal(self, cid, oid, p):
        deadline = time.monotonic() + TERMINAL_WAIT_SECONDS
        cancelled = False
        while True:
            if str(oid) not in self._active_ids():
                return self._finalize(cid, oid, p)
            if time.monotonic() >= deadline:
                if cancelled:
                    raise UnresolvedOrder(
                        "Order still active after cancel attempt; reconcile manually"
                    )
                try:
                    self.client.cancel(self.account_number, order_id=oid, external_id=cid)
                except Exception as e:
                    raise UnresolvedOrder(
                        "Cancel attempt failed after deadline; reconcile before any further trade"
                    ) from e
                cancelled = True
                continue
            time.sleep(1)

    def _finalize(self, cid, oid, p):
        detail = self.client.detail(self.account_number, order_id=oid, external_id=cid)
        if str(detail.get("id")) != str(oid) or detail.get("externalID") != cid:
            raise UnresolvedOrder("Order identity mismatch")
        # Confirmed against a real order: PPI's Detail returns "operation" in
        # upper case ("COMPRA"/"VENTA"), unlike the title-case value this same
        # code submits as the request's "operation" field.
        expected_operation = "COMPRA" if p["side"] == "BUY" else "VENTA"
        if detail.get("ticker") != p["product"] or detail.get("operation") != expected_operation:
            raise UnresolvedOrder("Order identity mismatch")
        # Confirmed against a real cancelled order: Detail's quantity/amount
        # reflect the originally *requested* size even when nothing filled
        # ("1"/"6778.73" for an order that never executed), so quantity==0 is
        # not the reliable no-fill signal the code previously assumed before
        # any live order had been observed. "CANCELADA" is a confirmed
        # terminal no-fill status; "RECHAZADA" is the plausible rejected
        # counterpart but is unconfirmed against a real response.
        if detail.get("status") in ("CANCELADA", "RECHAZADA"):
            self.l.mark(cid, "REJECTED")
            return {"mode": "live", "status": "REJECTED"}
        for key in ["quantity", "amount"]:
            if key not in detail:
                raise UnresolvedOrder("Final order lacks fill accounting")
        quantity = D(detail["quantity"])
        amount = D(detail["amount"])
        if quantity == 0:
            self.l.mark(cid, "REJECTED")
            return {"mode": "live", "status": "REJECTED"}
        if quantity > D(p["base_size"]):
            raise UnresolvedOrder("Filled quantity exceeds requested size")
        limit = D(p["limit_price"])
        if p["side"] == "BUY" and amount > limit * quantity + D(".01"):
            raise UnresolvedOrder("Buy fill exceeded limit price")
        if p["side"] == "SELL" and amount + D(".01") < limit * quantity:
            raise UnresolvedOrder("Sell fill below limit price")
        self.l.settle(cid, quantity, amount, self._observed_fee(p, amount))
        return {"mode": "live", "status": "SETTLED", "client_order_id": cid}

    def _observed_fee(self, plan, amount):
        # Confirmed against a real fill: PPI charged a real commission this
        # ledger has no field to read (Order/Detail has none, see module
        # docstring), producing exactly the cash mismatch verify_balances()
        # was already designed to catch. The only way to recover the true
        # fee is empirically: compare this trade's notional against how much
        # the real account's cash actually moved since the ledger's own
        # pre-trade balance (there is at most one order in flight at a time,
        # so no other settlement can have touched it in between). A
        # non-positive result (cash moved by no more than the notional
        # itself) is treated as no discoverable fee rather than a rebate,
        # since Ledger.settle() rejects a negative fee outright; the
        # existing fee_ceiling check in Ledger.settle() still halts further
        # orders if this ever comes back larger than what was reserved.
        real_ars = self.balances().get("ARS", D(0))
        if plan["side"] == "BUY":
            implied = (self.l.cash - amount) - real_ars
        else:
            implied = amount - (real_ars - self.l.cash)
        return implied if implied > 0 else D(0)

    def reconcile(self):
        for row in self.l.pending():
            cid = row["id"]
            p = row["plan"]
            oid = row["exchange_id"]
            if row["status"] == "PREPARED":
                # Network submission cannot have happened before UNKNOWN.
                self.l.mark(cid, "REJECTED")
                continue
            if not oid:
                detail = self.client.detail(self.account_number, external_id=cid)
                if detail.get("externalID") != cid or not detail.get("id"):
                    raise UnresolvedOrder(
                        "Uncertain submission not found by externalID. Check PPI; no automatic resubmission."
                    )
                oid = detail["id"]
                self.l.mark(cid, "ACCEPTED", str(oid))
            if str(oid) in self._active_ids():
                raise UnresolvedOrder("Order still pending at PPI")
            self._finalize(cid, oid, p)
