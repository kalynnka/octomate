# Plan: local SkillHub shared by all agent tentacles

> **Status:** proposed · **Scope:** one Octomate installation, many agent consumers

## Outcome

A skill is installed once into Octomate and becomes discoverable by every enabled agent
tentacle without separately maintaining copies for Inkling, Claude, and Codex.

## Feature requirements

1. Octomate stores one canonical package for each skill, including instructions,
   metadata, references, templates, scripts, and assets.
2. Agents can search skills by intent and read a selected skill progressively.
3. Skill discovery is available through a small, stable standard MCP surface attached
   to Inkling, Claude, and Codex.
4. Search results describe compatibility and required tools, permissions, channel
   features, and other dependencies.
5. Agents are instructed to read the selected skill before following it.
6. Referenced skill resources are readable without exposing arbitrary host filesystem
   paths.
7. Skill installation, update, disablement, and removal are Octomate control-plane
   operations and are auditable.
8. Availability can be limited by agent or channel-agent policy without copying the
   skill package.
9. Skill versions are identifiable so a run can record which instructions it used.

## Direction

- Start with a read-oriented MCP surface for search, skill reading, and resource reading.
- Index metadata and descriptions while keeping complete instructions and resources
  behind progressive reads.
- Keep skill execution in the agent's existing tools and sandbox; the hub distributes
  knowledge and resources rather than becoming a generic code-execution service.
- Treat provider-native skill folders or commands as optional generated projections of
  the canonical hub, not separately authored installations.
- Use the handoff skill as an early end-to-end consumer of the hub.

## Initial scope cut

- Included: local packages, discovery, reading, resource access, policy, and versioning.
- Postponed: public marketplace, remote publishing, automatic dependency installation,
  and arbitrary skill script execution by the hub.
- Not included: proxying all installed third-party MCP servers through Octomate.

## Acceptance

- Installing or updating one skill changes what all enabled agents discover without a
  duplicate provider-specific installation step.
- An agent can find a relevant skill from its description, read it completely, and load
  an allowed referenced resource.
- A run records the selected skill identity and version.

