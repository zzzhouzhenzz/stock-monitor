from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import unittest

from stock_monitor.rules import Quote, RuleConfig, VolumeObservation, evaluate


D = Decimal
UTC = timezone.utc
NOW = datetime(2026, 9, 25, 14, 5, 30, tzinfo=UTC)
SESSION = NOW.date()


def quote(price="99", timestamp=NOW, **kwargs):
    return Quote("META", D(price), timestamp, timestamp.date(), **kwargs)


def volume(shares="200", timestamp=NOW, **kwargs):
    return VolumeObservation("META", D(shares), timestamp, timestamp.date(), **kwargs)


def bar(day=25, shares="200", hour=14, minute=0, month=9):
    start = datetime(2026, month, day, hour, minute, tzinfo=UTC)
    end = start + timedelta(minutes=5)
    return volume(shares, end, bar_start=start, bar_end=end)


class RuleTests(unittest.TestCase):
    def setUp(self):
        self.daily = RuleConfig("META", D("100"), "daily", D("200"))
        self.relative = replace(self.daily, volume_mode="bar_relative", volume_threshold=D("2"),
                                min_baseline_sessions=2, lookback_sessions=3)
        self.baseline = [bar(24, "100"), bar(23, "100")]

    def evaluate(self, config=None, q=None, v=None, baselines=()):
        return evaluate(config or self.daily, q or quote(), v or volume(), NOW, baselines)

    def test_price_and_volume_must_both_match(self):
        for price, shares, matched in [("99", "200", True), ("101", "300", False),
                                       ("99", "199", False), ("101", "199", False)]:
            with self.subTest(price=price, shares=shares):
                result = self.evaluate(q=quote(price), v=volume(shares))
                self.assertTrue(result.eligible)
                self.assertEqual(result.matched, matched)

    def test_price_comparison_is_strict_and_decimal(self):
        self.assertFalse(self.evaluate(q=quote("100")).matched)
        self.assertTrue(self.evaluate(q=quote("99.99999999999999999999")).matched)

    def test_missing_observations(self):
        self.assertEqual(evaluate(self.daily, None, volume(), NOW).reason, "missing_quote")
        self.assertEqual(evaluate(self.daily, quote(), None, NOW).reason, "missing_volume")

    def test_stale_and_future_quote_or_volume(self):
        cases = [(quote(timestamp=NOW - timedelta(seconds=91)), volume(), "stale_quote"),
                 (quote(timestamp=NOW + timedelta(seconds=6)), volume(), "future_quote"),
                 (quote(), volume(timestamp=NOW - timedelta(seconds=361)), "stale_volume"),
                 (quote(), volume(timestamp=NOW + timedelta(seconds=6)), "future_volume")]
        for q, v, reason in cases:
            with self.subTest(reason=reason):
                result = self.evaluate(q=q, v=v)
                self.assertFalse(result.eligible)
                self.assertEqual(result.reason, reason)

    def test_freshness_boundaries_and_small_clock_skew(self):
        self.assertTrue(self.evaluate(q=quote(timestamp=NOW - timedelta(seconds=90)),
                                      v=volume(timestamp=NOW - timedelta(seconds=360))).matched)
        self.assertTrue(self.evaluate(q=quote(timestamp=NOW + timedelta(seconds=5))).matched)

    def test_symbol_and_session_must_match(self):
        self.assertEqual(self.evaluate(q=replace(quote(), symbol="AAPL")).reason, "symbol_mismatch")
        previous = NOW - timedelta(days=1)
        self.assertEqual(self.evaluate(v=volume(timestamp=previous)).reason, "session_mismatch")
        self.assertEqual(self.evaluate(q=quote(timestamp=previous), v=volume(timestamp=previous)).reason,
                         "not_current_session")

    def test_daily_cannot_accidentally_use_bar_volume(self):
        self.assertEqual(self.evaluate(v=bar()).reason, "unexpected_bar_data")

    def test_absolute_completed_bar(self):
        config = replace(self.daily, volume_mode="bar_absolute")
        self.assertTrue(self.evaluate(config, v=bar()).matched)
        self.assertFalse(self.evaluate(config, v=bar(shares="199")).matched)
        self.assertEqual(self.evaluate(config, v=volume()).reason, "missing_bar_boundaries")

    def test_partial_bar_rejected_even_with_fresh_quote(self):
        pending = bar(minute=5)
        pending = replace(pending, timestamp=NOW)
        self.assertEqual(self.evaluate(self.relative, v=pending).reason, "incomplete_bar")
        # An old captured partial bar is still partial after wall-clock completion.
        captured_partial = replace(bar(), timestamp=bar().bar_start + timedelta(minutes=2))
        self.assertEqual(self.evaluate(self.relative, v=captured_partial).reason, "incomplete_bar")

    def test_retimestamping_old_bar_does_not_make_it_fresh(self):
        old_bar = replace(bar(hour=13, minute=50), timestamp=NOW)
        self.assertEqual(self.evaluate(self.relative, v=old_bar).reason, "stale_bar")

    def test_wrong_duration_and_alignment_rejected(self):
        wrong_duration = replace(bar(), bar_start=bar().bar_start - timedelta(minutes=5))
        misaligned = bar(hour=13, minute=59)
        for value in [wrong_duration, misaligned]:
            self.assertEqual(self.evaluate(self.relative, v=value).reason, "invalid_bar_alignment")

    def test_relative_volume_uses_arithmetic_mean(self):
        result = self.evaluate(self.relative, v=bar(), baselines=[bar(24, "50"), bar(23, "150")])
        self.assertTrue(result.matched)
        self.assertEqual(result.volume_ratio, D("2"))

    def test_relative_volume_respects_price_and_ratio_thresholds(self):
        result = self.evaluate(self.relative, q=quote("100"), v=bar(), baselines=self.baseline)
        self.assertTrue(result.eligible)
        self.assertFalse(result.matched)
        self.assertEqual(result.volume_ratio, D("2"))
        self.assertEqual(self.evaluate(self.relative, v=bar(shares="199"),
                                       baselines=self.baseline).reason, "volume_below_threshold")

    def test_nearby_slots_same_day_and_other_symbols_do_not_count(self):
        candidates = [bar(), bar(minute=5), bar(24, hour=13, minute=55),
                      replace(bar(23), symbol="AAPL"), bar(26)]
        result = self.evaluate(self.relative, v=bar(), baselines=candidates)
        self.assertEqual(result.reason, "insufficient_baseline")

    def test_duplicate_sessions_rejected(self):
        result = self.evaluate(self.relative, v=bar(), baselines=[bar(24), bar(24), bar(23)])
        self.assertEqual(result.reason, "duplicate_baseline_session")

    def test_zero_baseline_rejected(self):
        result = self.evaluate(self.relative, v=bar(), baselines=[bar(24, "0"), bar(23, "0")])
        self.assertEqual(result.reason, "zero_baseline")

    def test_only_latest_available_lookback_sessions_used(self):
        config = replace(self.relative, lookback_sessions=2)
        result = self.evaluate(config, v=bar(), baselines=[bar(22, "10000"), *self.baseline])
        self.assertTrue(result.matched)
        self.assertEqual(result.volume_ratio, D("2"))

    def test_same_exchange_clock_slot_across_dst_transition(self):
        # March 6: 10:00 EST = 15:00 UTC. March 9: 10:00 EDT = 14:00 UTC.
        current = bar(9, month=3, hour=14)
        now = current.bar_end + timedelta(seconds=30)
        q = quote(timestamp=now)
        config = replace(self.relative, min_baseline_sessions=1)
        result = evaluate(config, q, current, now, [bar(6, "100", month=3, hour=15)])
        self.assertTrue(result.matched)
        wrong_utc_slot = evaluate(config, q, current, now, [bar(6, "100", month=3, hour=14)])
        self.assertEqual(wrong_utc_slot.reason, "insufficient_baseline")

    def test_incomplete_historical_bars_do_not_count(self):
        partial = replace(bar(24), timestamp=bar(24).bar_start)
        result = self.evaluate(self.relative, v=bar(), baselines=[partial, bar(23)])
        self.assertEqual(result.reason, "insufficient_baseline")


