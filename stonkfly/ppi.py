"""Thin synchronous PPI (Portfolio Personal Inversiones) REST client.

PPI's public docs do not document an error response schema, nor do they
render a concrete base server URL (the Swagger page resolves it client-side).
Rather than hardcode a guessed host, the base URL is required configuration.
Any non-2xx or unparsable response raises PPIError uniformly; callers must
treat that as an unknown outcome, never as an implied failure or success.
"""

import httpx


class PPIError(RuntimeError):
    pass


class PPIClient:
    def __init__(self, base_url, version, authorized_client, client_key, api_key, api_secret, timeout=10):
        if not base_url:
            raise ValueError(
                "PPI_BASE_URL is required; PPI's docs do not publish a fixed "
                "sandbox/production host, request it with your API credentials"
            )
        self.base_url = base_url.rstrip("/")
        self.version = version
        self.authorized_client = authorized_client
        self.client_key = client_key
        self.api_key = api_key
        self.api_secret = api_secret
        self.http = httpx.Client(timeout=timeout)
        self.access_token = None
        self.refresh_token = None
        self.expiration = 0

    @classmethod
    def from_env(cls, env):
        missing = [
            k
            for k in [
                "PPI_BASE_URL",
                "PPI_AUTHORIZED_CLIENT",
                "PPI_CLIENT_KEY",
                "PPI_API_KEY",
                "PPI_API_SECRET",
            ]
            if not env.get(k)
        ]
        if missing:
            raise RuntimeError(f"Missing PPI credentials: {', '.join(missing)}")
        return cls(
            base_url=env["PPI_BASE_URL"],
            version=env.get("PPI_API_VERSION", "1.0"),
            authorized_client=env["PPI_AUTHORIZED_CLIENT"],
            client_key=env["PPI_CLIENT_KEY"],
            api_key=env["PPI_API_KEY"],
            api_secret=env["PPI_API_SECRET"],
        )

    def _url(self, path):
        return f"{self.base_url}/api/{self.version}/{path}"

    def _raise_for_status(self, r):
        if r.status_code < 200 or r.status_code >= 300:
            raise PPIError(f"PPI {r.request.method} {r.request.url.path} -> {r.status_code}")
        try:
            return r.json()
        except ValueError as e:
            raise PPIError("PPI response was not valid JSON") from e

    def login(self):
        r = self.http.post(
            self._url("Account/LoginApi"),
            headers={
                "AuthorizedClient": self.authorized_client,
                "ClientKey": self.client_key,
                "ApiKey": self.api_key,
                "ApiSecret": self.api_secret,
            },
        )
        body = self._raise_for_status(r)
        for key in ["accessToken", "refreshToken"]:
            if not body.get(key):
                raise PPIError("Login response missing token fields")
        self.access_token = body["accessToken"]
        self.refresh_token = body["refreshToken"]
        self.expiration = body.get("expires") or 0
        return body

    def _refresh(self):
        r = self.http.post(
            self._url("Account/RefreshToken"),
            headers={
                "AuthorizedClient": self.authorized_client,
                "ClientKey": self.client_key,
            },
            json={"refreshToken": self.refresh_token},
        )
        body = self._raise_for_status(r)
        if not body.get("accessToken"):
            raise PPIError("Refresh response missing accessToken")
        self.access_token = body["accessToken"]
        self.refresh_token = body.get("refreshToken", self.refresh_token)

    def _request(self, method, path, retry=True, **kwargs):
        if not self.access_token:
            self.login()
        headers = {
            "AuthorizedClient": self.authorized_client,
            "ClientKey": self.client_key,
            "Authorization": f"Bearer {self.access_token}",
        }
        r = self.http.request(method, self._url(path), headers=headers, **kwargs)
        if r.status_code == 401 and retry:
            self._refresh()
            return self._request(method, path, retry=False, **kwargs)
        return self._raise_for_status(r)

    def balances_and_positions(self, account_number):
        return self._request(
            "GET", "Account/BalancesAndPositions", params={"accountNumber": account_number}
        )

    def current(self, ticker, instrument_type, settlement):
        return self._request(
            "GET",
            "MarketData/Current",
            params={"Ticker": ticker, "Type": instrument_type, "Settlement": settlement},
        )

    def book(self, ticker, instrument_type, settlement):
        return self._request(
            "GET",
            "MarketData/Book",
            params={"Ticker": ticker, "Type": instrument_type, "Settlement": settlement},
        )

    def search(self, ticker, instrument_type, settlement, date_from, date_to):
        return self._request(
            "GET",
            "MarketData/Search",
            params={
                "Ticker": ticker,
                "Type": instrument_type,
                "Settlement": settlement,
                "DateFrom": date_from,
                "DateTo": date_to,
            },
        )

    def budget(self, **body):
        return self._request("POST", "Order/Budget", json=body)

    def confirm(self, **body):
        return self._request("POST", "Order/Confirm", json=body)

    def active_orders(self, account_number):
        return self._request(
            "GET", "Order/ActiveOrders", params={"accountNumber": account_number}
        )

    def detail(self, order_id=None, external_id=None):
        params = {}
        if order_id is not None:
            params["orderID"] = order_id
        if external_id is not None:
            params["externalID"] = external_id
        return self._request("GET", "Order/Detail", params=params)

    def cancel(self, account_number, order_id=None, external_id=None):
        body = {"accountNumber": account_number}
        if order_id is not None:
            body["orderID"] = order_id
        if external_id is not None:
            body["externalID"] = external_id
        return self._request("POST", "Order/Cancel", json=body)
