# GitHub Actions workflow, parked

This belongs in `.github/workflows/`. The agent's PAT lacks the `workflow`
scope needed to push there (same restriction already hit on `deploy/rearch`
in both repos, and on Cerea's own `thin-agent/quickwins`):

    ! [remote rejected] thin-agent/quickwins -> thin-agent/quickwins
    (refusing to allow a Personal Access Token to create or update workflow
    `.github/workflows/ci.yml` without `workflow` scope)

The token is a fine-grained PAT; it needs **Repository permissions →
Workflows: Read and write** (or push this yourself). Then:

    git mv deploy/ci/github-workflows/ci.yml .github/workflows/ci.yml
    git rm deploy/ci/github-workflows/README.md
    git commit -m "Activate the CI workflow" && git push

What the workflow does: the gateway pytest suite, `ruff check` (never
`ruff format` — the tree has never been format-clean repo-wide, see
CLAUDE.md) and `mypy apps/gateway/src services` via `uv run` (the exact
commands from CLAUDE.md's "Verifying a change" section), plus `gofmt -l`,
`go vet` and `go test` for every Go module found under `deploy/` (a glob,
not a hardcoded path — the enrollment CLI's module has already moved once,
`deploy/opencode/enroll` → `deploy/agent`).

Deliberately left out: the full `pre-commit run --all-files`. Tried it as
part of verifying this workflow and it currently fails for three reasons
unrelated to these quick wins — `check-yaml` doesn't understand the
`!override` tag in `deploy/compose/docker-compose.edge.yml`,
`detect-private-key` false-positives on `install.sh`'s own
`openssl ecparam ... | grep -q "BEGIN EC PRIVATE KEY"` self-test (not a
real key), and the `mypy` hook's isolated environment (only the packages
listed in `.pre-commit-config.yaml`'s `additional_dependencies`) reports 13
errors that `uv run mypy apps/gateway/src services` — the project's real
environment — does not. Wiring the full hook suite in now would make CI
red on day one for pre-existing, out-of-scope issues.
