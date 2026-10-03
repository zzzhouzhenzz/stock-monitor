import copy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from stock_monitor.cli import main, poll, process_snapshot, run, run_robinhood
from stock_monitor.runtime import AlertState, load_config, rule_id


UTC = timezone.utc
NOW = datetime(2026, 9, 25, 15, tzinfo=UTC)
PRIOR_SESSIONS = [date(2026, 9, day) for day in (18, 21, 22, 23, 24)]
HAS_LIVE_DEPS = (importlib.util.find_spec("mcp") is not None
                 and importlib.util.find_spec("httpx2") is not None)


def volume_bar(end, shares):
    return {
        "symbol": "META", "volume": str(shares), "timestamp": end.isoformat(),
        "session": end.date().isoformat(),
        "bar_start": (end - timedelta(minutes=5)).isoformat(), "bar_end": end.isoformat(),
    }


def snapshot(price="788", shares=200, at=NOW, bar_end=NOW):
    return {
        "market_open": True,
        "market_close": NOW.replace(hour=20).isoformat(),
        "quote": {"symbol": "META", "price": price, "timestamp": at.isoformat(),
                  "session": at.date().isoformat()},
        "volume": volume_bar(bar_end, shares),
        "baseline_bars": [volume_bar(datetime.combine(day, NOW.time(), UTC), 100)
                          for day in PRIOR_SESSIONS],
    }


class ConfigFixture:
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.config_path = self.root / "config.json"
        self.raw_config = {
            "symbol": "META", "alerts": [{"price_above": "760"}, {"price_above": "787"}],
            "volume_mode": "bar_relative", "volume_threshold": "2",
            "lookback_sessions": 20, "min_baseline_sessions": 5,
            "poll_seconds": 30, "cooldown_seconds": 120,
            "source": "normalized_file", "snapshot_path": "snapshot.json",
            "state_path": "state.json", "notification": "console",
        }
        self.config, self.rule = self.write_config()

    def write_config(self, changes=None):
        raw = copy.deepcopy(self.raw_config)
        if changes:
            raw.update(changes)
        self.config_path.write_text(json.dumps(raw))
        return load_config(self.config_path)

    def state(self):
        return AlertState(self.config["state_path"])


