# 0049 — Self-service password reset by email, opt-in and environment-configured

- Date: 2026-09-02
- Status: **accepted, built**
- Amends [0043](0043-local-authentication.md), which declined email reset as
  "a bigger surface than the feature is worth".
- Requested as: "email password reset (env var configurable)".

## Context

0043's reasoning was sound and is recorded here so the amendment argues with
it rather than overwrites it: email-based reset puts account recovery in the
hands of whatever mail server the deployment has, with no SMTP credential
store and no way to test it. What changed is not that reasoning but its
weight: the platform now has real local accounts (ADR 0043, ADR 0048's
console-created users), and "ask an administrator to run `gateway passwd`"
stops being a footnote and becomes the recovery story.

## Decision

**Opt-in by environment, bounded to local accounts, single-use tokens,
indistinguishable answers.**

- `GATEWAY_LOCAL_AUTH__PASSWORD_RESET__ENABLED=false` (default) plus an SMTP
  host, port, optional credentials and From address — the whole feature and
  its mail server are deployment configuration, never database state. Disabled
  (or misconfigured) means `POST /auth/password-reset` answers 503 and the
  console says so; the admin-reset path (ADR 0048's account creation, the
  password endpoints) remains the only way in.
- **Local accounts only.** A directory user's password belongs to the IdP
  (the same rule that keeps passwords off IdP users, ADR 0043's
  `_load_local_user`); a reset link minted here would be overwritten at the
  next login and mean nothing in between.
- The request endpoint answers **the same 200 whether the address exists, is
  a directory account, or is throttled** — no user enumeration, by response,
  by timing (delivery is off the request path), or by error text. The page
  behind it keeps the promise: "if an account exists for that address".
- The link carries a **single-use, high-entropy token valid one hour**,
  stored only as a SHA-256 hash (`password_reset_tokens`, migration 0019) —
  the API-key shape (ADR 0010), because a high-entropy credential needs a
  lookup, not a KDF. A newer request deletes the older link: a forgotten
  "did I already ask?" must not leave a live door standing.
- Delivery is stdlib `smtplib` in a worker thread — the dependency that would
  be adopted for one email is not worth its supply chain — with STARTTLS
  always attempted, and delivery failure logged and invisible to the answer.
- A per-address cooldown (per worker, like the login throttle) keeps a stream
  of reset mails to a victim's address from being its own abuse; a throttled
  request answers as success, so throttling cannot be told apart from
  delivery.

## Consequences

- The console grows two public routes (`/console/password-reset`, with
  `?token=` for the confirm half) and a "Forgot your password?" link that
  shows on the login form whenever local auth does.
- The confirm endpoint validates the password with the deployment's own
  policy and does not spend the token on a failed validation — the person can
  type a proper password into the same link.
- A spent or foreign token and an expired one are one message: which of those
  failed is exactly what a holder of a stolen link should not learn.
- Statelessness cuts both ways as before: a password change cannot revoke an
  already-issued session cookie. The re-check on every request is what bounds
  a hijacked session, as it was before this existed.
