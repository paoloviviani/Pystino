# The console

!!! info "For administrators"

    A map of the console at `/console`: which screen answers which question, and which page explains it.

The console is the React admin app the gateway serves at `/console`. Everyone
who signs in sees two personal screens; administrators also get an **Admin**
button beside their name, which opens a landing page with a card per screen.
Each card says what the screen decides, not what it is called.

## For everyone

| Screen | What it shows |
|---|---|
| **Overview** | where you stand this period, your quotas and the API keys you spend with (create and revoke them here) |
| **Your usage** | what you spent, and what the figures rest on. Administrators have it too: their own spend is not administration |

Money is shown to three decimal places and never rounds a real amount down to
zero (it reads `< €0.001`). Administrators can switch to full precision with
the **Exact figures** toggle.

## Administration

| I want to… | Screen | Read |
|---|---|---|
| see what the deployment spent, by group, person, model or day | **Usage** | [Accounting and quotas](accounting-and-quotas.md#the-ledger) |
| set a ceiling, see what it has consumed, check a counter against the ledger (**Quota health**, with **Reconcile**), or reset it on the record, with or without a reason | **Quotas** | [Quotas](accounting-and-quotas.md#quotas), [Quota health and reconcile](accounting-and-quotas.md#quota-health-and-reconcile) |
| decide what is stripped from prompts before a provider sees them, and for whom | **Redaction** | [Redaction](redaction.md#operating-it) |
| add a provider, its credentials and what it reports | **Providers** | [Providers and plugins](gateway.md#providers-and-plugins) |
| change what models are on offer, who may use them and what they cost | **Models** | [Three cost figures](accounting-and-quotas.md#three-cost-figures-one-meaning-each) |
| choose the search backends and see how many searches each group spent | **Web search** | [Surfaces](gateway.md#surfaces) |
| add, disable, reset or merge a person | **Users** | [Bundled accounts](bundled-accounts.md), [Identity](identity.md) |
| decide who belongs together and what a group may use | **Groups** | [Groups](identity.md#groups) |
| import or dismiss the group names your identity provider reports | **Groups**, *Seen from your identity provider* | [Importing the provider's groups](identity.md#importing-the-providers-groups) |
| configure mail, read the identity provider, or decide who may become a user | **Settings** | [Identity](identity.md) |
| see what happened to a person's account, and who did it | **Users → Edit → Activity** | [The audit trail](identity.md#the-audit-trail) |

Two things on the **Settings** screen are worth knowing before you look for
them. The **identity provider is read-only**: it is set in `.env` and rewritten
at every start ([One provider, set in the environment](identity.md#one-provider-set-in-the-environment)),
so the console shows it and offers **User sync…** only for an external provider
([User sync](oidc-generic-provider.md#user-sync)). What the console *does* edit
is the provisioning policy: who may come to exist, which claim names their
groups, and what an IdP group means here.

**Reasons are optional.** Every form that offers one (resetting a quota, merging
users, a redaction rule, the sign-in policy) labels the field *Reason
(optional)*; a reason you give is kept in the audit log.

A non-administrator who follows a bookmarked admin link is told they are not
one, rather than shown a blank page; the API refuses them regardless.
