# Contributing

Thanks for helping. A short conversation first saves work on both sides.

- **Bugs and ideas:** open an issue. For a bug, include what you sent, what you
  expected, what happened, and the gateway version.
- **Changes:** open a pull request against `main`, focused on one thing, saying
  what problem it solves and how you checked it.
- **Before you push:** `uv run pytest -q`, `uv run ruff check .` and
  `uv run mypy apps/gateway/src packages/shared-py/src`. Anything touching
  accounting, quotas or redaction needs tests: a wrong answer there is a wrong
  invoice or a leak, not a stack trace.
- **Money is never a float.** Amounts are `Decimal` in Python and strings on the
  wire; `accounting/cost.py` is the only code that multiplies a count by a rate.
- **Security issues** go to [SECURITY.md](SECURITY.md), not to a public issue.

By contributing you agree that your contribution is licensed under the
EUPL-1.2, the licence of this repository.
