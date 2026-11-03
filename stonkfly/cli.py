"""Single-worker run loop. Default execution is paper; live must be explicit."""

import argparse
import dataclasses
import fcntl
import hashlib
import json
import os
import random
import sys
import time
import traceback
from pathlib import Path

from .config import D, INSTRUMENTS, Settings


def _account_check(broker, ledger, transient):
    """Reconcile and verify balances; return why this attempt must be skipped.

    A failed read (network drop, empty PPI response, both seen live) proves
    nothing about the account while no order is pending, so it retries next
    cycle. With a pending order the outcome is unknown and execution stops.
    Balance mismatches are not `transient` and always stop.
    """
    try:
        broker.reconcile()
        broker.verify_balances()
    except transient as e:
        if ledger.pending():
            raise
        return str(e)
    return None


def main():
    p = argparse.ArgumentParser(prog="stonkfly")
    sub = p.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--reuse-doomfly", type=Path)
    sub.add_parser("verify")
    instruments_cmd = sub.add_parser(
        "instruments", help="Show or refresh the tradable instrument allowlist"
    )
    instruments_cmd.add_argument(
        "--refresh",
        action="store_true",
        help="Fetch the MERVAL panel's BYMA ACCIONES listings via PPI SearchInstrument, probe each "
        "for a live book/current price, and cache the ones that have one in "
        "data/instruments.json; requires .env PPI credentials. Run this during BYMA "
        "market hours, or every ticker looks dead and the result is empty.",
    )
    run = sub.add_parser("run")
    run.add_argument("--live", action="store_true")
    run.add_argument(
        "--preflight-only",
        action="store_true",
        help="Read-only exchange checks; never submit an order",
    )
    run.add_argument(
        "--resume-reviewed",
        action="store_true",
        help="After manual review, clear a transient halt only after successful reconciliation",
    )
    run.add_argument(
        "--accept-migration",
        action="store_true",
        help="After reviewing what changed in stonkfly's source since this run directory's "
        "last provenance was recorded, keep using it instead of requiring a fresh directory",
    )
    run.add_argument(
        "--adopt-balances",
        action="store_true",
        help="Live only, on a fresh run directory: start from the account's current "
        "ARS and holdings (all must be in --products) instead of requiring an "
        "ARS-only account. Holdings are valued at the current bid as the baseline",
    )
    run.add_argument(
        "--debug",
        action="store_true",
        help="On a halt, print the real exception and traceback to stderr instead "
        "of the redacted message; this may include account/request details, "
        "so only use it in a terminal you trust and never paste the output elsewhere",
    )
    run.add_argument(
        "--fixture",
        action="store_true",
        help="Synthetic offline market input; paper only",
    )
    run.add_argument(
        "--real-data",
        action="store_true",
        help="Paper trading against real PPI quotes; requires .env credentials, never submits orders",
    )
    run.add_argument("--steps", type=int, default=0, help="0 keeps running")
    run.add_argument(
        "--fast",
        action="store_true",
        help="Skip waiting in paper mode; execution cooldown still applies",
    )
    run.add_argument(
        "--frozen",
        action="store_true",
        help="Freeze all memory efficacies for a control run",
    )
    run.add_argument("--out", type=Path)
    run.add_argument(
        "--products",
        nargs="+",
        default=["GGAL"],
        help="Tickers to trade, or the single value ALL to trade every instrument "
        "in the current allowlist (see `stonkfly instruments`)",
    )
    run.add_argument("--neural-ms", type=float, default=500)
    run.add_argument(
        "--sensory-noise-std",
        type=float,
        default=0.0,
        help="Gaussian phototransduction noise std, in [0,1] luminance units "
        "(0 = off, current exact behavior). Upstream of the retained connectome's "
        "propagation/decoding, which this never touches.",
    )
    status = sub.add_parser("status")
    status.add_argument("--out", type=Path, default=Path("runs/paper"))
    a = p.parse_args()
    from dotenv import load_dotenv

    # Never search parent projects for unrelated account credentials.
    load_dotenv(dotenv_path=Path.cwd() / ".env", override=False)
    if a.command in ("prepare", "verify"):
        from .data import prepare, verify

        if a.command == "prepare":
            prepare(a.reuse_doomfly)
        else:
            print(json.dumps(verify()))
        return
    if a.command == "instruments":
        from .config import refresh_instruments

        if a.refresh:
            n = refresh_instruments()
            print(json.dumps({"cached": n, "path": "data/instruments.json"}))
        else:
            print(json.dumps(sorted(INSTRUMENTS)))
        return
    if a.command == "status":
        import sqlite3

        db = sqlite3.connect(f"file:{a.out / 'ledger.sqlite'}?mode=ro", uri=True)
        meta = {k: json.loads(v) for k, v in db.execute("SELECT key,value FROM meta")}
        print(
            json.dumps(
                {
                    k: meta.get(k)
                    for k in [
                        "mode",
                        "tick",
                        "cash",
                        "positions",
                        "initial_cash",
                        "anchor",
                        "halted",
                    ]
                },
                indent=2,
            )
        )
        return
    if a.live and (a.fixture or a.fast or a.real_data):
        p.error("Live mode forbids fixtures, fast replay and --real-data")
    if a.adopt_balances and not a.live:
        p.error("--adopt-balances requires --live")
    if a.real_data and a.fixture:
        p.error("--real-data and --fixture are mutually exclusive")
    if a.steps < 0:
        p.error("steps cannot be negative")
    products = tuple(INSTRUMENTS) if a.products == ["ALL"] else tuple(a.products)
    settings = Settings(
        products=products,
        learning=not a.frozen,
        neural_ms=a.neural_ms,
        pulse_ms=min(200, a.neural_ms),
        sensory_noise_std=a.sensory_noise_std,
    )
    out = a.out or Path("runs/live" if a.live else "runs/paper")
    out.mkdir(parents=True, exist_ok=True)
    lock = (out / "worker.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("A worker already owns this run directory")
    from .broker import PaperBroker, PPIBroker
    from .ledger import Ledger

    ledger = Ledger(
        out / "ledger.sqlite",
        settings,
        "live" if a.live else "paper",
        accept_settings_change=a.accept_migration,
    )
    try:
        broker = (
            PPIBroker.from_env(settings, ledger)
            if a.live
            else PaperBroker(settings, ledger)
        )
        if a.adopt_balances:
            from .market import PPIMarket

            result = broker.preflight(
                PPIMarket([settings.instrument(t) for t in settings.products], broker.client)
            )
        else:
            result = broker.preflight()
        print(json.dumps(result), flush=True)
        if a.resume_reviewed:
            if (out / "STOP").exists() or ledger.pending():
                raise RuntimeError(
                    "Remove STOP only after review; unresolved orders cannot resume"
                )
            reason = ledger.get("halted")
            if reason and ("Loss stop" in reason or "fee exceeded" in reason):
                raise RuntimeError("A financial stop cannot be cleared by this flag")
            ledger.put("halted", None)
        if a.preflight_only:
            return
        from .data import verify

        verified = verify()
        from PIL import Image

        from .actions import StonkflyActions
        from .display import market_frame
        from .market import FixtureMarket, PPIMarket
        from .neural.controller import FlyController
        from .reinforcement import reinforcement
        from .risk import Guard, Veto

        instruments = [settings.instrument(t) for t in settings.products]
        if a.live or a.real_data:
            # Imported here, not at module load: a plain fixture/paper run
            # must never require httpx or any PPI-specific code.
            from .ppi import PPIError

            if a.live:
                market = PPIMarket(instruments, broker.client)
            else:
                from .ppi import PPIClient

                market = PPIMarket(instruments, PPIClient.from_env(os.environ))
        else:
            market = FixtureMarket(instruments)

            class PPIError(Exception):
                pass
        previous = ledger.get("observation")
        if previous:
            # Merge, don't replace: __init__ already seeded every *current*
            # product with an empty history. A prior run tracking fewer
            # products (e.g. switching from GGAL-only to --products ALL)
            # has no saved history for the newly added tickers, and
            # overwriting wholesale would drop their empty-list entries,
            # crashing snapshot()'s history lookup for anything new.
            for ticker, hist in previous["market_history"].items():
                if ticker in market.history:
                    market.history[ticker] = hist
            if isinstance(market, FixtureMarket):
                market.tick = previous["fixture_tick"]
        controller = FlyController(settings)
        cp = ledger.get("checkpoint")
        if cp:
            path = out / cp["file"]
            if hashlib.sha256(path.read_bytes()).hexdigest() != cp["sha256"]:
                raise RuntimeError("Checkpoint integrity mismatch")
            controller.restore(path)
        provenance = {
            "settings": dataclasses.asdict(settings),
            "dataset": verified,
            "circuit": controller.brain.circuit["report"],
            "vision": controller.brain.visual_report,
            "mode": broker.mode,
            "feed": "ppi-live" if a.live else "ppi-real-data" if a.real_data else "fixture",
            "decoder": "DNp20 mean R-L: buy/sell; DNpe017 spike gate; otherwise hold. Engineered fixed mapping.",
            "learning_validated": False,
            "pain_receptors_modeled": False,
            "timing": "Each observation advances configured neural_ms regardless of wall-market time; no claim of real-time fly physiology.",
            "source_sha256": {
                str(path.relative_to(Path(__file__).parent)): hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
                for path in Path(__file__).parent.rglob("*")
                if path.suffix in (".py", ".cpp")
            },
        }
        signature = hashlib.sha256(
            json.dumps(provenance, sort_keys=True).encode()
        ).hexdigest()
        stored_signature = ledger.get("provenance_sha256")
        if stored_signature not in (None, signature):
            if not a.accept_migration:
                raise RuntimeError(
                    "Run source/protocol changed; use a separate paper run, or review what "
                    "changed and pass --accept-migration to keep this run directory"
                )
            print(
                f"--accept-migration: provenance {stored_signature[:12]} -> {signature[:12]}",
                file=sys.stderr,
            )
        ledger.put("provenance_sha256", signature)
        (out / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
        guard = Guard(settings, ledger, out / "STOP")
        provider = StonkflyActions(guard, broker)
        count = 0

        def wait(started):
            if not a.fast:
                until = started + settings.interval_seconds
                while time.monotonic() < until and not (out / "STOP").exists():
                    time.sleep(min(1, until - time.monotonic()))

        while not a.steps or count < a.steps:
            started = time.monotonic()
            if (out / "STOP").exists() or ledger.get("halted"):
                break
            skipped = _account_check(broker, ledger, PPIError)
            if skipped:
                print(json.dumps({"tick": ledger.get("tick"), "skipped": skipped}), flush=True)
                wait(started)
                continue
            # Pick at random on every attempt, not just committed ticks:
            # `tick` only advances past guard.check()'s skip-and-continue
            # path, so a fixed rotation keyed off it would wedge on a single
            # chronically stale/illiquid ticker forever with a large uneven
            # universe (--products ALL). `cursor` is now just an attempt
            # counter for telemetry, not a selection index.
            cursor = (ledger.get("cursor") or 0) + 1
            ledger.put("cursor", cursor)
            product = random.choice(settings.products)
            needed = {product} | set(ledger.positions)
            try:
                quotes = market.snapshot(needed)
                guard.check(quotes, time.time(), needed)
            except (Veto, PPIError, RuntimeError) as e:
                # A transient rejection of this tick's snapshot (stale quote,
                # wide spread, ...) or a transient market-data fetch failure
                # (PPI network hiccup, empty/malformed response, no live
                # data for this ticker right now — all confirmed against
                # real BYMA data), not a proposed trade: skip this attempt
                # and retry next cycle, exactly like an execution veto below,
                # rather than stopping the worker. The one exception is a
                # genuine account-level stop (e.g. loss stop), which sets
                # `halted` itself before raising; that must still stop us.
                print(json.dumps({"tick": ledger.get("tick"), "skipped": str(e)}), flush=True)
                if ledger.get("halted"):
                    break
                wait(started)
                continue
            market.record(quotes)
            q = quotes[product]
            equity = ledger.equity(quotes)
            kind, delta = reinforcement(
                equity, ledger.get("anchor"), settings.reward_deadband
            )
            frame = market_frame(product, market.history[product], q.bid, q.ask)
            neural = controller.observe(frame, kind)
            # Checkpoint + accounting anchor are committed before any trade.
            # Two slots keep the last committed snapshot safe during a crash.
            slot = ledger.get("tick") % 2
            checkpoint = out / f"brain-{slot}.npz"
            controller.save(checkpoint)
            checkpoint_info = {
                "file": checkpoint.name,
                "sha256": hashlib.sha256(checkpoint.read_bytes()).hexdigest(),
            }
            observation = {
                "neural": neural,
                "product": product,
                "quote": q.json(),
                "pnl_delta_ars": str(delta),
                "market_history": market.history,
                "fixture_tick": getattr(market, "tick", None),
            }
            ledger.commit_tick(equity, checkpoint_info, observation)
            order = {"status": "HOLD"}
            if neural["side"] != "HOLD":
                try:
                    # Neural integration can be slow; use a fresh execution
                    # book. A transient market-data failure here (same
                    # causes as the top-of-loop fetch) means only that this
                    # tick's trade is abandoned — nothing has been submitted
                    # yet, so it's as safe to treat as a plain Veto. Once
                    # provider.invoke() is reached, a real order may be in
                    # flight; a bare RuntimeError from there (e.g. a
                    # ledger.settle() consistency check) must NOT be caught
                    # here as if it were a routine skip.
                    fresh = market.snapshot(needed)
                except (PPIError, RuntimeError) as e:
                    order = {"status": "VETO", "reason": str(e)}
                    fresh = None
                if fresh is not None:
                    try:
                        latest = fresh[product]
                        if abs(latest.bid - q.bid) / q.bid > D(settings.slippage):
                            raise Veto("Price moved beyond neural observation tolerance")
                        provider.quotes = fresh
                        order = provider.invoke({"product": product, "side": neural["side"]})
                    except Veto as e:
                        order = {"status": "VETO", "reason": str(e)}
            row = {
                "tick": ledger.get("tick"),
                "wall_time": time.time(),
                "product": product,
                "mode": broker.mode,
                "quote": q.json(),
                "equity_ars": str(equity),
                "pnl_delta_ars": str(delta),
                "neural": neural,
                "execution": order,
            }
            with (out / "events.jsonl").open("a") as f:
                f.write(json.dumps(row, allow_nan=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            Image.fromarray(frame).save(out / "latest-input.png")
            (out / "latest.json").write_text(json.dumps(row, indent=2) + "\n")
            print(
                json.dumps(
                    {
                        "tick": row["tick"],
                        "side": neural["side"],
                        "execution": order["status"],
                        "equity": str(equity),
                        "stimulus": kind,
                        "plastic_edges_changed": neural["memory"]["changed_edges"],
                    }
                ),
                flush=True,
            )
            count += 1
            if not a.fast and (not a.steps or count < a.steps):
                until = started + settings.interval_seconds
                while time.monotonic() < until and not (out / "STOP").exists():
                    time.sleep(min(1, until - time.monotonic()))
    except KeyboardInterrupt:
        print("Stopped; run state preserved.", flush=True)
    except Exception as e:
        # Never print SDK exception text: it may contain account/request details.
        if not ledger.get("halted"):
            ledger.halt(type(e).__name__)
        frames = traceback.extract_tb(e.__traceback__)
        origin = frames[-1] if frames else None
        internal = origin and Path(origin.filename).is_relative_to(
            Path(__file__).parent
        )
        diagnostic = {
            "type": type(e).__name__,
            "reason": str(e)
            if internal
            else "External dependency error; review connection and account state.",
            "locations": [
                f"{Path(f.filename).name}:{f.lineno} {f.name}" for f in frames
            ],
        }
        (out / "error.json").write_text(json.dumps(diagnostic, indent=2) + "\n")
        print(
            f"Stopped safely: {type(e).__name__}. Inspect local state and reconcile before restarting.",
            file=sys.stderr,
        )
        if a.debug:
            print(
                "--debug: real exception below; it may include account/request "
                "details, so treat this terminal output as sensitive.",
                file=sys.stderr,
            )
            traceback.print_exception(type(e), e, e.__traceback__, file=sys.stderr)
        raise SystemExit(1) from None
    finally:
        ledger.close()
        lock.close()


if __name__ == "__main__":
    main()
