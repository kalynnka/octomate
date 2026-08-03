FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends openssh-client && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY octomate/ octomate/
COPY main.py octomate.default.yaml ./

CMD ["uv", "run", "octomate", "serve"]
