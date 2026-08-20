#!/usr/bin/env bash
set -euo pipefail

# Cuts a release and publishes its Docker image in one step: runs
# release.sh (tests, version bump, git tag/push, GitHub release) followed
# by docker-build-and-publish.sh (build + push to Harbor) for the new version.
#
# Usage: scripts/release-and-publish.sh <new-version>
#   e.g. scripts/release-and-publish.sh 0.2.0

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ $# -ne 1 ]]; then
	echo "Usage: $0 <new-version>" >&2
	exit 1
fi
NEW_VERSION="$1"

"${ROOT_DIR}/scripts/release.sh" "$NEW_VERSION"
"${ROOT_DIR}/scripts/docker-build-and-publish.sh" "$NEW_VERSION"
