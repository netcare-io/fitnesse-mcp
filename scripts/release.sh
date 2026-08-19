#!/usr/bin/env bash
set -euo pipefail

# Cuts a release: bumps pyproject.toml, tags git, builds+pushes the production
# Docker image to the internal Harbor registry, and (if `gh` is available)
# opens a GitHub release. Run from a clean working tree on main.
#
# Usage: scripts/release.sh <new-version>
#   e.g. scripts/release.sh 0.2.0

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

REGISTRY="${FITNESSE_MCP_REGISTRY:-harbor.netcare.local}"
IMAGE="${REGISTRY}/fitnesse-mcp"

if [[ $# -ne 1 ]]; then
	echo "Usage: $0 <new-version>" >&2
	exit 1
fi
NEW_VERSION="$1"
TAG="v${NEW_VERSION}"

if [[ ! "$NEW_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
	echo "Version must be plain semver (e.g. 0.2.0), got: $NEW_VERSION" >&2
	exit 1
fi

if [[ -n "$(git status --porcelain)" ]]; then
	echo "Working tree is not clean; commit or stash first." >&2
	exit 1
fi

CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [[ "$CURRENT_BRANCH" != "main" ]]; then
	echo "Refusing to release from branch '$CURRENT_BRANCH' (expected main)." >&2
	exit 1
fi

if git rev-parse "$TAG" >/dev/null 2>&1; then
	echo "Tag $TAG already exists." >&2
	exit 1
fi

echo "==> Running tests"
python -m pytest

echo "==> Bumping version to $NEW_VERSION in pyproject.toml"
sed -i.bak -E "s/^version = \".*\"/version = \"${NEW_VERSION}\"/" pyproject.toml
rm -f pyproject.toml.bak

git add pyproject.toml
git commit -m "Release ${TAG}"
git tag -a "$TAG" -m "Release ${TAG}"

echo "==> Building production image"
docker build --target production -t "${IMAGE}:${NEW_VERSION}" -t "${IMAGE}:latest" \
	-f .devcontainer/Dockerfile .

echo "==> Pushing to ${REGISTRY}"
docker push "${IMAGE}:${NEW_VERSION}"
docker push "${IMAGE}:latest"

echo "==> Pushing git commit and tag"
git push origin "$CURRENT_BRANCH"
git push origin "$TAG"

if command -v gh >/dev/null 2>&1; then
	echo "==> Creating GitHub release"
	gh release create "$TAG" --title "$TAG" --generate-notes
else
	echo "==> gh CLI not found; create the GitHub release manually:"
	echo "    https://github.com/netcare-io/fitnesse-mcp/releases/new?tag=${TAG}"
fi

echo "==> Done: ${TAG} tagged, pushed, and image published as ${IMAGE}:${NEW_VERSION}"
