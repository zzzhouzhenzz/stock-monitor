# META price and volume monitor

A local Python service for macOS and Linux. During regular trading sessions it
polls Robinhood market data every 2 minutes, evaluates fixed rules, and sends
iPhone notifications through ntfy.
The running service does not use an LLM or consume model tokens.

Explore the [interactive code hierarchy](docs/architecture.html) by opening
`docs/architecture.html` in a browser. Keep `docs/hierarchy-data.js` beside it.
The tree shows real directories, files, classes, methods, and nested functions.
Use **+** to expand a node, search for a symbol, or select **Focus on this node**
to inspect a subtree. Switch between 3D, flat, and outline views. Each connection
means containment, not a call. The page makes no broker requests and needs no
server or frontend dependencies.

Regenerate the hierarchy after source changes with
`python docs/build_hierarchy.py`. It reads public Git-tracked paths and Python
syntax trees; local config, credentials, and runtime state are excluded. Source
links use the recorded Git base where available. Validate the generator with
`python -m unittest discover -s docs -p 'test_build_hierarchy.py'`.

The selected configuration has two independent alerts:

| Price condition | Volume condition |
| --- | --- |
| META price **> $760** | Latest completed 5-minute volume **≥ 2×** its same-time historical average |
| META price **> $787** | The same volume condition |

**Live status:** standalone Robinhood login, token renewal, phone delivery, and
an installed background service have not yet been verified end to end. Tests use
mocked network responses. A Codex connection does not verify this service.
The first standalone attempt registered the OAuth client, then timed out while
waiting for browser authorization. No token was saved. Run `login` again to finish.

## Install and configure

Use Python **3.12 or later**. From the repository on each machine, run the commands
below. If `config.local.json` already exists, skip the copy command.

```sh
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
cp config.robinhood.example.json config.local.json
```

Dependencies include `mcp==2.3.0` and `pandas-market-calendars==5.4.0`. Relative config paths resolve
against the config file's directory.

### Connect your iPhone

