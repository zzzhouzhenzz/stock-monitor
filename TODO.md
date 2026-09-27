# Remaining work

## Initial review units

1. **Price-and-volume rule engine:** `.gitignore`, `AGENTS.md`, package initializer,
   `rules.py`, `test_rules.py`, and a concise core README with a runnable usage
   example. Verify the rule tests on this CL alone.
2. **Portable snapshot monitoring and persistent alerts:** `runtime.py`, `cli.py`,
   `monitor.py`, `pyproject.toml`, example config, runtime tests, and the related
   README/TODO additions. Depends on CL 1. Verify the full suite, demo, rejected
   unset thresholds, and one-shot snapshot/restart behavior. Keep the package's
   console entry point with this CL because it requires `cli.py`.

These form two sequential commits, with docs scoped to behavior present at each
step. Apply the same one-purpose rule to subsequent source-adapter,
notification and deployment changes; do not bundle them into one integration CL.

## Live integration

- Choose actual price and volume thresholds, volume mode, and notification destination.
- Connect the official Robinhood MCP with user authentication and inspect live schemas.
- Verify volume intervals, timestamps, historical depth and polling limits.
- Implement and test a read-only source adapter against verified response fixtures.
- Verify real alert delivery, auth renewal and both target OS environments.
- Add the selected OS service definitions after the live loop works.

Prototype verification: `PYTHONPATH=src python3 -m unittest discover -s tests -v`
and `python3 monitor.py demo`. These do not validate live Robinhood data.
