# Coding agents: what the gateway provides

A coding agent on somebody's own machine is a `/v1` client like any other, except
that it cannot hold a credential the way a browser or a server can. It runs
unattended, it is configured by a file on disk, and the person it works for is not
watching. This page is the **gateway's side** of that contract: how such a machine
authenticates to `/v1`, which group it bills, and what the identity provider must
provide.

The machine side lives in the chat repository. galopin, the Cerea machine agent, is
its `agent/` directory. It enrols with the IdP, runs a local refreshing shim in front
of `/v1`, supervises opencode, and dials out to the chat's `/code` panel. Its
installation and operation are documented in the chat repository's
`docs/agent-machines.md` and `agent/PROTOCOL.md`. The stack side is one switch,
`CODE_AGENTS_ENABLED=true` in `.env` (the full stack's; a Pystino-only
deployment has no chat, so no `/code` panel to dial into).

## Two ways in

| How | Credential | Bills |
|---|---|---|
| The setup script (one `curl` line, below) | a `gwk_…` API key a person mints in the console | the key's group |
| galopin (`galopin enroll`, then `galopin run`) | an OIDC enrolment (device or loopback flow), renewed by galopin's local shim | the signed-in person, with `x-bill-to` set to their chosen group; the machine also appears in the chat's `/code` panel |

The script is for someone who just wants opencode to use this gateway, with no
chat and no enrolment. galopin is for a machine an organisation manages: it
holds no standing secret and shows up in the chat's `/code` panel.

## Point opencode at the gateway with a script

1. In the console, mint an API key (**API keys**) and copy it. The script cannot
   do this for you: minting needs a signed-in browser session, which a script
   holding a key does not have.
2. Run, with your gateway's address:

    ```bash
    curl -fsSL https://llm.example.org/opencode/install.sh | bash
    ```

    It asks for the key at the terminal (hidden, never echoed), lists the chat
    models your key can use, shows what it is about to change and asks before
    writing. The gateway serves the script itself, so the address it defaults
    to is the one you fetched it from.
3. Start opencode and pick a model with `/models`.

The script needs only `bash` and `python3`. Read it first if you like:
`curl -fsSL https://llm.example.org/opencode/install.sh | less`.

### What it writes, and where

It adds **one block**, `provider.pystino`, to opencode's global configuration,
`${XDG_CONFIG_HOME:-~/.config}/opencode/opencode.json` (opencode uses that path
on Linux and macOS alike). The block holds the gateway address, the key and one
entry per chat model. Each entry carries the model's name, its context and
output limits, and what the gateway says it can do: image input, and the
low/medium/high reasoning levels opencode offers as variants. Embedding, OCR and
image-generation models are left out, since opencode would offer them in its
picker and fail on first use. These are the same entries galopin writes.

- **An existing file is merged, not replaced.** Your other providers and
  settings stay as they are, and so does your default `model`, unless you pass
  `--model <id>`. An older `provider.pystino` block is replaced; running the
  script again after the catalogue changes is how you refresh it. Before
  writing, the script saves the old file as `opencode.json.bak-<timestamp>` and
  prints what it kept and what changed. The file is re-indented as plain JSON.
- **A file with comments is never rewritten.** opencode also reads
  `opencode.jsonc`, and comments or trailing commas would be lost in a rewrite.
  If the target is not strict JSON, or only an `opencode.jsonc` exists, the
  script stops and changes nothing. `--print` shows the block to paste in
  yourself, and `--output` writes a separate file.
- **One project instead of every project:** `--output ./opencode.json`.
- **Look first:** `--dry-run` shows the summary and writes nothing.

### The key

The key is never taken as an argument, because arguments show up in `ps` and in
shell history. It comes from the `PYSTINO_API_KEY` environment variable, or the
hidden prompt. For an unattended run, set it on the right-hand side of the pipe,
since a prefix on `curl` would reach only `curl`:

```bash
curl -fsSL https://llm.example.org/opencode/install.sh | PYSTINO_API_KEY=gwk_… bash
```

By default the key is written into the file, which is created readable by you
alone (mode 600). To keep the file free of secrets, add `--key-in-env`: the file
then holds `"apiKey": "{env:PYSTINO_API_KEY}"` and opencode reads the key from
that variable when it starts, so it has to be exported wherever you start
opencode (a desktop launcher will not see a variable set only in a shell
profile). The script still uses the key once to discover your models.

```bash
curl -fsSL https://llm.example.org/opencode/install.sh | bash -s -- --key-in-env
```

### Other options

Pass them after `bash -s --`, or run the script from a file.

| Option | Effect |
|---|---|
| `<address>` or `--base-url` | the gateway, if not the one the script came from; `/v1` is added if missing |
| `--model <id>` | also make `pystino/<id>` opencode's default model |
| `--install-opencode` | first install opencode, pinned to the version this setup was tested with, using opencode's own installer (`https://opencode.ai/install`) |
| `--no-discover` | do not call the gateway; write a placeholder model to edit by hand |
| `--yes` | do not ask for confirmation |

It stops, writing nothing, if the gateway refuses the key (the paste is wrong or
the key was revoked), if the key's group is over its cap, or if the gateway
cannot be reached. It does not follow redirects, so a mistyped address cannot
send your key somewhere else.

