#!/usr/bin/env bash
set -euo pipefail

# Cuts a release. main only accepts pull requests, so this takes two runs:
#
#   1. scripts/release.sh 1.0.0   bumps pyproject.toml on branch release/v1.0.0 and opens a PR
#   2. merge the PR once CI is green
#   3. scripts/release.sh 1.0.0   again, on main: tags v1.0.0 and pushes the tag
#
# The tag triggers .github/workflows/release.yml: tests, pushes the image to
# ghcr.io/netcare-io/fitnesse-mcp (1.0.0, 1.0, latest) and creates the GitHub
# release.
#
# Usage: scripts/release.sh <new-version>
#   e.g. scripts/release.sh 1.0.0

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

REPO_URL="https://github.com/netcare-io/fitnesse-mcp"

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

git fetch --quiet --tags origin main
if [[ "$(git rev-parse HEAD)" != "$(git rev-parse origin/main)" ]]; then
	echo "main is not in sync with origin/main; run git pull first." >&2
	exit 1
fi

if git rev-parse "$TAG" >/dev/null 2>&1; then
	echo "Tag $TAG already exists." >&2
	exit 1
fi

CURRENT_VERSION="$(sed -nE 's/^version = "(.*)"/\1/p' pyproject.toml)"

if [[ "$CURRENT_VERSION" != "$NEW_VERSION" ]]; then
	BRANCH="release/${TAG}"
	echo "==> Bumping version ${CURRENT_VERSION} -> ${NEW_VERSION} on ${BRANCH}"
	git switch --quiet -c "$BRANCH"
	sed -i.bak -E "s/^version = \".*\"/version = \"${NEW_VERSION}\"/" pyproject.toml
	rm -f pyproject.toml.bak
	git add pyproject.toml
	git commit --quiet -m "Release ${TAG}"
	git push --quiet -u origin "$BRANCH"

	if command -v gh >/dev/null 2>&1; then
		gh pr create --base main --head "$BRANCH" --title "Release ${TAG}" \
			--body "Bumps the version to ${NEW_VERSION}. After merging, run \`scripts/release.sh ${NEW_VERSION}\` on main to tag the release."
	else
		echo "==> gh CLI not found; open the PR manually:"
		echo "    ${REPO_URL}/compare/main...${BRANCH}?expand=1"
	fi
	git switch --quiet main

	echo "==> Merge the PR once CI is green, then run: git pull && $0 ${NEW_VERSION}"
	exit 0
fi

echo "==> Tagging ${TAG}"
git tag -a "$TAG" -m "Release ${TAG}"
git push --quiet origin "$TAG"

echo "==> Done: ${TAG} pushed. The release workflow builds and publishes the image:"
echo "    ${REPO_URL}/actions/workflows/release.yml"
