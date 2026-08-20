#!/usr/bin/env bash
set -euo pipefail

# Builds and pushes the production Docker image for the currently checked
# out source tree to the internal Harbor registry. Run this after checking
# out the tag/commit you want to publish (see scripts/release.sh to cut a
# release first, or scripts/release-and-publish.sh to do both in one step).
#
# Usage: scripts/docker-build-and-publish.sh [version]
#   Defaults to the version currently set in pyproject.toml.
#   e.g. scripts/docker-build-and-publish.sh 0.2.0

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

REGISTRY="${FITNESSE_MCP_REGISTRY:-harbor.netcare.local}"
IMAGE="${REGISTRY}/kdta/fitnesse-mcp"

VERSION="${1:-$(sed -nE 's/^version = "(.*)"/\1/p' pyproject.toml)}"
if [[ -z "$VERSION" ]]; then
	echo "Could not determine version from pyproject.toml; pass it explicitly." >&2
	echo "Usage: $0 [version]" >&2
	exit 1
fi
TAG="v${VERSION}"

HEAD_TAG="$(git describe --tags --exact-match HEAD 2>/dev/null || true)"
if [[ -z "$HEAD_TAG" ]]; then
	echo "Warning: HEAD is not tagged; building from an untagged commit." >&2
elif [[ "$HEAD_TAG" != "$TAG" ]]; then
	echo "Warning: HEAD is tagged '${HEAD_TAG}', not '${TAG}'." >&2
fi

echo "==> Building production image ${IMAGE}:${VERSION}"
docker build --target production -t "${IMAGE}:${VERSION}" -t "${IMAGE}:latest" \
	-f .devcontainer/Dockerfile .

echo "==> Pushing to ${REGISTRY}"
docker push "${IMAGE}:${VERSION}"
docker push "${IMAGE}:latest"

echo "==> Done: image published as ${IMAGE}:${VERSION} (and :latest)"
