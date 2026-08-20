#!/usr/bin/env bash
set -euo pipefail

# Cuts a release: runs tests, bumps pyproject.toml, tags git, pushes, and
# (if `gh` is available) opens a GitHub release. Run from a clean working
# tree on main. Does NOT build or publish the Docker image — see
# scripts/docker-build-and-publish.sh for that, or scripts/release-and-publish.sh to
# do both in one step.
#
# Usage: scripts/release.sh <new-version>
#   e.g. scripts/release.sh 0.2.0

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

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

echo "==> Running tests (in Docker; no local Python required)"
TEST_IMAGE="fitnesse-mcp-test:local"
docker build --target test -t "$TEST_IMAGE" -f .devcontainer/Dockerfile .
docker run --rm -v "${ROOT_DIR}:/app" -w /app "$TEST_IMAGE"

echo "==> Bumping version to $NEW_VERSION in pyproject.toml"
sed -i.bak -E "s/^version = \".*\"/version = \"${NEW_VERSION}\"/" pyproject.toml
rm -f pyproject.toml.bak

git add pyproject.toml
git commit -m "Release ${TAG}"
git tag -a "$TAG" -m "Release ${TAG}"

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

echo "==> Done: ${TAG} tagged and pushed."
echo "    Run scripts/docker-build-and-publish.sh to build and publish the Docker image."
