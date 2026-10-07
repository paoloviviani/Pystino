# Bundled accounts: the Users page

!!! info "For administrators"

    Running the bundled Authelia and its people from the console's Users page; with an external provider, accounts live there instead ([Identity: the OIDC provider](oidc-generic-provider.md)).

With the `authelia` profile on, Authelia runs beside the gateway, from the
stock image, with the configuration in `deploy/authelia/configuration.yml`.
Its people are not edited in a file or on Authelia's side: they are managed
from the console's **Users** page, which writes the login and the gateway
account together.

## The bundled Authelia

- **The issuer is a subpath** of the public origin: `<origin>/authelia`.
  Every endpoint in its discovery document carries the same prefix, and the
  proxy forwards `/authelia/*` without stripping it.
- **Server-to-server calls stay inside the network.** The gateway and the
  chat reach it at `http://authelia:9091/authelia`, with forwarded headers
  naming the public issuer, so no container needs to trust the proxy's
  certificate.
- **Three clients:** `pystino-console` and `cerea` (confidential, consent
  implied, since both are first-party) and `opencode-enrollment` (public,
  for agent machines: loopback and device flows, with a long-lived refresh
  token; see [Coding agents](coding-agents.md)). Access tokens are RS256 JWTs
  carrying the `pystino-api` audience and the `groups` claim, and live one
  hour.
- **Password only**: the policy is one factor, and TOTP and WebAuthn are
  switched off in the rendered configuration. A second factor comes from an
  external IdP such as Keycloak, configured through the environment
  ([Identity](identity.md#one-provider-set-in-the-environment)).
- It needs a **dotted host name** in this repository's `deploy/`: browsers
  refuse its session cookie on a dotless name, and `deploy/` has no way to
  serve an IP address. cerea-deploy also accepts an IP address (with
  `--tls internal`); it refuses only single-word names such as `myserver`.

## The Users page

Each row shows the person, their email and, for someone with a bundled login,
a `sign-in: <login>` line: the name they type on the sign-in page. The row
keeps **Edit**, **Disable** / **Enable** and **Delete**, the actions that
change state; the person's other tools live in the **Actions** section of the
edit panel (below).

### Add user

**Add user** (top right) creates the Authelia login and the gateway account
together. It asks for an email, a login, an optional display name, and
groups. Three details:

- the **login** prefills from the email's local part and stays editable; it
  is the name typed on the sign-in page, and may contain lowercase letters,
  digits, `.`, `_` and `-`. A name another person already holds is refused
  with a sentence saying so;
- the **groups** are console groups, the same manual memberships an
  administrator grants anywhere else. The bundled users file itself carries
  only `users`;
- the account exists, with its login, from the moment of creation, though
  the person has not signed in yet.

It then shows a **one-time password**, once, under "Sign in as *login* with
this password", with a Copy button.

!!! warning "Passing the password on"

    Share it over a one-time channel (a password manager share, or in
    person). Authelia cannot force a change at first sign-in: ask the person
    to change it from the sign-in page's reset link, where reset is enabled (it
    needs SMTP, below), or to keep it.

### The edit panel's Actions

**Edit** opens the person's panel: their profile, and under **Actions**:

| Action | What it does |
|---|---|
| **Reset password** | mints a fresh one-time password for their bundled login (the old one stops working) and shows it once, in the same notice as above |
| **Create sign-in** | gives an existing account a bundled login. Offered **only to a person who has none**: the case after switching back from an external IdP or after [break-glass](identity.md#break-glass), for someone who only ever had an identity at the other provider. A person who already has one is refused ("They already sign in as *login*. Use Reset password.") and is offered Reset password instead, so one person can never end up with two working passwords |
| **Activity** | the identity events for this person, from the [audit trail](identity.md#the-audit-trail) |
| **Merge into…** | moves this account onto another; see [Merging and deleting accounts](identity.md#merging-and-deleting-accounts) |

**Disable** and **Enable** cut a person's access everywhere without erasing
anything, and the bundled login follows: disabling the account disables their
Authelia login too, enabling re-enables it. **Delete** erases the person
everywhere, chat included (see [Merging and deleting accounts](identity.md#merging-and-deleting-accounts)).

### Signing in and resetting a password

Passwords belong to the identity provider. **With SMTP configured** (the
`SMTP_*` variables in `.env`) on the full stack (cerea-deploy), the bundled
Authelia enables self-service reset: people reset their own password from the
sign-in page, and the same setting sends quota notices. Without SMTP, and in
this repository's own Pystino-only `deploy/` (whose Authelia configuration keeps
password reset switched off), only an administrator can, by issuing a one-time
password from the edit panel.

### With an external provider

The Users page still lists the people and still disables and enables them on
the gateway side, but it cannot add a login or reset a password, and says so:
accounts live at the identity provider. Add user, Create sign-in and Reset
password appear only while the enabled provider is the bundled Authelia.
Optional [User sync](oidc-generic-provider.md#user-sync) is the external
provider's counterpart.
