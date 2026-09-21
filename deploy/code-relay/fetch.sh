#!/bin/sh
# Fetch the paseo-relay source this deployment builds its relay image from
# (deploy/compose/docker-compose.code-relay.yml). The commit is pinned — the
# relay's own README warns its internal protocol may change without notice,
# so the build context is a checkout of one verified commit, never a branch.
#
# Re-running is a no-op when the commit is already checked out: the installer
# calls this on every run, and an operator re-running the installer must not
# find a dirty tree (a `git fetch` + checkout of the same commit leaves the
# working tree untouched).
set -eu

COMMIT=3fc41c96c8c63f3a7109e832899cc57d473c4531
REPO=https://github.com/getpaseo/paseo-relay.git
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DEST="$HERE/paseo-relay"

if [ -d "$DEST/.git" ]; then
	# Already a checkout: verify the pin, fetch only when behind.
	CURRENT=$(git -C "$DEST" rev-parse HEAD 2>/dev/null || true)
	if [ "$CURRENT" = "$COMMIT" ]; then
		echo "relay source already at $COMMIT — nothing to do."
		exit 0
	fi
	echo "relay source is at $CURRENT, not the pinned $COMMIT — resetting." >&2
	git -C "$DEST" fetch origin "$COMMIT" --depth 1
	git -C "$DEST" checkout --detach "$COMMIT"
	exit 0
fi

if [ -e "$DEST" ]; then
	echo "refusing: $DEST exists and is not a git checkout" >&2
	exit 1
fi

mkdir -p "$DEST"
git clone "$REPO" "$DEST"
git -C "$DEST" checkout --detach "$COMMIT"
echo "relay source at $COMMIT."
