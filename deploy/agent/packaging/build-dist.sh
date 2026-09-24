#!/bin/sh
# Build pystino-agent for the four targets people run it on, into one
# directory with REVISION and SHA256SUMS — what to hand to users until there is
# a published release. Static binaries (CGO off), no runtime dependency beyond
# opencode itself.
#
#   deploy/agent/packaging/build-dist.sh [OUT_DIR]     (default ./pystino-agent-dist)
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)
out=${1:-pystino-agent-dist}
mkdir -p "$out"; out=$(cd "$out" && pwd)
rev=$(git -C "$here" rev-parse --short HEAD)
if [ -n "$(git -C "$here" status --porcelain --untracked-files=no -- .)" ]; then rev="$rev-dirty"; fi
cd "$here"
for target in linux/amd64 linux/arm64 darwin/amd64 darwin/arm64; do
    os=${target%/*}; arch=${target#*/}
    CGO_ENABLED=0 GOOS=$os GOARCH=$arch go build -trimpath -ldflags='-s -w' \
        -o "$out/pystino-agent-$os-$arch" .
done
cp packaging/pystino-agent.service packaging/org.pystino.agent.plist "$out/"
echo "built from Pystino $rev" > "$out/REVISION"
(cd "$out" && sha256sum pystino-agent-* > SHA256SUMS)
echo "wrote $out ($rev)"
