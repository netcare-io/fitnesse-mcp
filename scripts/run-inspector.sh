#!/usr/bin/env bash
set -euo pipefail

# Container-safe launcher for fastmcp-inspector.
# Keeps keyring/DBus warnings from root devcontainer sessions to a minimum.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
mkdir -p "$XDG_RUNTIME_DIR"
chmod 700 "$XDG_RUNTIME_DIR" || true

cd "$ROOT_DIR"

# Use FastMCP's inspector wrapper for correct server launch semantics.
if [[ "${INSPECTOR_RAW_LOGS:-0}" == "1" ]]; then
	exec dbus-run-session -- fastmcp-inspector "$@"
fi

# Hide known non-fatal keyring chatter common in root devcontainers.
dbus-run-session -- fastmcp-inspector "$@" 2>&1 | sed -E \
	'/org\.freedesktop\.secrets|keyring\/control|discover_other_daemon: 0/d'
