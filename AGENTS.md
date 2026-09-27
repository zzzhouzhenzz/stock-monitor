# Stock monitor working agreement

This directory is an independent Git repository. Preserve existing work and keep
all project changes inside this repository. Use `codex/` for working branches.

## Small CLs

Apply [Google's small CL guidance](https://google.github.io/eng-practices/review/developer/small-cls.html):

- Give each change one concrete purpose and keep its scope minimal.
- Include the related tests and documentation in the same change.
- Keep each change independently understandable and testable on its stated base;
  name dependencies explicitly when changes are stacked.
- Keep the project runnable after every change. Introduce APIs with a small
  working usage example; avoid unused scaffolding.
- Split substantial refactoring from behavior changes. Avoid unrelated cleanup.
- Judge size by review complexity, not an arbitrary line-count target.
- Before coding a feature spanning multiple concerns, identify the small changes
  and their verification gates. Summarize results and limitations after each.
- Commit when the user requests commits; creating a repository or requesting
  small CLs does not by itself authorize committing, pushing or publishing.

## Project scope and verification

- Build a portable macOS/Linux stock-price and volume alert monitor.
- Keep Robinhood access limited to the required data tools; monitoring does not
  authorize orders or account changes.
- Keep source timestamps and session semantics explicit; never present mock
  input or an unauthenticated probe as verified live data.
- Keep local configuration, credentials and runtime state out of Git.
- Run focused tests for the changed behavior. Use the full suite for integrated
  changes: `PYTHONPATH=src python3 -m unittest discover -s tests -v`.
