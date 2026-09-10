FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends openssh-client && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml uv.lock README.md LICENSE ./
COPY cli/ cli/
COPY protocol/ protocol/
COPY octomate/ octomate/
RUN uv sync --frozen --no-dev

CMD ["uv", "run", "--no-sync", "octomate", "service", "serve"]
