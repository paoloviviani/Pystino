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
| **Overview** | where you stand this period, your quotas and the API keys you spend with (create and revoke them here; a person without the API-keys capability is told so instead of being offered Create) |
| **Your usage** | what you spent, and what the figures rest on. Administrators have it too: their own spend is not administration |

Money is shown to three decimal places and never rounds a real amount down to
zero (it reads `< €0.001`). Administrators can switch to full precision with
the **Exact figures** toggle.

## Administration

| I want to… | Screen | Read |
|---|---|---|
| see what the deployment spent, by group, person, model or day | **Usage** | [Accounting and quotas](accounting-and-quotas.md#the-ledger) |
| set a ceiling — the group's total or each member — see what it has consumed, check a counter against the ledger (**Quota health**, with **Reconcile**), or reset it on the record, with or without a reason | **Quotas** | [Quotas](accounting-and-quotas.md#quotas), [Group rules: the total, or each member](accounting-and-quotas.md#group-rules-the-total-or-each-member), [Quota health and reconcile](accounting-and-quotas.md#quota-health-and-reconcile) |
| decide what is stripped from prompts before a provider sees them, and for whom | **Redaction** | [Redaction](redaction.md#operating-it) |
| add a provider, its credentials and what it reports | **Providers** | [Providers and plugins](gateway.md#providers-and-plugins) |
| change what models are on offer, who may use them and what they cost, and name a model as your clients should show it | **Models** | [Three cost figures](accounting-and-quotas.md#three-cost-figures-one-meaning-each), [A model's name and its display name](gateway.md#a-models-name-and-its-display-name) |
| choose the search backends and see how many searches each group spent | **Web search** | [Surfaces](gateway.md#surfaces) |
| add, disable, reset or merge a person | **Users** | [Bundled accounts](bundled-accounts.md), [Identity](identity.md) |
| see at a glance who may use only the chat | **Users** (the **Chat only** badge) | [Chat-only people](identity.md#chat-only-people) |
| decide who belongs together, what a group may use, and who holds coding agents or API keys | **Groups** (the two **Capabilities** toggles) | [Groups](identity.md#groups), [Chat-only people](identity.md#chat-only-people) |
| map or dismiss the group names your identity provider reports | **Groups**, *Groups from your identity provider* | [Mapping the provider's groups](identity.md#mapping-the-providers-groups) |
| configure mail, read the identity provider, decide who may become a user, set what new groups start with, or turn a capability on or off for every group | **Settings** | [Identity](identity.md) |
| see what happened to a person's account, and who did it | **Users → Edit → Activity** | [The audit trail](identity.md#the-audit-trail) |

Two things on the **Settings** screen are worth knowing before you look for
them. The **identity provider is read-only**: it is set in `.env` and rewritten
at every start ([One provider, set in the environment](identity.md#one-provider-set-in-the-environment)),
so the console shows it and offers **User sync…** only for an external provider
([User sync](oidc-generic-provider.md#user-sync)). What the console *does* edit
is the provisioning policy: who may come to exist, which claim names their
groups, what an IdP group means here, and what new groups start with
([Chat-only people](identity.md#chat-only-people)).

**Reasons are optional.** Every form that offers one (resetting a quota, merging
users, a redaction rule, the sign-in policy) labels the field *Reason
(optional)*; a reason you give is kept in the audit log.

A non-administrator who follows a bookmarked admin link is told they are not
one, rather than shown a blank page; the API refuses them regardless.

## Translations

The console's words live in one catalogue, `apps/console/messages/en.json`:
every button, heading, notice and placeholder on every screen is read from
there, and none is written in the screen's own file. English is the only
language that ships; the machinery exists so that adding one is a file, not a
change to twenty screens.

**Adding a language** is three steps:

1. Copy `messages/en.json` to `messages/<locale>.json` (say, `messages/de.json`)
   and translate the values. The keys and the file's shape stay exactly as they
   are; a `{placeholder}` or a `{#tag}…{/tag}` inside a value is syntax, not
   text, and must survive the translation where it is.
2. Add the locale to `locales` in `apps/console/project.inlang/settings.json`.
3. Run `pnpm --filter @llmp/console i18n:check`. It fails on a key the code uses
   that the new file is missing, and on a message whose placeholders do not
   match the English one, before anything ships.

The language a reader gets is picked in order: an explicit choice stored in the
browser (`localStorage["console.locale"]`), then the browser's own preferred
language if the catalogue has it, then English. Setting the key by hand — say,
`localStorage.setItem("console.locale", "de")` in the console's devtools — is
how you try a translation that has no menu yet.

**Finding what is not yet translated** is what `en-XA` is for. It is a
pseudo-locale generated from the English catalogue
(`pnpm --filter @llmp/console i18n:pseudo`): every message comes back accented
and wrapped in brackets, with the placeholders untouched. Set
`localStorage["console.locale"]` to `en-XA`, reload, and walk the screens: a
string still reading as plain English is one the catalogue does not carry yet.
It is a developer tool — no reader can arrive at it by accident, and no menu
offers it.

`pnpm --filter @llmp/console i18n:check` is the guard, in CI with the console's
own checks: it fails on a key used in code that `en.json` does not have, on
`en-XA.json` being out of date with `en.json`, and on a screen that has been
converted carrying English text in its JSX again.