class SnapshotTests(ConfigFixture, unittest.TestCase):
    def test_both_price_conditions_are_strict_and_require_twice_the_volume(self):
        cases = [
            ("760", 200, []), ("760.01", 200, ["META > $760"]),
            ("787", 200, ["META > $760"]), ("787.01", 200, ["META > $760", "META > $787"]),
            ("800", 199, []), ("759", 300, []),
        ]
        for index, (price, shares, titles) in enumerate(cases):
            with self.subTest(price=price, shares=shares):
                state = AlertState(self.root / f"case-{index}.json")
                with patch("stock_monitor.cli.notify") as notify:
                    process_snapshot(self.config, self.rule, state, NOW, snapshot(price, shares))
                self.assertEqual([call.args[1] for call in notify.call_args_list], titles)
                for call in notify.call_args_list:
                    self.assertIn("relative volume=2.00x", call.args[2])

    def test_independent_episodes_persist_and_rearm_only_the_cleared_rule(self):
        state = self.state()
        with patch("stock_monitor.cli.notify") as notify:
            outcomes = []
            for price, offset in [("761", 0), ("788", 30), ("788", 60),
                                  ("780", 90), ("788", 100), ("788", 150)]:
                if offset == 60:
                    state = self.state()  # Restart after both alerts have been delivered.
                at = NOW + timedelta(seconds=offset)
                outcomes.append(process_snapshot(self.config, self.rule, state, at, snapshot(price, at=at)))
            self.assertEqual(outcomes, [
                ">760: alerted; >787: clear",
                ">760: already_alerted; >787: alerted",
                ">760: already_alerted; >787: already_alerted",
                ">760: already_alerted; >787: clear",
                ">760: already_alerted; >787: cooldown",
                ">760: already_alerted; >787: alerted",
            ])
            self.assertEqual([call.args[1] for call in notify.call_args_list],
                             ["META > $760", "META > $787", "META > $787"])
        persisted = json.loads(self.config["state_path"].read_text())
        expected_keys = {f"{rule_id(current)}:2026-09-25" for current in self.config["rules"]}
        self.assertEqual(set(persisted), expected_keys)
        self.assertTrue(all(entry["active"] for entry in persisted.values()))

    def test_insufficient_baseline_does_not_deliver_or_rearm_either_rule(self):
        state = self.state()
        with patch("stock_monitor.cli.notify") as notify:
            process_snapshot(self.config, self.rule, state, NOW, snapshot())
            invalid = snapshot("700")
            invalid["baseline_bars"] = invalid["baseline_bars"][:4]
            result = process_snapshot(self.config, self.rule, state, NOW, invalid)
            self.assertEqual(result.count("ineligible: insufficient_baseline"), 2)
            result = process_snapshot(self.config, self.rule, state, NOW, snapshot())
            self.assertEqual(result.count("already_alerted"), 2)
            self.assertEqual(notify.call_count, 2)

    def test_delivery_failure_for_second_rule_preserves_first_rules_episode(self):
        with patch("stock_monitor.cli.notify", side_effect=[None, OSError("delivery failed")]):
            with self.assertRaises(OSError):
                process_snapshot(self.config, self.rule, self.state(), NOW, snapshot())
        with patch("stock_monitor.cli.notify") as notify:
            outcome = process_snapshot(self.config, self.rule, self.state(), NOW, snapshot())
            self.assertEqual(outcome, ">760: already_alerted; >787: alerted")
            self.assertEqual(notify.call_count, 1)
            self.assertEqual(notify.call_args.args[1], "META > $787")

    def test_dry_run_matches_without_delivery_or_state_creation(self):
        with patch("stock_monitor.cli.notify") as notify:
            outcome = process_snapshot(self.config, self.rule, None, NOW, snapshot(), dry_run=True)
        self.assertEqual(outcome, ">760: dry-run: matched; >787: dry-run: matched")
        self.assertFalse(self.config["state_path"].exists())
        notify.assert_not_called()

    def test_dry_run_nonmatch_does_not_rearm_existing_episodes(self):
        state = self.state()
        with patch("stock_monitor.cli.notify"):
            process_snapshot(self.config, self.rule, state, NOW, snapshot())
        before = self.config["state_path"].read_bytes()
        with patch("stock_monitor.cli.notify") as notify:
            process_snapshot(self.config, self.rule, state, NOW, snapshot("700"), dry_run=True)
        self.assertEqual(self.config["state_path"].read_bytes(), before)
        self.assertTrue(all(entry["active"] for entry in state.data.values()))
        notify.assert_not_called()

    def test_session_close_takes_precedence_over_open_flag(self):
        raw = snapshot()
        raw["market_close"] = NOW.isoformat()
        with patch("stock_monitor.cli.notify") as notify:
            result = process_snapshot(self.config, self.rule, self.state(), NOW, raw)
        self.assertEqual(result, "market_closed")
        notify.assert_not_called()
        self.assertFalse(self.config["state_path"].exists())

    def test_previous_bar_is_rejected_after_boundary_even_with_fresh_quote(self):
        after_boundary = NOW + timedelta(minutes=5, seconds=1)
        raw = snapshot(at=after_boundary, bar_end=NOW)
        with patch("stock_monitor.cli.notify") as notify:
            outcome = process_snapshot(self.config, self.rule, self.state(), after_boundary, raw)
        self.assertIn("not_latest_completed_bar", outcome)
        notify.assert_not_called()
        self.assertFalse(self.config["state_path"].exists())

    def test_run_dry_run_does_not_load_or_change_existing_alert_state(self):
        self.config["state_path"].write_text("invalid state deliberately unread in dry-run")
        before = self.config["state_path"].read_bytes()
        self.config["snapshot_path"].write_text(json.dumps(snapshot()))
        with patch("stock_monitor.cli.datetime") as clock, patch("stock_monitor.cli.notify") as notify, \
                patch("stock_monitor.cli.AlertState") as state_constructor, patch("sys.stdout", new=io.StringIO()):
            clock.now.return_value = NOW
            self.assertEqual(run(self.config_path, once=True, dry_run=True), 0)
        state_constructor.assert_not_called()
        notify.assert_not_called()
        self.assertEqual(self.config["state_path"].read_bytes(), before)


