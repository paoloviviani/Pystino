# 0010 — API key format, hashing and revocation

- Status: accepted
- Date: 2026-08-14

## Context

Programmatic callers authenticate with API keys. The brief specified SHA-256 rather
than a slow KDF, which is correct and counter-intuitive enough to write down —
"we hash secrets with Argon2" is otherwise a reasonable-sounding objection from the
next reviewer.

## Decision

### Format

```
gwk_<8 hex chars>_<43 url-safe base64 chars>
 │      │              └── 32 bytes from secrets.token_urlsafe
 │      └── public lookup handle, indexed
 └── configurable environment prefix
```

- The handle is embedded in the key, so authentication is **one indexed row fetch**
  rather than a table scan. A malformed credential is rejected by a string split,
  before any query.
- Prefixing also makes keys recognisable to secret scanners.

### SHA-256, not Argon2 — the reasoning

A slow KDF exists to make guessing a **low-entropy** secret expensive. There is
nothing to guess here: an attacker must find one value in 2^256, and no amount of key
stretching changes that. Meanwhile the cost is paid on **every single API call**,
because unlike a login this credential is presented on every request — Argon2 at sane
parameters would add tens of milliseconds to every completion.

What SHA-256 does buy is that a leaked database dump yields no usable keys.
Verification is `hmac.compare_digest` against the stored digest.

### Revocation, not deletion

`revoked_at` is set; the row stays. The usage ledger references keys, and deleting one
would turn historical spend into an unattributable row.

Expiry (`expires_at`) is optional and checked at every request.

### Billing group resolution

A key may pin a `billing_group_id`; otherwise the user's current
`default_billing_group_id` applies, so changing the default retroactively affects
unpinned keys.

**Membership is re-checked on every request**, never trusted from when the key was
minted. Leaving a group immediately stops you billing it. A disabled group is refused
too. Both are tested.

### Uniform failures

Every authentication failure — unknown key, wrong key, revoked, expired, inactive
user — returns the same 401 with the same message. Distinguishing them would tell an
attacker which of their guesses was structurally correct.

Missing billing group and non-membership return **403** with a specific message,
because those are the caller's own configuration problem and an opaque error would
just generate a support request.

## Consequences

- The secret is returned exactly once, from `POST /api/me/keys`, and is not
  recoverable afterwards.
- `last_used_at` is written at most once a minute per key. It is a convenience for
  humans auditing keys, not part of any decision, and a write per request would be
  pure overhead. Failures to update it never fail a request.
- Both `Authorization: Bearer <key>` and a bare key are accepted, because several
  OpenAI-compatible clients omit the scheme. The `api-key` header is also accepted.
- The caller's key is **never** forwarded upstream; the upstream gets its own
  credentials. Tested.
