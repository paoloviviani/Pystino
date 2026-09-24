# GitHub Actions workflows, parked

These belong in `.github/workflows/`. GitHub refused them from the agent's
token on 2026-09-24 with exactly:

    ! [remote rejected] deploy/rearch -> deploy/rearch (refusing to allow a
    Personal Access Token to create or update workflow
    `.github/workflows/images.yml` without `workflow` scope)

The token is a fine-grained PAT; it needs **Repository permissions →
Workflows: Read and write** (or push these yourself). Then:

    git mv deploy/ci/github-workflows/*.yml .github/workflows/
    git rm deploy/ci/github-workflows/README.md
    git commit -m "Activate the workflows" && git push

| Workflow | Runs on | What |
|---|---|---|
| `ci.yml` | PRs (docs excluded); jobs by changed paths | gateway: ruff, mypy, pytest, compose/Caddy parse · console: typecheck, tests · go: gofmt, vet, test — the one PR workflow (thin-agent's parked ci.yml folded in) |
| `images.yml` | pushes to main (path-filtered) and v* tags | build + push to `ghcr.io/paoloviviani/*` (private), prune untagged |
| `stack.yml` | release PRs (`release/*`), manual, Mondays | fresh install + upgrade, with a scripted sign-in |

Budgeted for the GitHub free plan: see the deployment re-architecture report,
§11 ("Action minutes").
