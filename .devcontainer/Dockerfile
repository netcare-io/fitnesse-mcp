# syntax=docker/dockerfile:1.7

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
	PYTHONUNBUFFERED=1 \
	VIRTUAL_ENV=/opt/venv \
	PATH="/opt/venv/bin:${PATH}"

WORKDIR /app

RUN python -m venv "$VIRTUAL_ENV" \
	&& "$VIRTUAL_ENV/bin/pip" install --no-cache-dir --upgrade pip uv

# Install deps from pyproject.toml; stub server.py so setuptools doesn't complain.
COPY pyproject.toml ./
RUN touch server.py \
	&& uv pip install --python "$VIRTUAL_ENV/bin/python" -e .

FROM base AS devcontainer

RUN apt-get update \
	&& apt-get install -y --no-install-recommends git ca-certificates curl dbus dbus-user-session libsecret-1-0 gnome-keyring \
	&& curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
	&& apt-get install -y --no-install-recommends nodejs \
	&& npm install -g @modelcontextprotocol/inspector@2.1.0 \
	&& rm -rf /var/lib/apt/lists/*

RUN cat <<'EOF' > /usr/local/bin/fastmcp-inspector && chmod +x /usr/local/bin/fastmcp-inspector
#!/usr/bin/env bash
set -euo pipefail

dbus-run-session -- bash -lc 'source /workspaces/fitnesse-mcp/.venv/bin/activate && HOST=127.0.0.1 fastmcp dev inspector server.py:mcp --ui-port 6274 --server-port 6277'
EOF

CMD ["bash"]

FROM base AS production

COPY . .

EXPOSE 8000

CMD ["fastmcp", "run", "server.py:mcp", "--transport", "http", "--host", "0.0.0.0", "--port", "8000"]
