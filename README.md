# META price + volume monitor

Portable Python prototype for macOS and Linux. The rule is:

```
fresh META price < configured dollar threshold
AND
fresh volume >= configured absolute threshold or relative-volume multiple
```

**Status: local rule engine and normalized-snapshot runner only. No live Robinhood
adapter, authenticated quote test, installed background service, or phone delivery
exists yet. Nothing is monitoring your account or stock prices.**

## Run the demonstration

Python 3.9+ with system timezone data; no third-party runtime dependencies.
From this directory, on either Mac or Linux:

```sh
python3 monitor.py demo
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

The demo uses fictional prices/volumes and emits one console alert when both
conditions match. It does not contact a broker or send desktop notifications.

## Configure the rule

Copy `config.example.json` to `config.local.json`. Set `price_below` and
`volume_threshold`; `null` intentionally prevents starting an unconfigured rule.
Then validate with:

```sh
python3 monitor.py validate --config config.local.json
```

Volume modes:

| Mode | Meaning of `volume_threshold` |
| --- | --- |
| `daily` | Minimum cumulative regular-session shares today |
| `bar_absolute` | Minimum shares in the latest completed 5-minute bar |
| `bar_relative` | Minimum latest-bar volume divided by the mean for the same New York clock interval in previous sessions |

For relative volume, `2` means at least twice the baseline. This is an example,
not a selected user threshold. The baseline uses the newest 20 distinct eligible
prior sessions available, requires at least 5, and compares matching 5-minute
time slots. Configure both lookback and minimum to 20 if you require a full
20-session baseline. Duplicate sessions, missing/zero baselines, incomplete bars,
misaligned bars, wrong sessions and stale observations cannot trigger alerts.

**Daily volume is cumulative:** after it crosses the threshold it usually stays
above it for the day. Relative bar volume is useful for a recent activity burst.
The latter compares price now with the latest completed bar; volume confirmation
can lag a developing burst by up to a bar. A 30-second poll does not create
30-second volume bars and can miss short price crossings between polls.

Notifications are `console` (default) or `desktop`. macOS uses `osascript`;
Linux uses `notify-send` and requires a logged-in graphical session. A successful
command is not proof the user saw a notification; OS settings can suppress it.
Phone notifications still require the user's choice of destination/service.

## Local snapshot source

This contract is **our internal format, not Robinhood's response schema**. A future
broker adapter must normalize verified source fields into it. Do not substitute
the fetch time for the actual source timestamp to make old data appear fresh.

```json
{
  "market_open": true,
  "quote": {
    "symbol": "META",
    "price": "599.50",
    "timestamp": "2026-09-25T15:00:00Z",
    "session": "2026-09-25"
  },
  "volume": {
    "symbol": "META",
    "volume": "1200000",
    "timestamp": "2026-09-25T15:00:00Z",
    "session": "2026-09-25"
  },
  "baseline_bars": []
}
```

This example is synthetic and will be rejected as stale outside its timestamp.
`market_open` must come from an exchange calendar including holidays/early closes;
the file runner does not implement a market calendar. Only regular-session data
is intended. All session dates refer to `America/New_York`. Timestamp offsets are
normalized to UTC. Use decimal strings for prices and volume quantities.

For bar modes, add `bar_start` and `bar_end` to `volume`, both timestamp strings.
For relative volume, include historical observations with the same fields in
`baseline_bars`. The adapter must supply complete bars in a consistent session
and volume convention and refresh them promptly. It should replace snapshots
atomically. The latest quote must be within 90 seconds; volume observation and
completed-bar end must be within 360 seconds by default. Older data is ineligible.

Once a producer exists, the same command runs on both machines:

```sh
python3 monitor.py run --config config.local.json --once
python3 monitor.py run --config config.local.json
```

The runner polls at 30 seconds, with bounded error backoff. It logs unavailable,
invalid and stale data; it does not yet send separate outage notifications.
Relative paths resolve against the config file, not the shell's directory.

An alert fires on the first valid matching sample, including at startup. It does
not repeat while both conditions remain true. A valid non-matching sample rearms
it; invalid/closed-market samples do not. The default 15-minute cooldown delays
realerts, and a still-matching condition is delivered when the cooldown expires.
State is per rule and New York session and is written after successful delivery.
Restart deduplication is tested, but a crash between delivery and state persistence
can still duplicate an alert. This is not exactly-once delivery.

Only one process can use a state file at a time on a machine. Run one active
monitor across the two machines unless duplicate alerts are desired; this prototype
does not provide shared state or automatic failover. Keep local configuration,
credentials, logs and runtime state out of cross-machine code sync.

## Robinhood integration gate

Robinhood's [official tool catalog](https://robinhood.com/us/en/support/articles/trading-with-your-agent/)
documents `get_equity_quotes` for real-time quotes and prior close,
`get_equity_historicals` for OHLCV bars, and `get_equity_fundamentals` for today's
OHLCV. The [official connection guide](https://robinhood.com/us/en/support/articles/agentic-trading-overview/)
provides the MCP endpoint `https://agent.robinhood.com/mcp/trading`.

The public catalog does not establish exact JSON fields, supported bar intervals,
intraday historical depth, incomplete-bar semantics, timestamp freshness, numeric
poll limits or unattended token lifetime. These must be discovered and verified
with an authenticated read-only client. The documented quote call alone is not
enough for a price-plus-volume rule.

On 2026-09-27, an unauthenticated initialization probe reached the official
endpoint and returned HTTP 401 with an OAuth protected-resource metadata URL.
Connectivity is verified; authenticated tool discovery and market data are not.

Before live use:

1. Confirm the user's price threshold, volume definition/threshold and alert destination.
2. Authenticate the official MCP client and inspect the actual input/output schemas.
3. Verify quote and volume timestamps, regular-session semantics, and sufficient
   same-time historical bars if relative volume is selected.
4. Implement the small normalization adapter, permitting only required data tools.
5. Check 30-second polling, throttling/backoff, token refresh and a delivered test
   notification; then configure `launchd` on Mac or a user `systemd` service on Linux.

No OAuth credentials are copied from another application, and no broker orders
or account mutations are part of this project.
