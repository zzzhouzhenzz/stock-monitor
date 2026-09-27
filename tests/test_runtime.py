import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from stock_monitor.runtime import AlertState, load_config, notify, parse_snapshot
from stock_monitor.cli import tick


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.now = datetime(2026, 9, 25, 15, 0, tzinfo=timezone.utc)
        self.session = date(2026, 9, 25)
        self.state_path = self.root / "state.json"
        self.delivered = []

    def process(self, state, matched=True, eligible=True, offset=0):
        result = SimpleNamespace(eligible=eligible, matched=matched)
        return state.process("rule", self.session, result,
                             self.now + timedelta(seconds=offset), 60,
                             lambda: self.delivered.append("alert"))

    def test_both_conditions_only_and_restart_persistence(self):
        state = AlertState(self.state_path)
        self.assertEqual(self.process(state, matched=False), "clear")
        self.assertEqual(self.process(state), "alerted")
        self.assertEqual(self.process(AlertState(self.state_path)), "already_alerted")
        self.assertEqual(self.delivered, ["alert"])

    def test_missing_data_does_not_rearm(self):
        state = AlertState(self.state_path)
        self.process(state)
        self.process(state, eligible=False, matched=False)
        self.assertEqual(self.process(state, offset=300), "already_alerted")

    def test_clear_rearms_but_cooldown_delays_delivery(self):
        state = AlertState(self.state_path)
        self.process(state)
        self.process(state, matched=False, offset=10)
        self.assertEqual(self.process(state, offset=30), "cooldown")
        self.assertEqual(self.process(state, offset=60), "alerted")
        self.assertEqual(len(self.delivered), 2)

    def test_failed_delivery_is_retryable(self):
        state = AlertState(self.state_path)
        def fail():
            raise OSError("delivery unavailable")
        with self.assertRaises(OSError):
            state.process("rule", self.session, SimpleNamespace(eligible=True, matched=True),
                          self.now, 60, fail)
        self.assertEqual(self.process(state), "alerted")

    def test_corrupt_state_is_not_silently_reset(self):
        self.state_path.write_text("not json")
        with self.assertRaises(ValueError):
            AlertState(self.state_path)

    def test_malformed_state_entries_are_rejected(self):
        for entry in ([1], {"active": "false", "last_sent": self.now.isoformat()},
                      {"active": False, "last_sent": None},
                      {"active": False, "last_sent": "2026-09-25T15:00:00"}):
            with self.subTest(entry=entry):
                self.state_path.write_text(json.dumps({"rule:2026-09-25": entry}))
                with self.assertRaises(ValueError):
                    AlertState(self.state_path)

    def test_failed_persistence_retries_without_redelivering(self):
        state = AlertState(self.state_path)
        with patch.object(state, "save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.process(state)
        self.assertEqual(self.process(state), "already_alerted")
        self.assertEqual(self.process(AlertState(self.state_path)), "already_alerted")
        self.assertEqual(self.delivered, ["alert"])

    def test_malformed_timestamp_reports_value_error(self):
        for value in (None, 42):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_snapshot({"quote": {"symbol": "META", "price": "599",
                                          "timestamp": value, "session": "2026-09-25"}})

    def test_unconfigured_example_cannot_run(self):
        path = Path(__file__).resolve().parents[1] / "config.example.json"
        with self.assertRaisesRegex(ValueError, "price_below"):
            load_config(path)

    def test_timestamps_normalized_to_utc(self):
        quote, _, _ = parse_snapshot({"quote": {
            "symbol": "META", "price": "599", "timestamp": "2026-09-25T11:00:00-04:00",
            "session": "2026-09-25"}})
        self.assertEqual(quote.timestamp, self.now)
        self.assertEqual(quote.timestamp.tzinfo, timezone.utc)

    def test_end_to_end_snapshot_and_staleness(self):
        raw_config = json.loads((Path(__file__).resolve().parents[1] / "config.example.json").read_text())
        raw_config.update(price_below="600", volume_threshold="1000000")
        config_path = self.root / "config.json"
        config_path.write_text(json.dumps(raw_config))
        config, rule = load_config(config_path)
        snapshot = {
            "market_open": True,
            "quote": {"symbol": "META", "price": "599", "timestamp": self.now.isoformat(), "session": "2026-09-25"},
            "volume": {"symbol": "META", "volume": "1100000", "timestamp": self.now.isoformat(), "session": "2026-09-25"}
        }
        config["snapshot_path"].write_text(json.dumps(snapshot))
        state = AlertState(config["state_path"])
        with patch("stock_monitor.cli.notify") as delivery:
            self.assertEqual(tick(config, rule, state, self.now), "alerted")
            self.assertEqual(tick(config, rule, AlertState(config["state_path"]), self.now), "already_alerted")
            self.assertTrue(tick(config, rule, state, self.now + timedelta(minutes=10)).startswith("ineligible:"))
            self.assertEqual(delivery.call_count, 1)
        snapshot["market_open"] = False
        config["snapshot_path"].write_text(json.dumps(snapshot))
        self.assertEqual(tick(config, rule, state, self.now), "market_closed")

    def test_desktop_arguments_are_not_shell_interpolated(self):
        with patch("stock_monitor.runtime.platform.system", return_value="Darwin"), \
             patch("stock_monitor.runtime.subprocess.run") as run:
            notify("desktop", 'title"', 'body $(echo bad)')
            self.assertEqual(run.call_args.args[0][-2:], ['title"', 'body $(echo bad)'])
            self.assertTrue(run.call_args.kwargs["check"])


if __name__ == "__main__":
    unittest.main()
