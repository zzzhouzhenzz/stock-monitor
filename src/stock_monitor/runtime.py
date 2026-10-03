"""Configuration, alert state, and delivery for the stock monitor."""
import hashlib
import json
import math
import os
import platform
import subprocess
import tempfile
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .rules import Quote, RuleConfig, VolumeObservation


def decimal(value, name):
    if value is None or isinstance(value, bool):
        raise ValueError(f"Set {name} to a numeric value before running")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"Invalid {name}") from exc
    if not result.is_finite() or result < 0:
        raise ValueError(f"{name} must be finite and nonnegative")
    return result


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("Timestamps must be ISO 8601 strings")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("Timestamps must have an explicit timezone")
    return result.astimezone(timezone.utc)


def read_json(path):
    with Path(path).open(encoding="utf-8") as stream:
        result = json.load(stream, parse_float=Decimal)
    if not isinstance(result, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return result


def load_config(path):
    path = Path(path).resolve()
    raw = read_json(path)
    if raw.get("source") not in ("normalized_file", "robinhood"):
        raise ValueError("source must be normalized_file or robinhood")
    if raw.get("notification") not in ("console", "desktop", "ntfy"):
        raise ValueError("notification must be console, desktop, or ntfy")
    if raw["notification"] == "ntfy":
        from .notifications import validate_ntfy_config
        validate_ntfy_config(raw)
    for key in ("poll_seconds", "cooldown_seconds"):
        value = raw[key]
        if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
            raise ValueError(f"{key} must be numeric")
        value = float(value)
        if not math.isfinite(value) or value < (1 if key == "poll_seconds" else 0):
            raise ValueError(f"Invalid {key}")
        raw[key] = value
    alerts = raw.get("alerts", [raw])
    if not isinstance(alerts, list) or not alerts or any(not isinstance(a, dict) for a in alerts):
        raise ValueError("alerts must be a nonempty list of price conditions")
    rules = []
    for alert in alerts:
        above = alert.get("price_above")
        below = alert.get("price_below")
        if above is not None and below is not None:
            raise ValueError("Set exactly one of price_above or price_below per alert")
        rule = RuleConfig(
            symbol=raw["symbol"],
            price_below=decimal(below, "price_below") if above is None else None,
            price_above=decimal(above, "price_above") if above is not None else None,
            volume_mode=raw["volume_mode"],
            volume_threshold=decimal(raw["volume_threshold"], "volume_threshold"),
            max_quote_age_seconds=raw.get("max_quote_age_seconds", 90),
            max_volume_age_seconds=raw.get("max_volume_age_seconds", 360),
            lookback_sessions=raw.get("lookback_sessions", 20),
            min_baseline_sessions=raw.get("min_baseline_sessions", 5),
        )
        if rule in rules:
            raise ValueError("Duplicate alert conditions")
        rules.append(rule)
    raw["rules"] = tuple(rules)
    keys = ["state_path"]
    if raw["source"] == "normalized_file":
        keys.append("snapshot_path")
    else:
        raw.setdefault("credentials_path", ".state/robinhood.json")
        keys.append("credentials_path")
        if raw["poll_seconds"] < 30:
            raise ValueError("Robinhood poll_seconds must be at least 30")
    for key in keys:
        raw[key] = path.parent / raw[key]
    if len({raw[key].resolve() for key in keys}) != len(keys):
        raise ValueError("Source, credentials, and state paths must be different files")
    # New rules get their own alert state; delivery/state paths do not alter the rule.
    raw["rule_id"] = rule_id(rules[0])
    return raw, rules[0]


def rule_id(rule):
    return hashlib.sha256(repr(rule).encode()).hexdigest()


def parse_snapshot(raw):
    def volume(item):
        return VolumeObservation(
            symbol=item["symbol"],
            volume=decimal(item["volume"], "volume"),
            timestamp=timestamp(item["timestamp"]),
            session=date.fromisoformat(item["session"]),
            bar_start=timestamp(item["bar_start"]) if item.get("bar_start") else None,
            bar_end=timestamp(item["bar_end"]) if item.get("bar_end") else None,
        )

    q = raw.get("quote")
    quote = Quote(
        symbol=q["symbol"], price=decimal(q["price"], "price"),
        timestamp=timestamp(q["timestamp"]), session=date.fromisoformat(q["session"]),
    ) if q else None
    observation = volume(raw["volume"]) if raw.get("volume") else None
    baseline = tuple(volume(item) for item in raw.get("baseline_bars", []))
    return quote, observation, baseline


class AlertState:
    """Persist delivered episodes; invalid observations never rearm an alert."""

    def __init__(self, path):
        self.path = Path(path)
        self.data = read_json(self.path) if self.path.exists() else {}
        self._dirty = False
        for entry in self.data.values():
            if not isinstance(entry, dict) or type(entry.get("active")) is not bool:
                raise ValueError("Alert state entries require a boolean active field")
            timestamp(entry.get("last_sent"))

    def process(self, rule_id, session, evaluation, now, cooldown_seconds, deliver):
        if self._dirty:
            self.save()  # Retry failed persistence before any further transition.
        if not evaluation.eligible:
            return "ineligible"
        key = f"{rule_id}:{session.isoformat()}"
        previous = self.data.get(key, {})
        if not evaluation.matched:
            if previous.get("active"):
                self.data[key] = dict(previous, active=False)
                self._dirty = True
                self.save()
            return "clear"
        if previous.get("active"):
            return "already_alerted"
        last = previous.get("last_sent")
        if last and (now - timestamp(last)).total_seconds() < cooldown_seconds:
            return "cooldown"
        deliver()  # Failed delivery must remain retryable.
        self.data[key] = {"active": True, "last_sent": now.isoformat()}
        self._dirty = True
        self.save()
        return "alerted"

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=".alerts-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(self.data, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            self._dirty = False
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def notify(channel, title, message, config=None):
    if channel == "console":
        print(f"ALERT: {title}\n{message}", flush=True)
    elif channel == "desktop" and platform.system() == "Darwin":
        script = ('on run argv\n'
                  'display notification (item 2 of argv) with title (item 1 of argv)\n'
                  'end run')
        subprocess.run(["osascript", "-e", script, title, message], check=True, timeout=15)
    elif channel == "desktop" and platform.system() == "Linux":
        subprocess.run(["notify-send", title, message], check=True, timeout=15)
    elif channel == "ntfy":
        from .notifications import send_ntfy
        send_ntfy(config or {}, title, message)
    else:
        raise ValueError(f"Unsupported notification channel/platform: {channel}")
