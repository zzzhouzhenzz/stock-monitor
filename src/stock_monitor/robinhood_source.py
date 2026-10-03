"""Read-only Robinhood market data mapped to the monitor's snapshot format."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import re

from .rules import EXCHANGE_TIMEZONE, Quote, VolumeObservation


BAR = timedelta(minutes=5)
LAG_RETRY = timedelta(seconds=15)


def _utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Expected an aware datetime")
    return value.astimezone(timezone.utc)


def _timestamp(value):
    if not isinstance(value, str):
        raise ValueError("Robinhood timestamps must be ISO 8601 strings")
    # Venue times can have nanoseconds. Python 3.9 accepts at most microseconds.
    value = re.sub(r"(\.\d{6})\d+(?=Z$|[+-]\d{2}:\d{2}$)", r"\1", value)
    return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


def _number(value):
    if isinstance(value, bool) or value is None:
        raise ValueError("Robinhood price and volume must be numeric")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("Robinhood price and volume must be numeric") from exc
    if not result.is_finite() or result < 0:
        raise ValueError("Robinhood price and volume must be finite and nonnegative")
    return result


def _results(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise ValueError("Robinhood response requires a data object")
    data = payload["data"]
    if "results" not in data:
        raise ValueError("Robinhood response is missing results")
    if data["results"] is None:
        return []
    if not isinstance(data["results"], list):
        raise ValueError("Robinhood results must be a list")
    if any(item is not None and not isinstance(item, dict) for item in data["results"]):
        raise ValueError("Robinhood result entries must be objects")
    return [item for item in data["results"] if item is not None]


def _volume_dict(value):
    result = {
        "symbol": value.symbol,
        "volume": str(value.volume),
        "timestamp": value.timestamp.isoformat(),
        "session": value.session.isoformat(),
    }
    if value.bar_start is not None:
        result.update(bar_start=value.bar_start.isoformat(), bar_end=value.bar_end.isoformat())
    return result


@dataclass(frozen=True)
class _Session:
    opens: datetime
    closes: datetime


class RobinhoodSource:
    """Fetch regular-session quotes and completed five-minute share-volume bars.

    ``call_tool(name, arguments)`` is an async function that returns the unwrapped
    tool payload. The injected calendar has pandas_market_calendars' ``schedule``
    API. Authentication and retrying transport errors belong to the caller.
    """

    def __init__(self, rule, call_tool, calendar=None):
        if rule.bar_minutes != 5:
            raise ValueError("Robinhood source currently requires five-minute bars")
        self.rule = rule
        self.call_tool = call_tool
        self.calendar = calendar
        self._day = None
        self._sessions = {}
        self._target_end = None
        self._bars = {}
        self._last_bar_fetch = None
        self._baseline = {}

    def _prepare_day(self, now):
        day = now.astimezone(EXCHANGE_TIMEZONE).date()
        if day == self._day:
            return day
        if self.calendar is None:
            try:
                import pandas_market_calendars as mcal
            except ImportError as exc:
                raise RuntimeError("Robinhood source requires pandas-market-calendars") from exc
            self.calendar = mcal.get_calendar("NASDAQ")
        start = day - timedelta(days=max(45, self.rule.lookback_sessions * 3 + 14))
        schedule = self.calendar.schedule(start_date=start, end_date=day)
        sessions = {}
        for _, row in schedule.iterrows():
            opens, closes = _utc(row["market_open"]), _utc(row["market_close"])
            session_day = opens.astimezone(EXCHANGE_TIMEZONE).date()
            if opens >= closes or closes.astimezone(EXCHANGE_TIMEZONE).date() != session_day:
                raise ValueError("Calendar returned invalid regular-session boundaries")
            sessions[session_day] = _Session(opens, closes)
        self._day, self._sessions = day, sessions
        self._target_end, self._last_bar_fetch = None, None
        self._bars, self._baseline = {}, {}
        return day

    def is_market_open(self, now):
        """Recheck this after a slow fetch before delivering an alert."""
        now = _utc(now)
        day = self._prepare_day(now)
        session = self._sessions.get(day)
        return session is not None and session.opens <= now < session.closes

    def _quote(self, payload):
        quotes = []
        for item in _results(payload):
            raw = item.get("quote")
            if raw is None:
                continue
            if not isinstance(raw, dict) or raw.get("symbol") != self.rule.symbol:
                raise ValueError("Robinhood quote has an unexpected symbol or shape")
            if raw.get("state") != "active" or raw.get("has_traded") is not True:
                raise ValueError("Robinhood quote is not active or has not traded")
            stamp = _timestamp(raw["venue_last_trade_time"])
            quotes.append(Quote(self.rule.symbol, _number(raw["last_trade_price"]), stamp,
                                stamp.astimezone(EXCHANGE_TIMEZONE).date()))
        if len(quotes) > 1:
            raise ValueError("Robinhood returned duplicate quotes for the symbol")
        if not quotes:
            return None
        quote = quotes[0]
        return {"symbol": quote.symbol, "price": str(quote.price),
                "timestamp": quote.timestamp.isoformat(), "session": quote.session.isoformat()}

    def _parse_bars(self, payload, session, end):
        results = _results(payload)
        if len(results) > 1:
            raise ValueError("Robinhood returned duplicate historical results")
        bars = {}
        for result in results:
            if result.get("symbol") != self.rule.symbol:
                raise ValueError("Robinhood historicals have an unexpected symbol")
            if result.get("interval") != "5minute" or result.get("bounds") != "regular":
                raise ValueError("Robinhood historicals must use regular five-minute bars")
            rows = result.get("bars")
            if rows is None:
                continue
            if not isinstance(rows, list):
                raise ValueError("Robinhood bars must be a list")
            for raw in rows:
                if raw is None:
                    continue
                if not isinstance(raw, dict):
                    raise ValueError("Robinhood bars must contain objects")
                interpolated = raw.get("interpolated", False)
                if type(interpolated) is not bool:
                    raise ValueError("Robinhood interpolated flag must be boolean")
                if interpolated or raw.get("session") != "reg":
                    continue
                start = _timestamp(raw["begins_at"])
                bar_end = start + BAR
                if start < session.opens or bar_end > min(end, session.closes):
                    continue
                if (start - session.opens) % BAR:
                    raise ValueError("Robinhood returned a misaligned five-minute bar")
                if start in bars:
                    raise ValueError("Robinhood returned duplicate bar timestamps")
                shares = _number(raw["volume"])
                if shares != shares.to_integral_value():
                    raise ValueError("Robinhood bar volume must be whole shares")
                value = VolumeObservation(self.rule.symbol, shares, bar_end,
                                          start.astimezone(EXCHANGE_TIMEZONE).date(), start, bar_end)
                bars[start] = _volume_dict(value)
        return bars

    async def _historicals(self, session, end):
        payload = await self.call_tool("get_equity_historicals", {
            "symbols": [self.rule.symbol], "start_time": session.opens.isoformat(),
            "end_time": end.isoformat(), "interval": "5minute", "bounds": "regular",
            "adjustment_type": "none",
        })
        return self._parse_bars(payload, session, end)

    async def fetch(self, now):
        now = _utc(now)
        day = self._prepare_day(now)
        session = self._sessions.get(day)
        snapshot = {"market_open": False, "quote": None, "volume": None, "baseline_bars": []}
        if session is None or not session.opens <= now < session.closes:
            return snapshot
        snapshot.update(market_open=True, market_close=session.closes.isoformat())
        snapshot["quote"] = self._quote(await self.call_tool(
            "get_equity_quotes", {"symbols": [self.rule.symbol]}))
        if snapshot["quote"] is None:
            return snapshot
        target = session.opens + ((now - session.opens) // BAR) * BAR
        if target == session.opens:
            return snapshot
        new_boundary = target != self._target_end
        retry_missing = (
            target - BAR not in self._bars
            and (self._last_bar_fetch is None or now - self._last_bar_fetch >= LAG_RETRY)
        )
        if new_boundary or retry_missing:
            self._bars = await self._historicals(session, target)
            self._target_end, self._last_bar_fetch = target, now
        latest = self._bars.get(target - BAR)
        if latest is None:
            return snapshot
        if self.rule.volume_mode == "daily":
            starts = [session.opens + index * BAR for index in range((target - session.opens) // BAR)]
            if any(start not in self._bars for start in starts):
                return snapshot
            shares = sum((Decimal(self._bars[start]["volume"]) for start in starts), Decimal(0))
            snapshot["volume"] = _volume_dict(VolumeObservation(self.rule.symbol, shares, target, day))
        else:
            snapshot["volume"] = latest
        if self.rule.volume_mode == "bar_relative":
            prior = sorted((stamp for stamp in self._sessions if stamp < day), reverse=True)
            for stamp in prior[:self.rule.lookback_sessions]:
                if stamp not in self._baseline:
                    previous = self._sessions[stamp]
                    self._baseline[stamp] = await self._historicals(previous, previous.closes)
            snapshot["baseline_bars"] = [bar for bars in self._baseline.values() for bar in bars.values()]
        return snapshot
