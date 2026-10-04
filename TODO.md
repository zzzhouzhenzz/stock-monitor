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
- [x] Verify that a closed session waits until the next opening without connecting.
  Off-hours process smoke test passed on 2026-10-04; see evidence below.
- [ ] Verify at a live session close that slow requests are cancelled and the
  connection closes before sleep. Automated deadline tests already pass.
- [ ] Repeat deployment verification on the other OS without copying credentials.

## Local verification

Off-hours smoke test on 2026-10-04, against commit `b95e28e`:

- Ran the real CLI with the local configuration under a network-attempt audit
  guard. One-shot mode reported `market_closed` and exited with code 0.
- Continuous mode selected 2026-10-05 06:30 Pacific as the next opening. It
  remained idle for 130 seconds with zero network attempts or repeated log entries.
- The process used 0.000282 CPU seconds during observation. Peak resident memory
  was 120,307,712 bytes; the process remains in memory while idle.
- A second instance was rejected by the state lock. Ctrl-C exited cleanly with
  code 0, and another one-shot run confirmed that the lock was released.
- Config, alert-state, and credential file metadata were unchanged. The test
  process was stopped; no background service was installed or started.
- Local report and logs: `.state/smoke-2026-10-04/` (ignored by Git).

This verifies closed-market behavior, not live quotes, OAuth renewal, or iPhone
delivery.

Run `PYTHONPATH=src python -m unittest discover -s tests -v` and
`python monitor.py demo`. Tests cover rule boundaries, persisted alert episodes,
market-data normalization, ntfy failures, and generated service files. The macOS
plist renderer also passed `plutil -lint`. Live checks above remain separate.

Continue using small changes with one purpose: rule changes, source/auth changes,
notification changes, and service deployment changes. Include focused tests and
matching docs. Do not commit local config, topic names, credentials, logs, or
runtime state.
