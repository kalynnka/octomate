# Product

<!-- impeccable:product-schema 1 -->

Scope: this file covers the web console (`octomate/web`) — the web surface of the
Octomate agent relay. Backend product truth lives in the repository's own docs.
Written by inference from the build brief and the repository (the brief delegated
detail decisions); inferred facts are marked [inferred].

## Platform

web

## Stack

pnpm + Vite + React + TypeScript, `@assistant-ui/react` for the agent chat
runtime (pinned by the brief). Delegated choices: React 19, TanStack Query for
server state, zustand for console UI state, no router (the console is one shell;
Control pages are in-shell states, matching the design). Plain CSS with design
tokens — no Tailwind — because the Lonetrail design system ships as CSS custom
properties and the console reproduces it directly.

## Users

Developers/operators running an Octomate instance (the design's example user is
`kalynnka`). They drive long-running agent sessions across channels (web, Slack,
Lark, Napcat, plus hook-ingested Claude Code / Codex sessions) and need one
mention-free surface to direct agents, approve writes, answer questions, review
plan files, and audit what happened. [inferred from the design's content]

## Product Purpose

Trunkline is the Octomate web console: the "entry channel" with zero relay
latency. It shows every thread from every channel, streams the live session
ledger (thinking, tools, approvals, questions, files), and exposes the Control
plane (agents, MCP, users, dashboard, settings) beside the chat — the chat is
primary; Control serves the chat. [inferred; matches the design's copy]

## Operating Context

Runs against the local Octomate FastAPI instance (design shows
`127.0.0.1:8000`; config layering `default.yaml → octomate.yaml → env`).
Threads may be filed to a project (a shared working tree with git state);
sessions are owned by one agent+model+effort and can be summoned/teleported
between agents and surfaces. Desktop-first, long-lived tab, both color schemes
(operators work day and night — theme toggle with `auto` default).

## Capabilities and Constraints

- Backend endpoints largely do not exist yet: the console is built against a
  typed API layer with mock adapters mirroring the backend's domain schemas, so
  real endpoints can replace mocks without UI changes. [constraint from brief]
- Domain vocabulary is binding: threads, sessions (entry/summon/teleport/
  ingest), ledger, directives, feelers (write approvals + asks), spills,
  dossiers/revs, verbs (scry · send · summon · commission · teleport).
- Chat renders live markdown and streams; history loads backwards on scroll.

## Brand Commitments

Product name **Octomate**; console codename **Trunkline**; visual world is the
**Lonetrail** design system from the user's Claude Design project (vendored in
`src/styles/` + `public/fonts/`) — cream archival paper, one burnt-orange
accent, hard corners, mono labels, teal·gold·red tri-stripe, functional-only
motion. The base look is pinned; motion and interaction details are delegated
to the implementer within Lonetrail's motion philosophy.

## Product Principles

1. The chat ledger is the product; every other panel exists to serve it.
2. Nothing decorative moves — motion is status, entry, or fold, nothing else.
3. Every fact on screen is an auditable record (ids, timestamps, token counts).
4. Operator control is explicit: approvals, answers, and routing are first-class
   UI, never buried in chat text.

## Accessibility & Inclusion

Honor `prefers-reduced-motion` (the design system already specifies it), keep
mono/label text above 4.5:1 contrast in both themes, and keep all approve /
answer / send controls keyboard-operable. [inferred baseline]
