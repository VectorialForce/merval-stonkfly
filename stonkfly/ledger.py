"""Durable money accounting and order intent. All values use Decimal strings."""

import contextlib
import json
import sqlite3
import sys
import uuid
from pathlib import Path

from .config import D


class Ledger:
    def __init__(self, path, settings, mode, accept_settings_change=False):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY,status TEXT NOT NULL,created REAL NOT NULL,plan TEXT NOT NULL,exchange_id TEXT,settlement TEXT)"
        )
        if self.get("settings") is None:
            with self.transaction():
                for k, v in {
                    "settings": settings.signature(),
                    "mode": mode,
                    "cash": settings.capital,
                    "initial_cash": settings.capital,
                    "positions": {},
                    "anchor": settings.capital,
                    "tick": 0,
                    "cursor": 0,
                    "checkpoint": None,
                    "halted": None,
                    "last_attempt": 0,
                }.items():
                    self.put(k, v)
        elif self.get("mode") != mode:
            # paper/live is a fundamental identity, never crossed silently.
            raise RuntimeError(
                "Run mode mismatch; use a separate run directory"
            )
        elif self.get("settings") != settings.signature():
            if not accept_settings_change:
                raise RuntimeError(
                    "Settings changed since this run directory was initialized; use a "
                    "separate run directory, or review the change and pass --accept-migration"
                )
            print(
                f"--accept-migration: settings {self.get('settings')[:12]} -> "
                f"{settings.signature()[:12]}",
                file=sys.stderr,
            )
            with self.transaction():
                self.put("settings", settings.signature())

    @contextlib.contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def get(self, key):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key, value):
        self.db.execute(
            "INSERT OR REPLACE INTO meta VALUES (?,?)",
            (key, json.dumps(value, allow_nan=False)),
        )

    @property
    def cash(self):
        return D(self.get("cash"))

    @property
    def positions(self):
        return {k: D(v) for k, v in self.get("positions").items()}

    def equity(self, quotes):
        return self.cash + sum(
            (v * quotes[p].bid for p, v in self.positions.items()), D(0)
        )

    def halt(self, reason):
        self.put("halted", reason)

    def reserve(self, plan, now):
        cid = str(uuid.uuid4())
        plan = {**plan, "client_order_id": cid}
        with self.transaction():
            if self.pending():
                raise RuntimeError("Unreconciled order exists")
            self.db.execute(
                "INSERT INTO orders(id,status,created,plan) VALUES (?,?,?,?)",
                (cid, "PREPARED", now, json.dumps(plan)),
            )
            self.put("last_attempt", now)
        return plan

    def mark(self, cid, status, exchange_id=None):
        self.db.execute(
            "UPDATE orders SET status=?,exchange_id=COALESCE(?,exchange_id) WHERE id=?",
            (status, exchange_id, cid),
        )

    def pending(self):
        rows = self.db.execute(
            "SELECT id,status,created,plan,exchange_id FROM orders WHERE status NOT IN ('SETTLED','REJECTED') ORDER BY created"
        ).fetchall()
        return [
            {
                "id": r[0],
                "status": r[1],
                "created": r[2],
                "plan": json.loads(r[3]),
                "exchange_id": r[4],
            }
            for r in rows
        ]

    def attempts_today(self, now):
        return self.db.execute(
            "SELECT COUNT(*) FROM orders WHERE created>=?", (now - now % 86400,)
        ).fetchone()[0]

    def settle(self, cid, base, quote, fee):
        base, quote, fee = map(D, (base, quote, fee))
        if min(base, quote, fee) < 0:
            raise ValueError("Negative settlement")
        if (base == 0 and (quote or fee)) or (base > 0 and quote == 0):
            raise ValueError("Inconsistent fill quantities")
        with self.transaction():
            row = self.db.execute(
                "SELECT status,plan,settlement FROM orders WHERE id=?", (cid,)
            ).fetchone()
            if not row:
                raise RuntimeError("Unknown order")
            payload = {"base": str(base), "quote": str(quote), "fee": str(fee)}
            if row[0] == "SETTLED":
                if json.loads(row[2]) != payload:
                    raise RuntimeError("Settlement changed after finalization")
                return
            if row[0] == "REJECTED":
                raise RuntimeError("Cannot settle a rejected intent")
            p = json.loads(row[1])
            positions = self.positions
            held = positions.get(p["product"], D(0))
            cash = self.cash
            if base > D(p["base_size"]):
                raise RuntimeError("Fill exceeds requested quantity")
            if p["side"] == "BUY":
                if quote > D(p["limit_price"]) * base + D(".00000001"):
                    raise RuntimeError("Buy fill exceeded limit price")
                cash -= quote + fee
                positions[p["product"]] = held + base
            else:
                if quote + D(".00000001") < D(p["limit_price"]) * base:
                    raise RuntimeError("Sell fill below limit price")
                cash += quote - fee
                positions[p["product"]] = held - base
            if cash < 0 or positions[p["product"]] < 0:
                raise RuntimeError("Fill exceeds reserved account funds")
            self.put("cash", str(cash))
            self.put("positions", {k: str(v) for k, v in positions.items()})
            self.db.execute(
                "UPDATE orders SET status='SETTLED',settlement=? WHERE id=?",
                (json.dumps(payload), cid),
            )
            if fee > D(p["fee_ceiling"]):
                self.halt(
                    "Actual fee exceeded preview ceiling; fill recorded, further orders stopped"
                )

    def commit_tick(self, anchor, checkpoint, observation=None):
        with self.transaction():
            self.put("anchor", str(anchor))
            self.put("checkpoint", checkpoint)
            self.put("tick", self.get("tick") + 1)
            if observation is not None:
                self.put("observation", observation)

    def close(self):
        self.db.close()
