# GitHub Actions workflows, parked

These belong in `.github/workflows/`. They are here because the token that
pushed branch `deploy/rearch` lacks the `workflow` scope, and GitHub refuses
any push that creates or edits a file under `.github/workflows/` without it.

To activate (with a token or SSH key that has the `workflow` scope):

    git mv deploy/ci/github-workflows/images.yml .github/workflows/images.yml
    git commit -m "Activate the image workflow" && git push

Nothing publishes until the repository variable `PUBLISH_IMAGES` is set to
`true` (decision D5 in the 2026-09-24 deployment re-architecture report).
