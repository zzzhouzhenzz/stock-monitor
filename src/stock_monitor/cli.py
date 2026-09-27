import argparse
import fcntl
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from .rules import Quote, RuleConfig, VolumeObservation, evaluate
from .runtime import AlertState, load_config, notify, parse_snapshot, read_json


def tick(config, rule, state, now):
    raw = read_json(config["snapshot_path"])
    if not isinstance(raw.get("market_open"), bool):
        raise ValueError("Snapshot requires boolean market_open from a trading calendar")
    if not raw["market_open"]:
        return "market_closed"
    quote, volume, baseline = parse_snapshot(raw)
    result = evaluate(rule, quote, volume, now, baseline_bars=baseline)
    if not result.eligible:
        return "ineligible: " + result.reason
    message = (
        f"{rule.symbol} ${quote.price} < ${rule.price_below}; "
        f"{rule.volume_mode} volume={volume.volume}; "
        f"threshold={rule.volume_threshold}; quote time={quote.timestamp.isoformat()}; "
        f"volume time={volume.timestamp.isoformat()}"
    )
    if result.volume_ratio is not None:
        message += f"; relative volume={result.volume_ratio:.2f}x"
    return state.process(
        config["rule_id"], quote.session, result, now, config["cooldown_seconds"],
        lambda: notify(config["notification"], f"{rule.symbol}: price + volume", message),
    )


def run(config_path, once):
    config, rule = load_config(config_path)
    config["state_path"].parent.mkdir(parents=True, exist_ok=True)
    lock_path = config["state_path"].with_suffix(".lock")
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Another monitor is using this state file") from exc
        state = AlertState(config["state_path"])
        failures = 0
        print("Source: normalized local snapshot. Robinhood is NOT connected.", flush=True)
        while True:
            started = time.monotonic()
            try:
                result = tick(config, rule, state, datetime.now(timezone.utc))
                failures = 0
                print(f"{datetime.now(timezone.utc).isoformat()} {result}", flush=True)
            except (OSError, ValueError, TypeError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
                failures += 1
                print(f"Monitor error ({type(exc).__name__}): {exc}", file=sys.stderr, flush=True)
                if once:
                    return 1
            if once:
                return 0
            delay = min(300, config["poll_seconds"] * (2 ** min(failures, 4)))
            time.sleep(max(0, delay - (time.monotonic() - started)))


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
    parser = argparse.ArgumentParser(description="Portable price-and-volume monitor prototype")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("demo", help="Run synthetic observations; no network or desktop alerts")
    validate = commands.add_parser("validate", help="Validate local rule configuration")
    validate.add_argument("--config", required=True)
    runner = commands.add_parser("run", help="Poll a normalized local snapshot; no broker adapter yet")
    runner.add_argument("--config", required=True)
    runner.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "demo":
            return demo()
        if args.command == "validate":
            load_config(args.config)
            print("Configuration valid. Live broker connectivity has not been verified.")
            return 0
        return run(args.config, args.once)
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(f"Error ({type(exc).__name__}): {exc}", file=sys.stderr)
        return 1
