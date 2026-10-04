import copy
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import importlib.util
import unittest
from zoneinfo import ZoneInfo

from stock_monitor.robinhood_source import RobinhoodSource
from stock_monitor.rules import RuleConfig, evaluate
from stock_monitor.runtime import parse_snapshot


UTC = timezone.utc
NY = ZoneInfo("America/New_York")
DAY = date(2026, 9, 28)
OPEN = datetime(2026, 9, 28, 13, 30, tzinfo=UTC)
NOW = OPEN + timedelta(minutes=10, seconds=30)
BAR = timedelta(minutes=5)


def rule(mode="bar_absolute", **changes):
    arguments = dict(symbol="META", price_below=Decimal("100"), volume_mode=mode,
                     volume_threshold=Decimal("200"), lookback_sessions=2, min_baseline_sessions=1)
    arguments.update(changes)
    return RuleConfig(**arguments)


def session(day, close_hour=16):
    return {
        "market_open": datetime.combine(day, time(9, 30), NY).astimezone(UTC),
        "market_close": datetime.combine(day, time(close_hour), NY).astimezone(UTC),
    }


class FakeSchedule:
    def __init__(self, rows):
        self.rows = rows

    def iterrows(self):
        return iter(self.rows.items())


class FakeCalendar:
    def __init__(self, rows=None):
        self.rows = rows if rows is not None else {DAY: session(DAY)}
        self.calls = []

    def schedule(self, start_date, end_date):
        self.calls.append((start_date, end_date))
        return FakeSchedule({key: value for key, value in self.rows.items()
                             if start_date <= key <= end_date})


def quote_payload(at=NOW, **changes):
    quote = dict(symbol="META", last_trade_price="99", venue_last_trade_time=at.isoformat(),
                 last_non_reg_trade_price="1000", venue_last_non_reg_trade_time=at.isoformat(),
                 state="active", has_traded=True)
    quote.update(changes)
    return {"data": {"results": [{"quote": quote}]}}


def bar(start, volume=200, **changes):
    value = dict(begins_at=start.isoformat(), volume=volume, session="reg", interpolated=False,
                 open_price="99", high_price="99", low_price="99", close_price="99")
    value.update(changes)
    return value


def history_payload(bars, **changes):
    value = dict(symbol="META", interval="5minute", bounds="regular", bars=bars)
    value.update(changes)
    return {"data": {"results": [value]}}


class FakeTools:
    def __init__(self, quotes=None, histories=None):
        self.quotes = quotes if quotes is not None else quote_payload()
        self.histories = histories if histories is not None else {
            DAY: history_payload([bar(OPEN), bar(OPEN + BAR)])}
        self.calls = []

    async def __call__(self, name, arguments):
        self.calls.append((name, copy.deepcopy(arguments)))
        if name == "get_equity_quotes":
            return copy.deepcopy(self.quotes)
        if name != "get_equity_historicals":
            raise AssertionError(f"Unexpected tool: {name}")
        day = datetime.fromisoformat(arguments["start_time"]).astimezone(NY).date()
        result = self.histories.get(day, {"data": {"results": []}})
        if isinstance(result, list):
            result = result.pop(0) if len(result) > 1 else result[0]
        if isinstance(result, Exception):
            raise result
        return copy.deepcopy(result)

    def count(self, name):
        return sum(tool == name for tool, _ in self.calls)


class SourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_regular_quote_timestamp_and_explicit_historical_arguments(self):
        tools = FakeTools(quotes=quote_payload(venue_last_trade_time="2026-09-28T13:40:29.976587896Z"))
        source = RobinhoodSource(rule(), tools, FakeCalendar())
        snapshot = await source.fetch(NOW)
        self.assertEqual(snapshot["quote"]["price"], "99")
        self.assertEqual(snapshot["quote"]["timestamp"], "2026-09-28T13:40:29.976587+00:00")
        self.assertEqual(snapshot["volume"]["timestamp"], "2026-09-28T13:40:00+00:00")
        self.assertEqual(snapshot["market_close"], "2026-09-28T20:00:00+00:00")
        self.assertEqual(tools.calls[0], ("get_equity_quotes", {"symbols": ["META"]}))
        self.assertEqual(tools.calls[1], ("get_equity_historicals", {
            "symbols": ["META"], "start_time": OPEN.isoformat(),
            "end_time": (OPEN + 2 * BAR).isoformat(), "interval": "5minute",
            "bounds": "regular", "adjustment_type": "none"}))
        q, v, baseline = parse_snapshot(snapshot)
        self.assertTrue(evaluate(source.rule, q, v, NOW, baseline).matched)

    async def test_holiday_weekend_before_open_and_at_close_make_no_tool_calls(self):
        for at in [OPEN - timedelta(seconds=1), OPEN + timedelta(hours=6, minutes=30),
                   OPEN + timedelta(days=5), datetime(2026, 11, 26, 16, tzinfo=UTC)]:
            with self.subTest(at=at):
                tools = FakeTools()
                snapshot = await RobinhoodSource(rule(), tools, FakeCalendar()).fetch(at)
                self.assertFalse(snapshot["market_open"])
                self.assertEqual(tools.calls, [])

    async def test_early_close_uses_calendar_boundaries(self):
        day = date(2026, 11, 27)
        calendar = FakeCalendar({day: session(day, close_hour=13)})
        tools = FakeTools()
        source = RobinhoodSource(rule(), tools, calendar)
        close = calendar.rows[day]["market_close"]
        self.assertTrue(source.is_market_open(close - timedelta(seconds=1)))
        self.assertFalse(source.is_market_open(close))
        self.assertFalse((await source.fetch(close))["market_open"])
        self.assertEqual(tools.calls, [])

    async def test_session_close_and_next_open_use_inclusive_open_exclusive_close(self):
        tomorrow = DAY + timedelta(days=1)
        calendar = FakeCalendar({day: session(day) for day in [DAY, tomorrow]})
        tools = FakeTools()
        source = RobinhoodSource(rule(), tools, calendar)
        close = calendar.rows[DAY]["market_close"]
        self.assertIsNone(source.session_close(OPEN - timedelta(seconds=1)))
        self.assertEqual(source.next_open(OPEN - timedelta(seconds=1)), OPEN)
        self.assertEqual(source.session_close(OPEN), close)
        self.assertEqual(source.next_open(OPEN), OPEN)
        self.assertEqual(source.next_open(NOW.astimezone(NY)), NOW)
        self.assertIsNone(source.session_close(close))
        self.assertEqual(source.next_open(close), calendar.rows[tomorrow]["market_open"])
        self.assertEqual(len(calendar.calls), 1)
        self.assertEqual(calendar.calls[0][1], DAY + timedelta(days=90))
        self.assertEqual(tools.calls, [])

    async def test_no_future_session_in_horizon_has_clear_bounded_failure(self):
        calendar = FakeCalendar({})
        tools = FakeTools()
        source = RobinhoodSource(rule(), tools, calendar)
        with self.assertRaisesRegex(RuntimeError, "NASDAQ calendar has no future opening within 90 days"):
            source.next_open(NOW)
        self.assertIsNone(source.session_close(NOW))
        self.assertEqual(len(calendar.calls), 1)
        self.assertEqual(tools.calls, [])

    async def test_open_inclusive_but_first_volume_waits_for_completed_bar(self):
        tools = FakeTools(quotes=quote_payload(OPEN))
        source = RobinhoodSource(rule(), tools, FakeCalendar())
        for at in [OPEN, OPEN + BAR - timedelta(microseconds=1)]:
            snapshot = await source.fetch(at)
            self.assertTrue(snapshot["market_open"])
            self.assertIsNone(snapshot["volume"])
        self.assertEqual(tools.count("get_equity_quotes"), 2)
        self.assertEqual(tools.count("get_equity_historicals"), 0)

    async def test_incomplete_interpolated_nonregular_and_out_of_session_bars_are_filtered(self):
        rows = [bar(OPEN - BAR), bar(OPEN), bar(OPEN + BAR, interpolated=True),
                bar(OPEN + BAR, session="post"), bar(OPEN + 2 * BAR), None]
        tools = FakeTools(histories={DAY: history_payload(rows)})
        snapshot = await RobinhoodSource(rule(), tools, FakeCalendar()).fetch(NOW)
        # The expected latest bar is absent; an older valid bar is insufficient.
        self.assertIsNone(snapshot["volume"])

    async def test_daily_sum_has_completed_bar_as_of_time(self):
        tools = FakeTools(histories={DAY: history_payload([bar(OPEN, 125), bar(OPEN + BAR, 175),
                                                         bar(OPEN + 2 * BAR, 999)])})
        snapshot = await RobinhoodSource(rule("daily"), tools, FakeCalendar()).fetch(NOW)
        self.assertEqual(snapshot["volume"]["volume"], "300")
        self.assertEqual(snapshot["volume"]["timestamp"], (OPEN + 2 * BAR).isoformat())
        self.assertNotIn("bar_start", snapshot["volume"])

    async def test_daily_missing_or_interpolated_earlier_bar_does_not_undercount(self):
        for first in [[], [bar(OPEN, 0, interpolated=True)], [bar(OPEN, session="pre")]]:
            with self.subTest(first=first):
                tools = FakeTools(histories={DAY: history_payload(first + [bar(OPEN + BAR)])})
                snapshot = await RobinhoodSource(rule("daily"), tools, FakeCalendar()).fetch(NOW)
                self.assertIsNone(snapshot["volume"])

    async def test_quotes_each_fetch_bars_once_per_boundary(self):
        tools = FakeTools(histories={DAY: history_payload([bar(OPEN + BAR), bar(OPEN + 2 * BAR)])})
        calendar = FakeCalendar()
        source = RobinhoodSource(rule(), tools, calendar)
        await source.fetch(NOW)
        await source.fetch(NOW + timedelta(seconds=30))
        await source.fetch(NOW + BAR)
        self.assertEqual(tools.count("get_equity_quotes"), 3)
        self.assertEqual(tools.count("get_equity_historicals"), 2)
        self.assertEqual(len(calendar.calls), 1)

    async def test_missing_latest_retries_after_brief_lag(self):
        tools = FakeTools(histories={DAY: [history_payload([bar(OPEN)]),
                                          history_payload([bar(OPEN), bar(OPEN + BAR)])]})
        source = RobinhoodSource(rule(), tools, FakeCalendar())
        boundary = OPEN + 2 * BAR
        self.assertIsNone((await source.fetch(boundary))["volume"])
        self.assertIsNone((await source.fetch(boundary + timedelta(seconds=10)))["volume"])
        self.assertIsNotNone((await source.fetch(boundary + timedelta(seconds=20)))["volume"])
        await source.fetch(boundary + timedelta(seconds=30))
        self.assertEqual(tools.count("get_equity_historicals"), 2)

    async def test_two_minute_polls_recover_delayed_bar_without_reusing_it_next_interval(self):
        tools = FakeTools(histories={DAY: [history_payload([]), history_payload([]),
                                          history_payload([bar(OPEN + BAR)])]})
        source = RobinhoodSource(rule(), tools, FakeCalendar())
        boundary = OPEN + 2 * BAR
        self.assertIsNone((await source.fetch(boundary))["volume"])
        self.assertIsNone((await source.fetch(boundary + timedelta(seconds=120)))["volume"])
        recovered = await source.fetch(boundary + timedelta(seconds=240))
        self.assertEqual(recovered["volume"]["bar_end"], boundary.isoformat())
        self.assertIsNone((await source.fetch(boundary + timedelta(seconds=360)))["volume"])
        self.assertEqual(tools.count("get_equity_historicals"), 4)
        self.assertEqual(tools.calls[-1][1]["end_time"], (boundary + BAR).isoformat())

    async def test_same_new_york_clock_baselines_across_dst_and_cached_once_per_day(self):
        current, previous = date(2026, 3, 9), date(2026, 3, 6)
        calendar = FakeCalendar({current: session(current), previous: session(previous)})
        start = datetime(2026, 3, 9, 14, tzinfo=UTC)  # 10:00 EDT
        old_start = datetime(2026, 3, 6, 15, tzinfo=UTC)  # 10:00 EST
        now = start + BAR + timedelta(seconds=10)
        tools = FakeTools(quotes=quote_payload(now), histories={
            current: history_payload([bar(start, 200)]),
            previous: history_payload([bar(old_start, 100), bar(old_start + BAR, 1000),
                                       bar(old_start + 2 * BAR, 0, interpolated=True)]),
        })
        source = RobinhoodSource(rule("bar_relative", volume_threshold=Decimal(2)), tools, calendar)
        snapshot = await source.fetch(now)
        q, v, baselines = parse_snapshot(snapshot)
        result = evaluate(source.rule, q, v, now, baselines)
        self.assertTrue(result.matched)
        self.assertEqual(result.volume_ratio, Decimal(2))
        self.assertEqual(len(baselines), 2)
        self.assertEqual(baselines[0].timestamp, old_start + BAR)
        await source.fetch(now + timedelta(seconds=30))
        self.assertEqual(tools.count("get_equity_quotes"), 2)
        self.assertEqual(tools.count("get_equity_historicals"), 2)
        historic_args = tools.calls[2][1]
        self.assertEqual(historic_args["start_time"], calendar.rows[previous]["market_open"].isoformat())
        self.assertEqual(historic_args["end_time"], calendar.rows[previous]["market_close"].isoformat())

    async def test_prior_sessions_use_separate_windows_and_honor_lookback(self):
        days = [DAY - timedelta(days=n) for n in (3, 4, 5)]
        calendar = FakeCalendar({day: session(day) for day in [DAY, DAY + timedelta(days=1)] + days})
        tools = FakeTools()
        source = RobinhoodSource(rule("bar_relative"), tools, calendar)
        await source.fetch(NOW)
        historical_days = [datetime.fromisoformat(args["start_time"]).date()
                           for name, args in tools.calls if name == "get_equity_historicals"]
        self.assertEqual(historical_days, [DAY, days[0], days[1]])

    async def test_successful_prior_windows_survive_transient_failure(self):
        recent, older = DAY - timedelta(days=3), DAY - timedelta(days=4)
        calendar = FakeCalendar({day: session(day) for day in [DAY, recent, older]})
        tools = FakeTools(histories={DAY: history_payload([bar(OPEN + BAR)]),
                                    recent: history_payload([]),
                                    older: [OSError("temporary transport error"), history_payload([])]})
        source = RobinhoodSource(rule("bar_relative"), tools, calendar)
        with self.assertRaises(OSError):
            await source.fetch(NOW)
        await source.fetch(NOW + timedelta(seconds=30))
        days = [datetime.fromisoformat(args["start_time"]).date()
                for name, args in tools.calls if name == "get_equity_historicals"]
        self.assertEqual(days, [DAY, recent, older, older])

    async def test_caches_reset_when_new_york_session_changes(self):
        tomorrow = DAY + timedelta(days=1)
        calendar = FakeCalendar({day: session(day) for day in [DAY, tomorrow]})
        tools = FakeTools()
        source = RobinhoodSource(rule(), tools, calendar)
        await source.fetch(NOW)
        await source.fetch(NOW + timedelta(days=1))
        self.assertEqual(tools.count("get_equity_historicals"), 2)
        self.assertEqual(len(calendar.calls), 2)

    async def test_inactive_untraded_or_wrong_symbol_quote_is_rejected(self):
        for changes in [dict(state="inactive"), dict(has_traded=False), dict(has_traded="true"),
                        dict(symbol="AAPL")]:
            with self.subTest(changes=changes):
                tools = FakeTools(quotes=quote_payload(**changes))
                with self.assertRaises(ValueError):
                    await RobinhoodSource(rule(), tools, FakeCalendar()).fetch(NOW)
                self.assertEqual(tools.count("get_equity_historicals"), 0)

    async def test_missing_quote_is_preserved_without_extra_requests(self):
        for payload in [{"data": {"results": None}}, {"data": {"results": [None, {"quote": None}]}}]:
            tools = FakeTools(quotes=payload)
            snapshot = await RobinhoodSource(rule(), tools, FakeCalendar()).fetch(NOW)
            self.assertIsNone(snapshot["quote"])
            self.assertIsNone(snapshot["volume"])
            self.assertEqual(tools.count("get_equity_historicals"), 0)

    async def test_stale_or_future_quote_timestamp_is_preserved_for_evaluator(self):
        for offset, expected in [(-100, "stale_quote"), (10, "future_quote")]:
            with self.subTest(offset=offset):
                tools = FakeTools(quotes=quote_payload(NOW + timedelta(seconds=offset)))
                config = rule()
                snapshot = await RobinhoodSource(config, tools, FakeCalendar()).fetch(NOW)
                q, v, baseline = parse_snapshot(snapshot)
                self.assertEqual(evaluate(config, q, v, NOW, baseline).reason, expected)

    async def test_missing_historical_data_is_ineligible(self):
        for payload in [{"data": {"results": None}}, history_payload(None), history_payload([])]:
            tools = FakeTools(histories={DAY: payload})
            snapshot = await RobinhoodSource(rule(), tools, FakeCalendar()).fetch(NOW)
            self.assertIsNone(snapshot["volume"])

    async def test_malformed_quote_payloads_are_rejected(self):
        cases = [None, {"data": []}, {"data": {}}, {"data": {"results": {}}},
                 {"data": {"results": [123]}}, quote_payload(venue_last_trade_time=None),
                 quote_payload(venue_last_trade_time="2026-09-28T13:40:00"),
                 quote_payload(last_trade_price="NaN"), quote_payload(last_trade_price="0")]
        for payload in cases:
            with self.subTest(payload=payload):
                tools = FakeTools()
                tools.quotes = payload
                with self.assertRaises(ValueError):
                    await RobinhoodSource(rule(), tools, FakeCalendar()).fetch(NOW)

    async def test_malformed_historical_payloads_are_rejected(self):
        cases = [history_payload({}, symbol="META"), history_payload([], symbol="AAPL"),
                 history_payload([], bounds="extended"), history_payload([], interval="hour"),
                 history_payload([bar(OPEN, interpolated="false")]),
                 history_payload([bar(OPEN, volume=-1)]), history_payload([bar(OPEN, volume=1.5)]),
                 history_payload([bar(OPEN, volume="NaN")]), history_payload([bar(OPEN), bar(OPEN)]),
                 history_payload([bar(OPEN + timedelta(seconds=1))])]
        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    await RobinhoodSource(rule(), FakeTools(histories={DAY: payload}), FakeCalendar()).fetch(NOW)

    async def test_requires_aware_now_and_supported_bar_interval(self):
        source = RobinhoodSource(rule(), FakeTools(), FakeCalendar())
        with self.assertRaises(ValueError):
            await source.fetch(NOW.replace(tzinfo=None))
        with self.assertRaises(ValueError):
            RobinhoodSource(rule(bar_minutes=10), FakeTools(), FakeCalendar())

    @unittest.skipUnless(importlib.util.find_spec("pandas_market_calendars"), "optional calendar is not installed")
    async def test_real_nasdaq_calendar_holiday_and_early_close(self):
        tools = FakeTools()
        source = RobinhoodSource(rule(), tools)
        holiday = datetime(2026, 11, 26, 16, tzinfo=UTC)
        close = datetime(2026, 11, 27, 18, tzinfo=UTC)
        self.assertFalse((await source.fetch(holiday))["market_open"])
        self.assertTrue(source.is_market_open(close - timedelta(seconds=1)))
        self.assertFalse((await source.fetch(close))["market_open"])
        self.assertEqual(tools.calls, [])

    @unittest.skipUnless(importlib.util.find_spec("pandas_market_calendars"), "optional calendar is not installed")
    async def test_real_calendar_next_open_handles_weekend_holiday_early_close_and_dst(self):
        tools = FakeTools()
        source = RobinhoodSource(rule(), tools)
        cases = [
            # Friday close to Monday; both are on daylight saving time.
            (datetime(2026, 10, 2, 20, tzinfo=UTC), datetime(2026, 10, 5, 13, 30, tzinfo=UTC)),
            # Thanksgiving to the shortened Friday session.
            (datetime(2026, 11, 26, 16, tzinfo=UTC), datetime(2026, 11, 27, 14, 30, tzinfo=UTC)),
            # Friday's 13:00 New York close to Monday.
            (datetime(2026, 11, 27, 18, tzinfo=UTC), datetime(2026, 11, 30, 14, 30, tzinfo=UTC)),
            # Spring DST transition: Monday's opening moves one hour earlier in UTC.
            (datetime(2026, 3, 6, 21, tzinfo=UTC), datetime(2026, 3, 9, 13, 30, tzinfo=UTC)),
            # Fall DST transition: Monday's opening moves one hour later in UTC.
            (datetime(2026, 10, 30, 20, tzinfo=UTC), datetime(2026, 11, 2, 14, 30, tzinfo=UTC)),
        ]
        for now, expected in cases:
            with self.subTest(now=now):
                self.assertIsNone(source.session_close(now))
                self.assertEqual(source.next_open(now), expected)
                self.assertEqual(source.next_open(now).utcoffset(), timedelta(0))
        early_session = datetime(2026, 11, 27, 16, tzinfo=UTC)
        self.assertEqual(source.session_close(early_session), datetime(2026, 11, 27, 18, tzinfo=UTC))
        self.assertEqual(tools.calls, [])


if __name__ == "__main__":
    unittest.main()
