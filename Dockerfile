FROM node:22-bookworm-slim AS node

FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates git openssh-client ripgrep \
    && rm -rf /var/lib/apt/lists/*

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

WORKDIR /app

COPY pyproject.toml uv.lock README.md LICENSE ./
COPY cli/ cli/
COPY protocol/ protocol/
COPY octomate/ octomate/
RUN uv sync --locked --no-default-groups \
    && .venv/bin/python -c 'from pathlib import Path; import claude_agent_sdk; from codex_cli_bin import bundled_codex_path; Path("/usr/local/bin/claude").symlink_to(Path(claude_agent_sdk.__file__).parent / "_bundled/claude"); Path("/usr/local/bin/codex").symlink_to(bundled_codex_path())'

ARG OCTOMATE_UID=1000
ARG OCTOMATE_GID=1000
RUN if ! getent group "$OCTOMATE_GID" >/dev/null; then groupadd --gid "$OCTOMATE_GID" octomate; fi \
    && useradd --create-home --uid "$OCTOMATE_UID" --gid "$OCTOMATE_GID" octomate \
    && mkdir /data && chown "$OCTOMATE_UID:$OCTOMATE_GID" /data
ENV PATH="/app/.venv/bin:$PATH" \
    HOME=/home/octomate \
    OCTOMATE_HOME=/data/config \
    OCTOMATE_DB_URL=sqlite+aiosqlite:////data/octomate.db
USER octomate
WORKDIR /data

CMD ["octomate", "service", "serve"]
