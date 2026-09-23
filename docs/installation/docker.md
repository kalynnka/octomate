# Docker

The checkout ships a `Dockerfile` and a `docker-compose.yml`. Compose starts Octomate
and nothing else: the database is a SQLite file under `.octomate/`.

```sh
git clone --branch 'octomate-vX.Y.Z' --depth 1 https://github.com/kalynnka/octomate.git
cd octomate
mkdir -p .octomate/config
```

Write the config home at `.octomate/config/` and the secrets in `.env` exactly as on
any other host. Compose mounts the checkout at `/app` and starts the server there,
so `./.octomate/config/` is the config home and `.octomate/octomate.db` is the
database, both persisted on the host.

The container binds `0.0.0.0` through `OCTOMATE__HOST`, because loopback inside a
container is reachable from nowhere. Port 8000 is published. The compose file also
mounts `~/.ssh` read-only for git over SSH.

## Initialise and run

```sh
docker compose build
docker compose run --rm octomate uv run --no-sync alembic upgrade head
docker compose up -d
docker compose logs -f octomate
```

Issue the first invitation from inside the container:

```sh
docker compose exec octomate uv run --no-sync octomate service invite --url http://localhost:8000
```

Then continue with [Accounts and tokens](accounts.md).

## What fits in a container

Channels, Trunkline and Inkling run well in a container: they need network access
and provider API keys, nothing else. The harness-driven agents are harder. A driven
Claude Code or Codex run needs that harness installed and logged in inside the
container, and its login state is not in the image. Recording your own native
sessions is unaffected: the client and its transcript tail run on your machine and
talk to the container over HTTP.

The web console needs a build. Build it on the host and point `static_dir` at the
`dist` directory, which the bind mount makes visible at `/app/trunkline/dist`.

## QQ through NapCat

NapCat is a QQ account rather than infrastructure, so it sits behind a profile:

```sh
docker compose --profile qq up -d
```

It publishes NapCat's WebSocket, HTTP and web UI ports. Configure the `napcat`
channel with `ws_url: ws://napcat:3001` and `http_url: http://napcat:3000` so
Octomate reaches it on the compose network. See [QQ](../usage/channels/napcat.md).

## Upgrading

```sh
docker compose down
git fetch --tags && git checkout 'octomate-vX.Y.Z'
docker compose build
docker compose run --rm octomate uv run --no-sync alembic upgrade head
docker compose up -d
```

Back up `.octomate/octomate.db` before the migration. [Upgrades and
backups](upgrading.md)
explains how to rehearse one on a copy.
