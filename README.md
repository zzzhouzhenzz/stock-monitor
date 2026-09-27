# META price + volume rules

A pure Python rule engine for a portable stock alert monitor. A match requires
price strictly below a configured threshold AND volume at or above its threshold.

Supports daily share counts, completed five-minute bar share counts, and relative
bar volume against matching New York clock intervals in previous sessions.
Invalid, stale, future, incomplete or insufficient observations cannot match.
There are no network calls, broker integration, notifications or background runner
in this change. These arrive in subsequent independent changes.

## Verify

Python 3.9+ with system timezone data; no third-party dependencies.

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -p test_rules.py -v
```

## Runnable example

This uses synthetic data and an explicitly injected clock.

```sh
PYTHONPATH=src python3 - <<'PYTHON'
from datetime import datetime, timezone
from decimal import Decimal
from stock_monitor.rules import Quote, RuleConfig, VolumeObservation, evaluate

now = datetime(2026, 9, 25, 15, 0, tzinfo=timezone.utc)
rule = RuleConfig('META', Decimal('600'), 'daily', Decimal('1000000'))
quote = Quote('META', Decimal('599'), now, now.date())
volume = VolumeObservation('META', Decimal('1200000'), now, now.date())
print(evaluate(rule, quote, volume, now))
PYTHON
```

The caller owns exchange-calendar eligibility and provider normalization. Rules
use UTC timestamps and New York session dates. Credentials and live account data
are not part of this repository. Follow the small-CL agreement in `AGENTS.md`.
