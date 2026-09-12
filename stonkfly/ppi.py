"""Thin adapter over PPI's official `ppi_client` SDK (PyPI: ppi-client).

Hand-rolling REST calls against PPI's public docs proved unreliable: the docs
never render a concrete base server URL, and real working examples in the
vendor's own documentation revealed field values (`operation`, `quantityType`)
that differ in casing from what the prose/enum listing implied. The vendor
SDK is authoritative on all of this (base URLs, auth-header wiring, exact
field casing) and is used here as a dependency, the same way the retired
Coinbase integration depended on Coinbase's official SDK rather than a
hand-rolled client.

The vendor SDK itself raises bare `Exception` for any non-200 response (it
does not use its own `ppi_exceptions` types), so every call here is wrapped
into a single `PPIError` — callers must treat that uniformly as an unknown
outcome, never as an implied failure or success.
"""

from ppi_client.models.disclaimer import Disclaimer
from ppi_client.models.order import Order
from ppi_client.models.order_budget import OrderBudget
from ppi_client.models.order_confirm import OrderConfirm
from ppi_client.ppi import PPI


class PPIError(RuntimeError):
    pass


class PPIClient:
    def __init__(self, sandbox, api_key, api_secret):
        self._ppi = PPI(sandbox=sandbox)
        self._api_key = api_key
        self._api_secret = api_secret
        self._logged_in = False

    @classmethod
    def from_env(cls, env):
        missing = [k for k in ["PPI_API_KEY", "PPI_API_SECRET"] if not env.get(k)]
        if missing:
            raise RuntimeError(f"Missing PPI credentials: {', '.join(missing)}")
        sandbox = env.get("PPI_ENV", "sandbox") != "production"
        return cls(sandbox, env["PPI_API_KEY"], env["PPI_API_SECRET"])

    def _ensure_login(self):
        if self._logged_in:
            return
        try:
            self._ppi.account.login_api(self._api_key, self._api_secret)
        except Exception as e:
            raise PPIError(f"PPI login failed: {e}") from e
        self._logged_in = True

    def _call(self, fn, *a, **kw):
        self._ensure_login()
        try:
            return fn(*a, **kw)
        except Exception as e:
            raise PPIError(str(e)) from e

    def balances_and_positions(self, account_number):
        return self._call(self._ppi.account.get_balance_and_positions, account_number)

    def current(self, ticker, instrument_type, settlement):
        return self._call(self._ppi.marketdata.current, ticker, instrument_type, settlement)

    def book(self, ticker, instrument_type, settlement):
        return self._call(self._ppi.marketdata.book, ticker, instrument_type, settlement)

    def search(self, ticker, instrument_type, settlement, date_from, date_to):
        # date_from/date_to must be datetime objects, not strings: the vendor
        # SDK builds the query string with plain str()/format(), so passing
        # an already-formatted string would double-encode it incorrectly.
        return self._call(
            self._ppi.marketdata.search, ticker, instrument_type, settlement, date_from, date_to
        )

    def budget(self, **kw):
        params = OrderBudget(
            accountNumber=kw["accountNumber"],
            quantity=kw["quantity"],
            price=kw["price"],
            ticker=kw["ticker"],
            instrumentType=kw["instrumentType"],
            quantityType=kw["quantityType"],
            operationType=kw["operationType"],
            operationTerm=kw["operationTerm"],
            operationMaxDate=kw.get("operationMaxDate"),
            operation=kw["operation"],
            settlement=kw["settlement"],
        )
        return self._call(self._ppi.orders.budget, params)

    def confirm(self, **kw):
        disclaimers = [
            Disclaimer(code=d["code"], accepted=d.get("accepted", True))
            for d in kw.get("disclaimers") or []
        ]
        params = OrderConfirm(
            accountNumber=kw["accountNumber"],
            quantity=kw["quantity"],
            price=kw["price"],
            ticker=kw["ticker"],
            instrumentType=kw["instrumentType"],
            quantityType=kw["quantityType"],
            operationType=kw["operationType"],
            operationTerm=kw["operationTerm"],
            operationMaxDate=kw.get("operationMaxDate"),
            operation=kw["operation"],
            settlement=kw["settlement"],
            disclaimers=disclaimers,
            externalId=kw["externalID"],
        )
        return self._call(self._ppi.orders.confirm, params)

    def active_orders(self, account_number):
        return self._call(self._ppi.orders.get_active_orders, account_number)

    def detail(self, account_number, order_id=None, external_id=None):
        return self._call(
            self._ppi.orders.get_order_detail, account_number, order_id or "", external_id or ""
        )

    def cancel(self, account_number, order_id=None, external_id=None):
        return self._call(
            self._ppi.orders.cancel_order,
            Order(id=order_id, account_number=account_number, externalId=external_id),
        )
