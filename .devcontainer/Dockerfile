FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
	PYTHONUNBUFFERED=1 \
	VIRTUAL_ENV=/opt/venv \
	PATH="/opt/venv/bin:${PATH}"

WORKDIR /app

# Pull in current Debian security fixes from apt repositories.
RUN apt-get update \
	&& apt-get upgrade -y \
	&& rm -rf /var/lib/apt/lists/*

RUN python -m venv "$VIRTUAL_ENV" \
	&& "$VIRTUAL_ENV/bin/pip" install --no-cache-dir --upgrade pip uv

# Install deps from pyproject.toml; stub server.py so setuptools doesn't complain.
COPY pyproject.toml LICENSE ./
RUN touch server.py \
	&& uv pip install --python "$VIRTUAL_ENV/bin/python" -e .

FROM base AS devcontainer

RUN useradd --create-home --uid 1000 --shell /bin/bash vscode \
	&& chown -R vscode:vscode /opt/venv

RUN mkdir -p /workspaces \
	&& ln -sfn /app /workspaces/fitnesse-mcp

RUN apt-get update \
	&& apt-get upgrade -y \
	&& apt-get install -y --no-install-recommends git ca-certificates curl dbus dbus-user-session libsecret-1-0 gnome-keyring gnupg \
	&& curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
	&& apt-get install -y --no-install-recommends nodejs \
	&& npm install -g @modelcontextprotocol/inspector@2.1.0 \
	&& curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
	&& chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
	&& echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list \
	&& apt-get update \
	&& apt-get install -y --no-install-recommends gh \
	&& apt-get clean \
	&& rm -rf /var/lib/apt/lists/*

# cwd is captured at runtime so the script works from any project directory.
RUN cat <<'EOF' > /usr/local/bin/fastmcp-inspector && chmod +x /usr/local/bin/fastmcp-inspector
#!/usr/bin/env bash
set -euo pipefail

_PROJECT_DIR="$(pwd)"
dbus-run-session -- bash -lc "
  cd \"$_PROJECT_DIR\"
  HOST=127.0.0.1 fastmcp dev inspector --ui-port 6274 --server-port 6277
"
EOF

CMD ["bash"]

FROM base AS production
RUN useradd --create-home --uid 1000 app
COPY --chown=app:app . .
USER app
EXPOSE 8000

CMD ["fastmcp", "run", "server.py:mcp"]
