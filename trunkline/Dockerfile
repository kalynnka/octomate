FROM node:22-bookworm-slim AS build

WORKDIR /trunkline
RUN corepack enable && corepack prepare pnpm@10.11.0 --activate
COPY trunkline/package.json trunkline/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY trunkline/ ./
RUN pnpm build

FROM nginxinc/nginx-unprivileged:1.28-alpine

COPY trunkline/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /trunkline/dist /usr/share/nginx/html
