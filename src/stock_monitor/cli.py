import argparse
import asyncio
import fcntl
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from .rules import Quote, RuleConfig, VolumeObservation, evaluate
from .runtime import AlertState, load_config, notify, parse_snapshot, read_json, rule_id, timestamp


def process_snapshot(config, rule, state, now, raw, dry_run=False):
    if not isinstance(raw.get("market_open"), bool):
        raise ValueError("Snapshot requires boolean market_open from a trading calendar")
    if not raw["market_open"] or (raw.get("market_close") and now >= timestamp(raw["market_close"])):
        return "market_closed"
    quote, volume, baseline = parse_snapshot(raw)
    if rule.volume_mode != "daily" and volume is not None:
        latest_end = now.replace(minute=now.minute - now.minute % rule.bar_minutes,
                                 second=0, microsecond=0)
        if volume.bar_end != latest_end:
            return "ineligible: not_latest_completed_bar"
    outcomes = []
    rules = config.get("rules", (rule,))
    for current in rules:
        result = evaluate(current, quote, volume, now, baseline_bars=baseline)
        operator = ">" if current.price_above is not None else "<"
        threshold = current.price_above if current.price_above is not None else current.price_below
        if not result.eligible:
            outcome = "ineligible: " + result.reason
        elif dry_run:
            outcome = "dry-run: " + ("matched" if result.matched else result.reason)
        else:
            message = (
                f"{current.symbol} ${quote.price} {operator} ${threshold}; "
                f"{current.volume_mode} volume={volume.volume}; "
                f"threshold={current.volume_threshold}; quote time={quote.timestamp.isoformat()}; "
                f"volume time={volume.timestamp.isoformat()}"
            )
            if result.volume_ratio is not None:
                message += f"; relative volume={result.volume_ratio:.2f}x"
            outcome = state.process(
                rule_id(current), quote.session, result, now, config["cooldown_seconds"],
                lambda: notify(config["notification"], f"{current.symbol} {operator} ${threshold}",
                               message, config),
            )
        outcomes.append(f"{operator}{threshold}: {outcome}" if len(rules) > 1 else outcome)
    return "; ".join(outcomes)


def tick(config, rule, state, now, dry_run=False):
    return process_snapshot(config, rule, state, now, read_json(config["snapshot_path"]), dry_run)


def _report_error(exc):
    print(f"Monitor error ({type(exc).__name__}): {exc}", file=sys.stderr, flush=True)


def _delay(config, failures):
    return min(300, config["poll_seconds"] * (2 ** min(failures, 4)))