class ValidationTests(unittest.TestCase):
    def test_invalid_prices(self):
        for value in [D("0"), D("-1"), D("NaN"), D("Infinity"), 1.25, "99"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                Quote("META", value, NOW, SESSION)

    def test_invalid_volumes(self):
        for value in [D("-1"), D("NaN"), D("Infinity"), D("1.5"), 100]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                VolumeObservation("META", value, NOW, SESSION)
        self.assertEqual(volume("0").volume, D(0))

    def test_naive_or_non_utc_datetimes(self):
        for stamp in [NOW.replace(tzinfo=None), NOW.astimezone(timezone(timedelta(hours=-4)))]:
            with self.subTest(stamp=stamp), self.assertRaises(ValueError):
                quote(timestamp=stamp)
        with self.assertRaises(ValueError):
            evaluate(RuleConfig("META", D(100), "daily", D(200)), quote(), volume(),
                     NOW.replace(tzinfo=None))

    def test_wrong_session_label_and_partial_boundaries(self):
        with self.assertRaises(ValueError):
            replace(quote(), session=SESSION - timedelta(days=1))
        with self.assertRaises(ValueError):
            volume(bar_start=NOW)
        with self.assertRaises(ValueError):
            volume(bar_start=NOW, bar_end=NOW)
        with self.assertRaises(ValueError):
            volume(bar_start=NOW.replace(tzinfo=None), bar_end=NOW + timedelta(minutes=5))

    def test_session_uses_new_york_date_not_utc_date(self):
        stamp = datetime(2026, 9, 26, 0, 30, tzinfo=UTC)
        self.assertEqual(Quote("META", D(99), stamp, date(2026, 9, 25)).session, date(2026, 9, 25))
        with self.assertRaises(ValueError):
            Quote("META", D(99), stamp, date(2026, 9, 26))

    def test_invalid_config(self):
        valid = RuleConfig("META", D(100), "daily", D(200))
        invalid = [dict(symbol="meta"), dict(price_below=D(0)), dict(volume_mode="unknown"),
                   dict(volume_threshold=D("2.5")), dict(max_quote_age_seconds=0),
                   dict(max_volume_age_seconds=True), dict(future_tolerance_seconds=-1),
                   dict(min_baseline_sessions=21), dict(lookback_sessions=0), dict(bar_minutes=7)]
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(valid, **changes)


if __name__ == "__main__":
    unittest.main()