### Undo

Restore the backup, or take the block out:

```bash
mv ~/.config/opencode/opencode.json.bak-20261004T120000 ~/.config/opencode/opencode.json
```

or delete the `provider.pystino` object (and `model`, if you set it) from the
file. Revoking the key in the console ends its access at once either way.

### For operators

The script is part of the gateway image and is served at
`/opencode/install.sh`, without authentication. Both bundled Caddyfiles already
send that path to the gateway, so no proxy change is needed. The gateway fills
in the origin the request arrived on (scheme and `Host`), after checking that it
is a plain host name or address with an optional port; anything else is left
out and the script asks instead. Behind a proxy, `Host` must reach the gateway
unchanged, which Caddy does by default.

## What `/v1` sees from an enrolled machine

- `Authorization: Bearer <access token>` for the `opencode-enrollment` client. It is
  validated locally like every OIDC bearer: issuer, audience, signature, expiry.
  The shim refreshes it before it expires, and opencode never holds it.
- `x-bill-to: <group>`: the group chosen at enrolment, which must be one of the
  person's groups. Spend lands in the gateway ledger under that group,
  where the console shows it. `/v1` has no usage endpoint of its own.
- Revoking the grant at the IdP ends the machine's `/v1` access and its chat link
  together within one access-token lifetime, because both use the same enrolment
  credential.

## What the IdP side needs

The bundled Authelia has a third OIDC client beside the console's and the chat's:
`opencode-enrollment`. It is **public** — a binary on a user's machine cannot
keep a secret — so there is no client secret, `token_endpoint_auth_method` is
`none`, and PKCE S256 is mandatory so a stolen code is useless without the
verifier. Redirect URIs are loopback only.

Two details are easy to get wrong, and both were found live:

- **`offline_access` must be in `scopes` _and_ `refresh_token` must be in
  `grant_types`.** The token endpoint issues a refresh token only to a client
  whose grant types carry the refresh-token grant; `offline_access` alone is
  not enough. The symptom is a device flow that approves cleanly and a shim
  that then has no refresh credential at all.
- **The device grant needs Authelia 4.39.22 or later.** 4.39.0 introduced it
  and later 4.39.x fixed its bugs, which is why the compose files pin
  `4.39.22` rather than floating on `4.39`.

The `groups` scope matters here for the same reason it matters for the chat:
`/v1` access tokens are validated locally with no userinfo round trip, so a
claim that is not in the token is not seen, and the enrollment would bill to
no group.

### An agent machine needs a longer-lived refresh token than a browser does

A browser session logs out; an agent machine sits idle over a weekend and is
expected to still work on Monday. Identity providers size their defaults for
the browser case:

- **Authelia's** default `refresh_token` lifespan is 90 minutes. The bundled
  configuration (`authelia/configuration.yml`) defines a lifespan profile,
  `agent_machine` (`access_token: 1h`, `refresh_token: 90d`), and assigns it
  to the `opencode-enrollment` client only; the console and chat clients keep
  the default, since their sessions really are browser-length.
- **Keycloak's** realm-level offline-session idle timeout (what governs an
  `offline_access` refresh token) defaults to **30 days**, which is long
  enough for this use case. With your own Keycloak, register an
  `opencode-enrollment` client in the same shape as above.

## Machine credentials, and how access ends

An enrolled machine holds two credentials: an access token that lives an hour,
and a **long-lived refresh token** (90 days on the bundled Authelia's
`agent_machine` profile, above) that galopin's shim uses to mint the next one.
Neither is ever handed to opencode.

Access ends in three ways, and they differ in how fast and how permanently:

- **Revocation at the identity provider** ends the machine's `/v1` access and its
  chat link together, within one access-token lifetime, because both use the
  same enrolment credential.
- **Disabling or deleting the account** in the console is faster. The Users page
  says so when you confirm: console sessions, chat sessions and machine links
  are refused **within about a minute**, and refresh credentials and minted
  API keys are revoked at once. The minute is the chat's revalidation cadence:
  every live machine link is re-checked against the gateway (`GET /v1/me`) once
  a minute, and a refused account **revokes the device** (it is tombstoned, and
  its link is closed) rather than merely disconnecting it. With the bundled
  Authelia, disabling the account disables its login too, under the same
  action; with an external provider, disable the person there as well, or they
  can still authenticate.
- **Re-enabling does not resume a revoked machine.** Its device stays revoked: the person has to re-enroll it by running the pairing command again on that machine. Tell them when you re-enable the account. A machine enrolled before the
  account's sessions were last invalidated is treated the same way.

A gateway that is briefly unreachable is not a refusal: the chat keeps
serving for up to five minutes after the last good answer, and after that
closes the link **without** revoking the device, so the machine reconnects by
itself once the gateway answers again.

## Where the rest of it is documented

The machine agent, the `/code` panel and the machine link are the chat's. See
Cerea's [Agent machines](https://paoloviviani.github.io/Cerea/agent-machines/)
(installing, pairing and running a machine, and the machine policy),
[The `/code` panel](https://paoloviviani.github.io/Cerea/code-panel/)
(operators) and `agent/PROTOCOL.md` in the same repository.
