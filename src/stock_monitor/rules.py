"""Pure price-and-volume rules for normalized US equity observations.

All input times are explicit aware UTC datetimes. ``session`` is the exchange's
America/New_York calendar date, and volume counts shares. Invalid normalized
objects raise ValueError; missing or unusable observations yield an ineligible
Evaluation. No wall clock, data provider, or notification dependency lives here.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
import re
from typing import Iterable, Optional
from zoneinfo import ZoneInfo


EXCHANGE_TIMEZONE = ZoneInfo("America/New_York")
VOLUME_MODES = frozenset(("daily", "bar_absolute", "bar_relative"))


def _symbol(value: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z][A-Z0-9.-]*", value):
        raise ValueError("symbol must be a nonempty uppercase equity symbol")


def _decimal(value: Decimal, name: str, *, zero_allowed: bool = False) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{name} must be a finite Decimal")
    if value < 0 or (value == 0 and not zero_allowed):
        raise ValueError(f"{name} must be {'nonnegative' if zero_allowed else 'positive'}")


def _utc(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be an aware UTC datetime")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be an aware UTC datetime")


def _session(value: date, timestamp: datetime, name: str) -> None:
    if type(value) is not date:
        raise ValueError(f"{name} must be a date")
    if value != timestamp.astimezone(EXCHANGE_TIMEZONE).date():
        raise ValueError(f"{name} must match the timestamp's New York date")


@dataclass(frozen=True)
class RuleConfig:
    symbol: str
    price_below: Decimal
    volume_mode: str
    volume_threshold: Decimal
    max_quote_age_seconds: int = 90
    max_volume_age_seconds: int = 360
    future_tolerance_seconds: int = 5
    lookback_sessions: int = 20
    min_baseline_sessions: int = 5
    bar_minutes: int = 5

    def __post_init__(self) -> None:
        _symbol(self.symbol)
        _decimal(self.price_below, "price_below")
        _decimal(self.volume_threshold, "volume_threshold")
        if self.volume_mode not in VOLUME_MODES:
            raise ValueError(f"volume_mode must be one of {sorted(VOLUME_MODES)}")
        if self.volume_mode != "bar_relative" and self.volume_threshold % 1:
            raise ValueError("absolute volume_threshold must be a whole share count")
        for name in ("max_quote_age_seconds", "max_volume_age_seconds", "lookback_sessions",
                     "min_baseline_sessions", "bar_minutes"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if type(self.future_tolerance_seconds) is not int or self.future_tolerance_seconds < 0:
            raise ValueError("future_tolerance_seconds must be a nonnegative integer")
        if self.min_baseline_sessions > self.lookback_sessions:
            raise ValueError("min_baseline_sessions cannot exceed lookback_sessions")
        if self.bar_minutes > 60 or 60 % self.bar_minutes:
            raise ValueError("bar_minutes must be a positive divisor of 60")


@dataclass(frozen=True)
class Quote:
    symbol: str
    price: Decimal
    timestamp: datetime
    session: date

    def __post_init__(self) -> None:
        _symbol(self.symbol)
        _decimal(self.price, "price")
        _utc(self.timestamp, "timestamp")
        _session(self.session, self.timestamp, "session")


@dataclass(frozen=True)
class VolumeObservation:
    symbol: str
    volume: Decimal
    timestamp: datetime
    session: date
    bar_start: Optional[datetime] = None
    bar_end: Optional[datetime] = None

    def __post_init__(self) -> None:
        _symbol(self.symbol)
        _decimal(self.volume, "volume", zero_allowed=True)
        if self.volume % 1:
            raise ValueError("volume must be a whole share count")
        _utc(self.timestamp, "timestamp")
        _session(self.session, self.timestamp, "session")
        if (self.bar_start is None) != (self.bar_end is None):
            raise ValueError("bar_start and bar_end must be supplied together")
        if self.bar_start is not None and self.bar_end is not None:
            _utc(self.bar_start, "bar_start")
            _utc(self.bar_end, "bar_end")
            if self.bar_start >= self.bar_end:
                raise ValueError("bar_start must precede bar_end")
            _session(self.session, self.bar_start, "session")
            _session(self.session, self.bar_end, "session")


@dataclass(frozen=True)
class Evaluation:
    eligible: bool
    matched: bool
    reason: str
    volume_ratio: Optional[Decimal] = None


def _bar_problem(config: RuleConfig, volume: VolumeObservation, now: datetime) -> Optional[str]:
    start, end = volume.bar_start, volume.bar_end
    if start is None or end is None:
        return "missing_bar_boundaries"
    if end > now or volume.timestamp < end:
        return "incomplete_bar"
    local_start = start.astimezone(EXCHANGE_TIMEZONE)
    if (end - start != timedelta(minutes=config.bar_minutes)
            or local_start.minute % config.bar_minutes
            or local_start.second or local_start.microsecond):
        return "invalid_bar_alignment"
    return None


def _slot(volume: VolumeObservation) -> tuple:
    """Compare exchange-local clock slots, preserving alignment across DST."""
    assert volume.bar_start is not None and volume.bar_end is not None
    start = volume.bar_start.astimezone(EXCHANGE_TIMEZONE)
    end = volume.bar_end.astimezone(EXCHANGE_TIMEZONE)
    return start.time(), end.time()


def evaluate(
    config: RuleConfig,
    quote: Optional[Quote],
    volume: Optional[VolumeObservation],
    now: datetime,
    baseline_bars: Iterable[VolumeObservation] = (),
) -> Evaluation:
    """Require price < threshold AND volume >= threshold.

    For relative volume, threshold is a multiplier of the arithmetic mean of
    matching completed bars in the latest ``lookback_sessions`` available prior
    distinct sessions. Other symbols, clock slots, and the current/future session
    do not count. Duplicate bars for a matching session are rejected. Calendar
    and market-hours eligibility belong to the caller.
    """
    _utc(now, "now")
    if quote is None:
        return Evaluation(False, False, "missing_quote")
    if volume is None:
        return Evaluation(False, False, "missing_volume")
    if quote.symbol != config.symbol or volume.symbol != config.symbol:
        return Evaluation(False, False, "symbol_mismatch")
    if quote.session != volume.session:
        return Evaluation(False, False, "session_mismatch")
    if quote.session != now.astimezone(EXCHANGE_TIMEZONE).date():
        return Evaluation(False, False, "not_current_session")
    for name, observation, max_age in (
        ("quote", quote, config.max_quote_age_seconds),
        ("volume", volume, config.max_volume_age_seconds),
    ):
        age = (now - observation.timestamp).total_seconds()
        if age < -config.future_tolerance_seconds:
            return Evaluation(False, False, f"future_{name}")
        if age > max_age:
            return Evaluation(False, False, f"stale_{name}")

    ratio = None
    if config.volume_mode == "daily":
        if volume.bar_start is not None:
            return Evaluation(False, False, "unexpected_bar_data")
        volume_matched = volume.volume >= config.volume_threshold
    else:
        problem = _bar_problem(config, volume, now)
        if problem:
            return Evaluation(False, False, problem)
        assert volume.bar_end is not None
        if (now - volume.bar_end).total_seconds() > config.max_volume_age_seconds:
            return Evaluation(False, False, "stale_bar")
        if config.volume_mode == "bar_absolute":
            volume_matched = volume.volume >= config.volume_threshold
        else:
            by_session = {}
            for bar in baseline_bars:
                if (bar.symbol != config.symbol or bar.session >= volume.session
                        or _bar_problem(config, bar, now) or _slot(bar) != _slot(volume)):
                    continue
                if bar.session in by_session:
                    return Evaluation(False, False, "duplicate_baseline_session")
                by_session[bar.session] = bar.volume
            sessions = sorted(by_session, reverse=True)[:config.lookback_sessions]
            if len(sessions) < config.min_baseline_sessions:
                return Evaluation(False, False, "insufficient_baseline")
            mean = sum((by_session[session] for session in sessions), Decimal(0)) / len(sessions)
            if mean == 0:
                return Evaluation(False, False, "zero_baseline")
            ratio = volume.volume / mean
            volume_matched = ratio >= config.volume_threshold

    price_matched = quote.price < config.price_below
    if not price_matched:
        return Evaluation(True, False, "price_not_below_threshold", ratio)
    if not volume_matched:
        return Evaluation(True, False, "volume_below_threshold", ratio)
    return Evaluation(True, True, "matched", ratio)