class ConfigurationTests(ConfigFixture, unittest.TestCase):
    def test_loads_two_above_rules_and_distinct_state_ids(self):
        self.assertEqual([current.price_above for current in self.config["rules"]],
                         [Decimal("760"), Decimal("787")])
        self.assertTrue(all(current.price_below is None for current in self.config["rules"]))
        self.assertEqual(len({rule_id(current) for current in self.config["rules"]}), 2)

    def test_duplicate_conditions_with_different_numeric_spelling_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            self.write_config({"alerts": [{"price_above": 760}, {"price_above": "760.0"}]})

    def test_both_or_missing_price_directions_are_rejected(self):
        for alerts in [[{"price_above": 760, "price_below": 787}], [{}],
                       [{"price_above": None}], [{"price_above": True}], [], {}, None, [760]]:
            with self.subTest(alerts=alerts), self.assertRaises(ValueError):
                self.write_config({"alerts": alerts})

    def test_live_config_needs_no_snapshot_and_resolves_credentials(self):
        raw = copy.deepcopy(self.raw_config)
        raw.update(source="robinhood", credentials_path="secrets/robinhood.json")
        raw.pop("snapshot_path")
        self.config_path.write_text(json.dumps(raw))
        config, _ = load_config(self.config_path)
        self.assertNotIn("snapshot_path", config)
        self.assertEqual(config["credentials_path"], self.root / "secrets/robinhood.json")

    def test_live_poll_floor_and_path_collisions_are_rejected(self):
        for changes in [dict(source="robinhood", poll_seconds=29),
                        dict(source="robinhood", credentials_path="state.json"),
                        dict(snapshot_path="state.json")]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.write_config(changes)

    def test_login_rejects_file_source_before_any_network_action(self):
        with patch("sys.stderr", new=io.StringIO()) as errors:
            self.assertEqual(main(["login", "--config", str(self.config_path)]), 1)
        self.assertIn("login requires source=robinhood", errors.getvalue())

    def test_main_reports_invalid_config_without_traceback(self):
        self.raw_config["alerts"] = [{"price_above": 760, "price_below": 787}]
        self.config_path.write_text(json.dumps(self.raw_config))
        with patch("sys.stderr", new=io.StringIO()) as errors:
            self.assertEqual(main(["run", "--config", str(self.config_path), "--once"]), 1)
        self.assertIn("Set exactly one", errors.getvalue())
        self.assertNotIn("Traceback", errors.getvalue())


