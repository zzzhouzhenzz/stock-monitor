# Verification still required

The selected rules are META **> $760** and **> $787**, each with latest completed
5-minute volume **≥ 2×** its average at the same time over prior sessions.
The selected phone service is ntfy. Polling is 2 minutes; cooldown is 900 seconds.

## Live checks

- [ ] Complete standalone `login` and verify the META quote check.
- [ ] Run `run --once --dry-run` during a regular session. Verify timestamps,
  completed bars, same-time historical depth, and calendar handling.
- [ ] Observe 2-minute polling and Robinhood throttling. Verify token renewal
  after expiry and an actionable error after authorization is revoked.
- [ ] Set an unguessable local ntfy topic, subscribe on the iPhone, and run
  `notify-test`. Confirm receipt while locked; HTTP acceptance is not delivery proof.
- [ ] Review and install one generated service. Verify that restart preserves
  alert state and inspect logs. Keep only one machine active.
- [ ] Verify that a closed session waits until the next opening without connecting,
  and that session close cancels slow requests and closes the connection before sleep.
- [ ] Repeat deployment verification on the other OS without copying credentials.

## Local verification

Run `PYTHONPATH=src python -m unittest discover -s tests -v` and
`python monitor.py demo`. Tests cover rule boundaries, persisted alert episodes,
market-data normalization, ntfy failures, and generated service files. The macOS
plist renderer also passed `plutil -lint`. Live checks above remain separate.

Continue using small changes with one purpose: rule changes, source/auth changes,
notification changes, and service deployment changes. Include focused tests and
matching docs. Do not commit local config, topic names, credentials, logs, or
runtime state.
