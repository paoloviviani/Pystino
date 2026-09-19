#!/usr/bin/env bash
#
# Tear the stack down.
#
# The overlay list is the error-prone part of doing this by hand: `down` only
# removes what the `-f` files it was given describe, so a forgotten overlay
# leaves its containers behind as orphans that the next `up` then fights with.
#
# So this script does not use compose at all. It works from the labels Docker
# already put on the containers, which is the one description guaranteed to
# match what is actually running. Passing every overlay was tried first and is
# worse, not better: `docker-compose.keycloak.yml` interpolates a variable this
# deployment never sets, so the superset refuses to parse and the teardown
# fails at the moment you least want it to.
#
#   ./deploy/teardown.sh                 # containers, network, named volumes
#   ./deploy/teardown.sh --backup        # save deploy/.env + profiles first
#   ./deploy/teardown.sh --images        # also remove the images we built
#   ./deploy/teardown.sh --all --yes     # backup + images, no prompt
#
# Deliberately not a flag here: wiping every volume on the host. That is
#   docker volume prune -af
# and it belongs in your hands, not behind an option in a per-project teardown
# where it would one day run on a shared box.
#
# What dies with the named volumes, so it is never a surprise:
#   postgres-data    every user account and password, the ledger, grants
#   chat-mongo-data  conversations, attachments, skills, connectors
#   caddy-data       the local CA — browsers will warn again after a rebuild
#   valkey-data      quota counters (they rebuild from the ledger, which is
#                    also gone, so they rebuild as zero)
#
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
env_file="$here/.env"
project="llm-platform"

do_backup=false do_images=false assume_yes=false
for arg in "$@"; do
	case "$arg" in
	--backup) do_backup=true ;;
	--images) do_images=true ;;
	--all) do_backup=true; do_images=true ;;
	--yes | -y) assume_yes=true ;;
	--help | -h) sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
	*) echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
	esac
done

containers="$(docker ps -aq --filter "label=com.docker.compose.project=$project" || true)"
vols="$(docker volume ls -q --filter "name=^${project}_" || true)"

echo "project:    $project"
echo "containers: $([ -n "$containers" ] && echo "$containers" | wc -l || echo 0)"
echo "volumes:    $(echo "$vols" | tr '\n' ' ')"
$do_images && echo "images:   will be removed"

if ! $assume_yes; then
	printf 'This destroys data. Type the project name to continue: '
	read -r answer
	[ "$answer" = "$project" ] || { echo "aborted."; exit 1; }
fi

if $do_backup; then
	stamp="$(date +%Y%m%d-%H%M%S)"
	dest="$HOME/pystino-backup-$stamp"
	mkdir -p "$dest/profiles"
	# deploy/.env is gitignored and exists nowhere else. CHAT_SECRET_KEY in it
	# encrypts MCP connector credentials at rest: lose it and restoring the
	# database still leaves those unreadable.
	[ -f "$env_file" ] && cp "$env_file" "$dest/env"
	cp "$here"/profiles/*.env "$dest/profiles/" 2>/dev/null || true
	if docker ps --format '{{.Names}}' | grep -q "^${project}-chat-mongo-1$"; then
		docker exec "${project}-chat-mongo-1" mongodump --archive > "$dest/chat-mongo.archive" 2>/dev/null \
			&& echo "  mongo dumped" || echo "  mongo dump skipped (container not healthy)"
	fi
	if docker ps --format '{{.Names}}' | grep -q "^${project}-postgres-1$"; then
		docker exec "${project}-postgres-1" pg_dumpall -U "${POSTGRES_USER:-gateway}" > "$dest/postgres.sql" 2>/dev/null \
			&& echo "  postgres dumped" || echo "  postgres dump skipped (check POSTGRES_USER)"
	fi
	echo "backup:   $dest"
fi

# Re-read rather than reusing the list from before the prompt: the backup step
# can take minutes, and a container that appeared meanwhile is still ours.
containers="$(docker ps -aq --filter "label=com.docker.compose.project=$project" || true)"
if [ -n "$containers" ]; then
	echo "==> stopping"
	# Stop before kill. The volumes are about to go either way, but a database
	# killed mid-write makes the *backup* taken a minute ago the only clean
	# copy, and that is a bad thing to discover later.
	echo "$containers" | xargs -r docker stop -t 20 >/dev/null
	echo "==> removing containers (-v takes their anonymous volumes too)"
	echo "$containers" | xargs -r docker rm -fv >/dev/null
fi

echo "==> removing named volumes"
docker volume ls -q --filter "name=^${project}_" | xargs -r docker volume rm >/dev/null

echo "==> removing networks"
docker network ls -q --filter "name=^${project}_" | xargs -r docker network rm >/dev/null 2>&1 || true

if $do_images; then
	echo "==> removing built images"
	docker images --format '{{.Repository}}:{{.Tag}}' \
		| grep -E "^${project}-" \
		| xargs -r docker rmi -f
fi

echo
echo "remaining (all three should be empty):"
echo "  containers: $(docker ps -aq --filter "label=com.docker.compose.project=$project" | wc -l)"
echo "  volumes:    $(docker volume ls -q --filter "name=^${project}_" | wc -l)"
echo "  networks:   $(docker network ls -q --filter "name=^${project}_" | wc -l)"