class PollTests(ConfigFixture, unittest.IsolatedAsyncioTestCase):
    async def test_slow_fetch_age_is_checked_after_io(self):
        after_fetch = NOW + timedelta(seconds=91)
        source = SimpleNamespace(fetch=AsyncMock(return_value=snapshot()), is_market_open=Mock(return_value=True))
        with patch("stock_monitor.cli.datetime") as clock, patch("stock_monitor.cli.notify") as notify, \
                patch("sys.stdout", new=io.StringIO()) as output:
            clock.now.side_effect = [NOW, after_fetch]
            result = await poll(self.config, self.rule, self.state(), once=True, source=source)
        self.assertEqual(result, 0)
        self.assertEqual(output.getvalue().count("ineligible: stale_quote"), 2)
        source.fetch.assert_awaited_once_with(NOW)
        source.is_market_open.assert_called_once_with(after_fetch)
        notify.assert_not_called()
        self.assertFalse(self.config["state_path"].exists())

    async def test_fresh_response_after_latency_evaluates_both_rules_from_one_fetch(self):
        response_time = NOW + timedelta(seconds=10)
        source = SimpleNamespace(fetch=AsyncMock(return_value=snapshot(at=response_time)),
                                 is_market_open=Mock(return_value=True))
        with patch("stock_monitor.cli.datetime") as clock, patch("stock_monitor.cli.notify") as notify, \
                patch("sys.stdout", new=io.StringIO()):
            clock.now.side_effect = [NOW, response_time + timedelta(seconds=1)]
            result = await poll(self.config, self.rule, self.state(), once=True, source=source)
        self.assertEqual(result, 0)
        source.fetch.assert_awaited_once()
        self.assertEqual(notify.call_count, 2)

    async def test_market_closing_during_fetch_prevents_delivery(self):
        before = NOW.replace(hour=19, minute=59, second=59)
        after = NOW.replace(hour=20, second=1)
        source = SimpleNamespace(fetch=AsyncMock(return_value=snapshot(at=before)),
                                 is_market_open=Mock(return_value=False))
        with patch("stock_monitor.cli.datetime") as clock, patch("stock_monitor.cli.notify") as notify, \
                patch("sys.stdout", new=io.StringIO()) as output:
            clock.now.side_effect = [before, after]
            result = await poll(self.config, self.rule, self.state(), once=True, source=source)
        self.assertEqual(result, 0)
        self.assertIn("market_closed", output.getvalue())
        source.is_market_open.assert_called_once_with(after)
        notify.assert_not_called()

    async def test_invalid_file_snapshot_returns_failure_without_state_or_delivery(self):
        malformed = snapshot()
        malformed["quote"]["timestamp"] = None
        self.config["snapshot_path"].write_text(json.dumps(malformed))
        with patch("stock_monitor.cli.datetime") as clock, patch("stock_monitor.cli.notify") as notify, \
                patch("sys.stderr", new=io.StringIO()) as errors:
            clock.now.return_value = NOW
            result = await poll(self.config, self.rule, self.state(), once=True)
        self.assertEqual(result, 1)
        self.assertIn("Timestamps must be ISO 8601 strings", errors.getvalue())
        self.assertFalse(self.config["state_path"].exists())
        notify.assert_not_called()

    @unittest.skipUnless(HAS_LIVE_DEPS, "optional Robinhood client dependencies are not installed")
    async def test_live_transport_failure_propagates_to_reconnection_boundary(self):
        from stock_monitor.robinhood_client import RobinhoodError
        source = SimpleNamespace(fetch=AsyncMock(side_effect=RobinhoodError("transport unavailable")))
        with self.assertRaises(RobinhoodError):
            await poll(self.config, self.rule, self.state(), once=False, source=source)
        self.assertFalse(self.config["state_path"].exists())

    @unittest.skipUnless(HAS_LIVE_DEPS, "optional Robinhood client dependencies are not installed")
    async def test_run_robinhood_reconnects_transport_and_keeps_same_alert_state(self):
        from stock_monitor.robinhood_client import RobinhoodError
        self.config["credentials_path"] = self.root / "credentials.json"
        state = self.state()
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        with patch("stock_monitor.robinhood_client.RobinhoodClient", return_value=client) as constructor, \
                patch("stock_monitor.cli.poll", new=AsyncMock(side_effect=[RobinhoodError("disconnected"), 0])) as polling, \
                patch("stock_monitor.cli.asyncio.sleep", new=AsyncMock()) as sleep, \
                patch("sys.stderr", new=io.StringIO()):
            result = await run_robinhood(self.config, self.rule, state, once=False, dry_run=False)
        self.assertEqual(result, 0)
        self.assertEqual(constructor.call_count, 2)
        self.assertTrue(all(call.args[2] is state for call in polling.await_args_list))
        sleep.assert_awaited_once_with(60)

    @unittest.skipUnless(HAS_LIVE_DEPS, "optional Robinhood client dependencies are not installed")
    async def test_auth_required_stops_without_retrying_or_launching_login(self):
        from stock_monitor.robinhood_client import AuthRequired
        self.config["credentials_path"] = self.root / "credentials.json"
        client = MagicMock()
        client.__aenter__ = AsyncMock(side_effect=AuthRequired("run login"))
        client.__aexit__ = AsyncMock(return_value=False)
        with patch("stock_monitor.robinhood_client.RobinhoodClient", return_value=client), \
                patch("stock_monitor.cli.asyncio.sleep", new=AsyncMock()) as sleep:
            with self.assertRaises(AuthRequired):
                await run_robinhood(self.config, self.rule, self.state(), once=False, dry_run=False)
        sleep.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
