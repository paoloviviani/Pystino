# 0052 — Quota notifications: the rule is the administrator's, the threshold is the user's

- Date: 2026-09-03
- Status: **accepted, built**
- Builds on [0009](0009-quota-model.md)/[0025](0025-quota-model-v2.md) (the
  rules) and [0051](0051-settings-identity-and-email.md) (the mail server).
- Requested as: email notifications per quota, at arbitrary percentage reach —
  and, on the design question: **quotas are admin-defined, notifications are
  user-defined.**

## Decision

**Each user subscribes, per rule that constrains them, to their own arbitrary
percentage thresholds.** No admin-defined thresholds, no deployment-wide
default, no opt-out to implement — there is nothing to opt out of, because a
user who never subscribes is never mailed.

- The tables split decision from memory: `quota_notification_settings` is the
  user's thresholds (cascading with user and rule);
  `quota_notification_state` is the highest percentage already announced for a
  (user, rule). A threshold fires when the live percentage reaches it **above**
  that mark; when usage falls below it again — a quota reset, or a rolling
  window decaying — the mark is pulled down and the threshold re-arms.
  Without the re-arm, one crossing in March would buy silence forever.
- **Zero queries on the request path.** Admission's round trips are pinned by
  test_query_counts.py, so the notifier polls its own view every 10s and
  `observe()` — called from inside admission with the rules just measured — is
  dictionary work. A crossing spawns a detached send; the announcement memory
  updates synchronously first, so a crash after it costs one email, never a
  flood of retries.
- **The percentage is the message.** The mail names the rule, the percentage,
  the threshold the user asked for and the window — and points at the
  Overview for exact figures. Rendering the *amount* would mean Decimal
  arithmetic in a mail template, which is the ledger's job; the percentage is
  what "notify me at 80%" means anyway.
- Delivery uses the Settings screen's mail configuration (ADR 0051), read per
  send. Not configured means the crossing is recorded and skipped — when the
  mail server arrives, the next crossing tells them.

## Consequences

- A subscription to a rule that stops applying is refused (`PUT
  /api/me/limits/{id}/notifications` validates the rule constrains the
  caller) — a threshold on someone else's budget is a subscription to
  somebody else's mail.
- Saving new thresholds clears the announcement memory for that (user, rule):
  if the usage is already past the new threshold, the next poll announces it —
  which is what the user just asked for.
- For a shared quota (group or global scope), each subscriber's own threshold
  fires independently against the shared counter: the group's usage crossing
  80% mails everyone who asked at 80%, each once, until the usage falls back.