1. Install the [ntfy iOS app](https://apps.apple.com/us/app/ntfy/id1625396347).
2. Allow notifications when iOS asks.
3. Generate a random topic locally:

   ```sh
   python -c 'import secrets; print("meta-" + secrets.token_hex(16))'
   ```

4. Set `ntfy_server` to `https://ntfy.sh` and `ntfy_topic` to that generated name
   in `config.local.json`.
5. Add a subscription in the app with the same server and topic.
6. Validate the config, then send a test:

   ```sh
   python monitor.py validate --config config.local.json
   python monitor.py notify-test --config config.local.json
   ```

7. Confirm receipt on your iPhone, including while locked. Server acceptance
   alone does not prove phone delivery.

See the [ntfy phone guide](https://docs.ntfy.sh/subscribe/phone/).
Public topics have no access control: anyone who knows the name can read or
publish messages. Use an unguessable name and keep it out of Git. HTTPS protects
transport; it does not make a public topic private. For a protected topic, set
`ntfy_token_env` to an environment variable name and put the bearer token in that
variable. See [topic selection and publishing](https://docs.ntfy.sh/publish/#picking-a-topic).

Messages use normal priority. A self-hosted server needs
[iOS upstream configuration](https://docs.ntfy.sh/config/#ios-instant-notifications)
and a server URL reachable by the phone. If notifications stop, ntfy's
[iOS troubleshooting notes](https://docs.ntfy.sh/known-issues/#ios-app-not-receiving-notifications-anymore)
suggest removing and adding the subscription again.

### Authorize this service

Run login in an interactive terminal on the machine that will run the monitor:

```sh
python monitor.py login --config config.local.json
```

Complete the Robinhood browser flow. The callback uses `127.0.0.1:8766`; login
then verifies a META quote. The client stores its own OAuth credentials in
`.state/robinhood.json` with mode `0600`. It does not copy Codex tokens.
Authenticate each machine separately; do not sync credentials.

The client allows only `get_equity_quotes`, `get_equity_historicals`, and
`get_equity_fundamentals`. The monitor uses quotes and historical bars. It has no
account-data or order calls. This application boundary does not narrow the
underlying OAuth grant. See Robinhood's
[connection guide](https://robinhood.com/us/en/support/articles/agentic-trading-overview/)
and [tool catalog](https://robinhood.com/us/en/support/articles/trading-with-your-agent/).

Unattended runs attempt token renewal when possible. If interactive authorization
is required, they report an error and do not open a browser. Stop the service,
run `login`, and restart it. Live renewal still requires verification.

## Check and run

```sh
python monitor.py run --config config.local.json --once --dry-run
python monitor.py run --config config.local.json
```

`--dry-run` fetches data and evaluates rules without alerts or alert-state changes.
`--once` performs one poll. The NASDAQ calendar excludes weekends and holidays and
handles early closes and daylight-saving changes. Outside a regular session,
`--once` exits without connecting to Robinhood; this does not verify credentials
or data freshness.

The background process connects at market open and closes the connection at
session end. Between sessions it sleeps until the next scheduled opening, with
one log entry and no two-minute checks or Robinhood authentication attempts.
The session deadline also cancels slow Robinhood requests at close. The process stays in
memory while idle and does not wake a sleeping computer. Keep the host awake;
restart the service after host suspension or a manual clock change to recalculate
the wait. The explicit `login` command remains available outside trading hours.

Quotes must be at most 90 seconds old. Volume uses the latest completed 5-minute
bar, with a maximum age of 360 seconds. The baseline uses up to 20 prior sessions
at the same New York clock interval and requires at least 5 valid sessions.
Missing, stale, interpolated, incomplete, or invalid data cannot trigger an alert.
Insufficient history means no alert until enough data is available.

Quotes are polled every 2 minutes. Historical bars are cached and refreshed at
bar boundaries. Missing bars are retried on later polls until the next boundary.
Source timestamps remain
explicit. Polling can miss brief price crossings; volume confirmation waits for a
completed bar. Robinhood polling limits and available history need live checks.

Each rule has persisted state for each New York session. A first valid match can
alert at startup. A restart preserves an already-delivered match. A valid
non-match rearms the rule; invalid samples do not. A 900-second cooldown delays
another alert after rearming. If price exceeds $787 and volume matches, **both
rules can alert**. Failed delivery remains retryable. A crash after delivery but
before saving state can still cause a duplicate.

## Run in the background

Complete login, a regular-session dry run, and the phone test first. Stop any
foreground monitor. With the virtual environment active, render the service:

```sh
python monitor.py service --config config.local.json --output-dir .state/services
```

This only writes a file. Review its absolute paths before installation. It keeps
the virtual-environment interpreter path. Regenerate after moving the repository
or environment. If `ntfy_token_env` is set, supply that variable in the service
environment; a terminal export is not a persistent service setting.

On macOS, install and start the launch agent:

```sh
mkdir -p "$HOME/Library/LaunchAgents"
install -m 600 .state/services/com.stock-monitor.meta.plist "$HOME/Library/LaunchAgents/com.stock-monitor.meta.plist"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.stock-monitor.meta.plist"
launchctl print "gui/$(id -u)/com.stock-monitor.meta"
```

It starts on login and restarts after exit, with a 30-second throttle. Logs are
`monitor.stdout.log` and `monitor.stderr.log` in `.state` beside the config.
Stop it with `launchctl bootout "gui/$(id -u)/com.stock-monitor.meta"`. To reload a
changed plist, stop it, install the new file, and run `bootstrap` again.

On Linux, install and start the user unit:

```sh
mkdir -p "$HOME/.config/systemd/user"
install -m 600 .state/services/stock-monitor.service "$HOME/.config/systemd/user/stock-monitor.service"
systemctl --user daemon-reload
systemctl --user enable --now stock-monitor.service
systemctl --user status stock-monitor.service
journalctl --user -u stock-monitor.service -f
```

It restarts after failure with a 30-second delay. Stop it with
`systemctl --user disable --now stock-monitor.service`. After changing the unit,
run `daemon-reload` and `systemctl --user restart stock-monitor.service`. A user
service follows the user's session lifetime; configure Linux lingering separately
if it must run after logout.

Both machines can hold the code, but run **one active monitor** to avoid duplicate
alerts. Locks protect local state; there is no distributed state or failover.
The active machine must stay awake and online. Keep config, OAuth credentials,
ntfy topic/token, logs, and runtime state out of this public repository.

## Local verification

```sh
PYTHONPATH=src python -m unittest discover -s tests -v
python monitor.py demo
```

The demo uses synthetic data and prints a console alert. The original
`normalized_file` source remains available through `config.example.json`; set its
thresholds and provide snapshots using the contract in `parse_snapshot`.
The `console` and `desktop` notification modes also remain available. Local tests
do not verify Robinhood rate limits, historical depth, token renewal, iPhone
delivery, or background execution on both operating systems.