async def poll(config, rule, state, once, dry_run=False, source=None):
    # Refresh the clock after I/O: response latency must count toward data age.
    failures = 0
    while True:
        started = time.monotonic()
        try:
            if source is None:
                raw = read_json(config["snapshot_path"])
            else:
                before_fetch = datetime.now(timezone.utc)
                if not source.is_market_open(before_fetch):
                    if once:
                        print(f"{before_fetch.isoformat()} market_closed", flush=True)
                        return 0
                    return None  # Let the caller close the connection before waiting.
                raw = await source.fetch(before_fetch)
            now = datetime.now(timezone.utc)
            if source is not None and not source.is_market_open(now):
                raw = {"market_open": False}
            result = process_snapshot(config, rule, state, now, raw, dry_run)
            failures = 0
            print(f"{now.isoformat()} {result}", flush=True)
        except (OSError, ValueError, TypeError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
            if source is not None:
                from .robinhood_client import RobinhoodError
                if isinstance(exc, RobinhoodError):
                    raise  # Reconnect failed transport, retaining persisted alert episodes.
            failures += 1
            _report_error(exc)
            if once:
                return 1
        if once:
            return 0
        delay = max(0, _delay(config, failures) - (time.monotonic() - started))
        if source is not None:
            now = datetime.now(timezone.utc)
            closes = source.session_close(now)
            if closes is None:
                return None
            delay = min(delay, (closes - now).total_seconds())
        await asyncio.sleep(delay)


async def run_robinhood(config, rule, state, once, dry_run):
    from .robinhood_client import AuthRequired, RobinhoodClient, RobinhoodError
    from .robinhood_source import RobinhoodSource
    failures = 0
    source = RobinhoodSource(rule, None)
    while True:
        now = datetime.now(timezone.utc)
        if not source.is_market_open(now):
            if once:
                print(f"{now.isoformat()} market_closed", flush=True)
                return 0
            opens = source.next_open(now)
            print(f"{now.isoformat()} market_closed; sleeping until {opens.isoformat()}", flush=True)
            await asyncio.sleep(max(0, (opens - datetime.now(timezone.utc)).total_seconds()))
            continue  # Recheck the calendar and wall clock before authenticating.
        # Stop connection setup and in-flight fetches at the session boundary.
        # The client context closes before the scheduler sleeps overnight.
        closes = source.session_close(now)
        deadline = asyncio.timeout((closes - now).total_seconds())
        try:
            async with deadline:
                async with RobinhoodClient(config["credentials_path"]) as client:
                    source.call_tool = client.call_tool
                    result = await poll(config, rule, state, once, dry_run, source)
            if result is not None:
                return result
            failures = 0
        except TimeoutError:
            if not deadline.expired():
                raise
            if once:
                print(f"{datetime.now(timezone.utc).isoformat()} market_closed", flush=True)
                return 0
            failures = 0
        except AuthRequired:
            raise
        except RobinhoodError as exc:
            _report_error(exc)
            if once:
                return 1
            failures += 1
            now = datetime.now(timezone.utc)
            closes = source.session_close(now)
            if closes is not None:
                await asyncio.sleep(min(_delay(config, failures), (closes - now).total_seconds()))


def run(config_path, once=False, dry_run=False):
    config, rule = load_config(config_path)
    config["state_path"].parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = config["state_path"].with_suffix(".lock")
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Another monitor is using this state file") from exc
        state = None if dry_run else AlertState(config["state_path"])
        print(f"Source: {config['source']}; alerts: {len(config['rules'])}; dry-run: {dry_run}", flush=True)
        if config["source"] == "robinhood":
            return asyncio.run(run_robinhood(config, rule, state, once, dry_run))
        return asyncio.run(poll(config, rule, state, once, dry_run))


def demo():
    print("SIMULATED DATA ONLY: $600 price threshold; 1,000,000-share daily volume threshold.")
    rule = RuleConfig(symbol="META", price_below=Decimal("600"), volume_mode="daily",
                      volume_threshold=Decimal("1000000"))
    now = datetime(2026, 9, 25, 15, 0, tzinfo=timezone.utc)
    session = date(2026, 9, 25)
    with tempfile.TemporaryDirectory(prefix="stock-monitor-demo-") as directory:
        state = AlertState(Path(directory) / "alerts.json")
        for index, (price, shares) in enumerate([
            ("599", "900000"), ("601", "1100000"),
            ("599", "1200000"), ("598", "1300000"),
        ]):
            at = now + timedelta(seconds=30 * index)
            quote = Quote("META", Decimal(price), at, session)
            volume = VolumeObservation("META", Decimal(shares), at, session)
            result = evaluate(rule, quote, volume, at)
            outcome = state.process("demo", session, result, at, 900,
                                    lambda: notify("console", "DEMO META", "Both conditions met"))
            print(f"price={price}, daily volume={shares}: {outcome}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Price-and-volume alerts without LLM calls")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("demo", help="Run synthetic observations without network access")
    for name, help_text in (
        ("validate", "Validate configuration without network access"),
        ("login", "Authorize this service through Robinhood in your browser"),
        ("notify-test", "Send a labeled test notification"),
        ("service", "Generate an OS service file without installing it"),
        ("run", "Poll market data and evaluate alert conditions"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--config", required=True)
        if name == "run":
            command.add_argument("--once", action="store_true")
            command.add_argument("--dry-run", action="store_true", help="Do not send alerts or update alert state")
        if name == "service":
            command.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "demo":
            return demo()
        config, _ = load_config(args.config)
        if args.command == "validate":
            print("Configuration valid. Broker connectivity and phone delivery have not been verified.")
        elif args.command == "login":
            if config["source"] != "robinhood":
                raise ValueError("login requires source=robinhood")
            from .robinhood_client import login
            print("Complete Robinhood login in your browser. Waiting up to five minutes.", flush=True)
            asyncio.run(login(config["credentials_path"]))
            print("Robinhood login and META quote request succeeded. No account data was requested.")
        elif args.command == "notify-test":
            notify(config["notification"], "Stock Monitor test", "Test message only. META monitoring is not confirmed active.", config)
            print("Notification accepted. Confirm receipt on your phone.")
        elif args.command == "service":
            from .service import render_service
            print(render_service(args.config, args.output_dir))
            print("Service file generated. It has not been installed or started.")
        else:
            return run(args.config, args.once, args.dry_run)
        return 0
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, ImportError) as exc:
        print(f"Error ({type(exc).__name__}): {exc}", file=sys.stderr)
        return 1
