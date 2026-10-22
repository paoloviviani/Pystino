# Provider catalogue overrides

Any `*.yaml` or `*.yml` file dropped here is read by the gateway at startup and
merged over the built-in provider catalogue, field by field, by provider id.
A new id adds a provider. It is mounted read-only at `/etc/pystino/providers.d`.

None is needed. An empty directory (this one) is fine.

The files hold no secrets: API keys stay encrypted in the database and are
entered in the console. Apply a change by restarting the gateway:

```
docker compose restart gateway
```

A file the gateway cannot accept stops it from starting, and the log names the
file and the field. Format and examples: `docs/provider-catalogue.md`.

Git ignores `*.yaml` and `*.yml` in this directory (see `deploy/.gitignore`), so
`git pull` never conflicts with a local file.
