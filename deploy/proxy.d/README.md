# Component hook

Any `*.caddy` file dropped here is imported into the public site block, above
the built-in routes (`deploy/caddy/Caddyfile`'s `import /etc/caddy.d/*.caddy`).
It is a directory mount, so a new or replaced file is seen on the next proxy
reload without the stale-inode trap a single-file mount has.

None is needed by default; a glob that matches nothing is not an error.

Reload after adding or editing a file here:

```
docker compose exec proxy caddy reload --config /etc/caddy/Caddyfile
```

Git ignores `*.caddy` in this directory (see `deploy/.gitignore`), so
`git pull` never conflicts with a local snippet.
