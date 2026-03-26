FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY octomate/ octomate/
COPY octotools/ octotools/
COPY main.py octomate.default.yaml ./

CMD ["uv", "run", "python", "main.py"]
