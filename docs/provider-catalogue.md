# Provider catalogue

The catalogue describes **known providers** in YAML: where each lives, which
behaviour plugin talks to it, which region it serves from, what it retains, and
optional dated price hints. It has two layers:

1. the **built-in catalogue**, shipped read-only in the image, and
2. an **operator directory**, mounted read-only into the gateway, whose files
   override built-in entries field by field and may add providers of their own.

So you can fix one price, record a retention policy your contract gives you, or
add a provider without waiting for a release, and upgrades still bring new
built-in entries. The catalogue is read **once, at startup**: a change applies
when the gateway restarts.

## What it is, and what it is not

The catalogue holds **no credential** and creates **no provider**. A provider
row (its encrypted API key, whether it is active, the models and grants behind
it) is still made by an administrator in the console. The catalogue supplies what
is true of the *kind* of provider.

The two are related by a link, `providers.catalogue_id`:

- **Creating** a provider in the console, you may pick a catalogue entry. The
  plugin, endpoint and model-name prefix are pre-filled from it (and you may
  change them); the entry id is stored on the row.
- **Nothing else is copied** into the database. Region and retention are read
  through the link each time they are needed, so correcting a file and
  restarting corrects every provider linked to it at once, with nothing stale
  left in a table.
- An existing provider can be **linked or unlinked** in its Edit dialog. Rows
  created before the catalogue existed are not linked automatically: guessing
  from the plugin name would attach region and retention claims nobody made.
- If a row names an entry the catalogue no longer has (you removed your own),
  the row keeps working, the Providers page flags it, and its region and
  retention count as unknown.

The Providers page lists every entry with its endpoint, region, retention and
price-hint count, and for each value says where it came from: **built-in**, or
**override** with the file that set it. `GET /api/admin/provider-catalogue`
returns the same, with the origin of every field.

## The operator directory

| | |
|---|---|
| Setting | `GATEWAY_PROVIDERS_DIR` |
| Default (unset) | `/etc/pystino/providers.d`, used if it exists |
| If set | must exist, or the gateway refuses to start |
| Files read | `*.yaml` and `*.yml`, in name order; other files are ignored |

In `deploy/compose.yaml` the host directory `./providers.d` (or
`PYSTINO_PROVIDERS_DIR`) is mounted read-only at `/etc/pystino/providers.d`, so
the default applies and nothing needs setting. An empty directory is fine.
After editing a file: `docker compose restart gateway`.

## File format

Each file has one top-level key, `providers`, a list of entries:

```yaml
providers:
  - id: mistral                      # required: the key the layers merge on
    name: Mistral AI                 # display name
    description: Hosted in the EU.   # optional
    plugin: mistral                  # a provider plugin (see /api/admin/provider-plugins)
    base_url: https://api.mistral.ai/v1   # optional; the plugin's default if omitted
    prefix: ""                       # optional model-name prefix
    region: eu                       # lower-case tag: eu, us, eu-west, ...
    retention:
      zero_data_retention: false     # true / false; leave out if unknown
      retention_days: 30
      trains_on_customer_data: false
      note: Per our DPA of 2026-03-01.
      source_url: https://example.org/dpa
    prices:                          # dated hints, per model id as the provider names it
      mistral-large-latest:
        - effective_from: 2026-01-01
          input_per_mtok: "2.00"     # per million tokens; quote decimals
          output_per_mtok: "6.00"
          currency: EUR
```

| Field | Notes |
|---|---|
| `id` | Required. Lower-case letters, digits, `.`, `_`, `-`. |
| `name` | Required once merged (built-in or override must set it). |
| `plugin` | Required once merged; must be an installed plugin. |
| `base_url` | `http(s)` with a host, no credentials in the URL. Omit to use the plugin's own default, where it has one. |
| `region` | A free-form lower-case tag. Routing rules compare tags, so be consistent. |
| `retention.*` | Absent means **unknown**, which is not the same as "no". |
| `prices` | `effective_from` is a date. Give `input_per_mtok` and `output_per_mtok`, or `per_image` / `per_page`; `cache_read_per_mtok` and `cache_write_per_mtok` are optional. |

**Price hints** are used by model discovery and import when "fill missing
prices" is ticked: for a model the provider lists and publishes no price for, the
latest hint already in force is used before the community file, and the price
row is recorded with the source `catalogue_hint`. A price the provider publishes
is never replaced.

## Merging

Entries are merged **by provider id**, and a file later in the name order wins
over an earlier one (built-in first, then the operator directory):

- every scalar is overridden **individually**: an override that names only
  `region` leaves the name, endpoint and the rest alone;
- `retention` merges **per sub-field**;
- `prices` merge **per model and `effective_from` date**, and per rate within a
  dated hint, so fixing one rate leaves its sibling rates and the other dates;
- an explicit `null` **clears** a value (`region: null`); leaving the key out
  keeps the lower layer's value;
- a new id adds a provider; built-in entries you do not mention are kept.

An override for an existing id therefore only needs its `id` and what it changes:

```yaml
# providers.d/local.yaml
providers:
  # The contract with Mistral gives us zero retention.
  - id: mistral
    retention:
      zero_data_retention: true
      retention_days: 0
      note: Zero-data-retention addendum signed 2026-05-12.

  # A price hint: both rates are needed when no lower layer has this one yet.
  # Once a hint exists (built-in, or an earlier file), a later file may name
  # just the rate it fixes and keep the rest of that hint.
  - id: nebius
    prices:
      meta-llama/Llama-3.3-70B-Instruct:
        - effective_from: 2026-06-01
          input_per_mtok: "0.13"
          output_per_mtok: "0.40"

  # A provider of our own.
  - id: in-house
    name: In-house vLLM
    plugin: generic
    base_url: http://vllm.internal:8000/v1
    region: eu
    retention:
      zero_data_retention: true
```

## Validation

The files are validated when the gateway starts. A bad file **stops the
gateway**, and the message (also logged at CRITICAL) names the file and the
field, for every problem at once:

```
override typo.yaml: providers[0].regoin: Extra inputs are not permitted (unknown key)
provider 'fresh' (from override new.yaml): plugin: required, and no layer sets it
```

- **Unknown keys are refused**, because a typo that is ignored is a residency
  rule that quietly does not apply.
- **Types are strict**: `zero_data_retention: "yes"` is an error, not a guess.
- **No secrets.** A field *named* like a secret (`api_key`, `secret`,
  `password`, `token`, `credential`, `authorization`, `bearer`, ...), or a value
  that looks like a key (`sk-...`), or credentials inside a URL, is refused.
  Keys live encrypted in the database and are entered in the console.
- After merging, each entry needs a `name` and an installed `plugin`.

## Region and retention in routing

Routing selection rules (`SelectionRule`, see [Model aliases](model-aliases.md))
see each candidate's catalogue facts as `candidate.region`,
`candidate.zero_data_retention` and `candidate.retention_days` (and the full
`candidate.facts`). `None` means unknown: a rule that excludes on a fact decides
for itself what an unknown provider means. The built-in rules do not use them;
residency and zero-data-retention rules are a later step.

## The built-in entries

OpenAI, Anthropic, Mistral, Nebius, Tensorix, Cortecs and OpenRouter ship
built in. They state a region only where the provider commits to one (Mistral
and Cortecs: `eu`) and no retention claims at all: those are for you to record
from your own contract, in an override.
