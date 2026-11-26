FROM node:24.22.3-bookworm-slim@sha256:873f586f3794f52281bd039bf02c048a0be08fde619338f9941bad1eb6b34530 AS node
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim@sha256:531f855bda2c73cd6ef67d56b733b357cea384185b3022bd09f05e002cd144ca AS python

FROM python

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates git openssh-client ripgrep bubblewrap sudo \
    && rm -rf /var/lib/apt/lists/*
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
COPY images/sandbox/package.json images/sandbox/package-lock.json /opt/dsh/
RUN --mount=type=cache,target=/root/.npm \
    ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && npm ci --prefix /opt/dsh --no-audit --no-fund

WORKDIR /opt/octomate
# Restricted Codex profiles can read /usr, including their own runtime helpers.
ENV UV_PROJECT_ENVIRONMENT=/usr/local/share/octomate
COPY pyproject.toml uv.lock ./
COPY cli/pyproject.toml cli/pyproject.toml
COPY protocol/pyproject.toml protocol/pyproject.toml
RUN --mount=type=cache,target=/root/.cache/uv \
    UV_HTTP_TIMEOUT=180 UV_CONCURRENT_DOWNLOADS=4 UV_LINK_MODE=copy \
    uv sync --locked --only-group sandbox --no-install-workspace
COPY cli cli
COPY protocol protocol
RUN --mount=type=cache,target=/root/.cache/uv \
    UV_LINK_MODE=copy uv sync --locked --only-group sandbox --no-editable
RUN ln -s /usr/local/share/octomate/lib/python3.13/site-packages/claude_agent_sdk/_bundled/claude /usr/local/bin/claude \
    && ln -s /usr/local/share/octomate/lib/python3.13/site-packages/codex_cli_bin/bin/codex /usr/local/bin/codex \
    && /usr/local/share/octomate/bin/python -VV > /opt/octomate/python-version.txt \
    && uv pip freeze --python /usr/local/share/octomate/bin/python > /opt/octomate/python-packages.txt \
    && dpkg-query -W > /opt/octomate/system-packages.txt \
    && npm list --prefix /opt/dsh --all --json > /opt/octomate/dsh-packages.json

RUN groupadd --gid 1000 agent \
    && useradd --create-home --uid 1000 --gid 1000 --shell /bin/bash agent \
    && mkdir -p /home/agent/workspace /home/agent/.claude \
    && chown -R agent:agent /home/agent \
    && printf 'agent ALL=(ALL) NOPASSWD:ALL\n' > /etc/sudoers.d/agent \
    && chmod 0440 /etc/sudoers.d/agent
ENV PATH="/usr/local/share/octomate/bin:/opt/dsh/node_modules/.bin:$PATH" \
    HOME=/home/agent
USER agent
WORKDIR /home/agent/workspace
CMD ["/bin/bash"]
