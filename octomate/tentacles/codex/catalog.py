"""Known app/IDE commands, separate from dynamically discovered skills.

Codex implements these composer actions in its clients, not a commands/list RPC.
Sources: https://learn.chatgpt.com/docs/reference/slash-commands and
https://developers.openai.com/codex/ide/slash-commands.
"""

from octomate.schemas.commands import CommandDescriptor

# TODO: Inspect CLI-only commands separately; this catalog covers app/IDE actions.
APP_COMMANDS: frozenset[CommandDescriptor] = frozenset(
    CommandDescriptor(
        id=f"builtin:{name}",
        name=name,
        description=description,
        unavailable_reason=reason,
    )
    for name, description, reason in (
        ("cloud", "Run in the cloud.", "Cloud execution is not supported here."),
        (
            "cloud-environment",
            "Choose a cloud environment.",
            "Cloud environments are managed in Codex.",
        ),
        (
            "fast",
            "Toggle fast execution.",
            "Codex service-tier changes are not supported here yet.",
        ),
        ("feedback", "Send feedback to OpenAI.", "Use the feedback dialog in Codex."),
        (
            "ide-context",
            "Toggle automatic editor context.",
            "Editor context is managed in the Codex IDE extension.",
        ),
        (
            "local",
            "Run in a local workspace.",
            "Driven Codex runs already use the thread's workspace.",
        ),
        (
            "memories",
            "Configure memory use and generation.",
            "Codex memory settings are not supported here yet.",
        ),
        (
            "model",
            "Choose the conversation model.",
            "Use the agent/model picker when starting a conversation.",
        ),
        (
            "pet",
            "Show or hide the desktop pet.",
            "Desktop pets are managed in the Codex app.",
        ),
        (
            "personality",
            "Choose a response style.",
            "Codex personality changes are not supported here yet.",
        ),
        (
            "project",
            "Choose a project for a new conversation.",
            "Use Trunkline's project selector.",
        ),
        (
            "side",
            "Open a temporary side conversation.",
            "Side conversations are not supported here yet.",
        ),
        (
            "task",
            "Start a conversation without a project.",
            "Use /new to start a conversation.",
        ),
        (
            "worktree",
            "Run in a new Git worktree.",
            "Worktree creation is not supported as a command here yet.",
        ),
    )
) | frozenset(
    {
        CommandDescriptor(
            id="builtin:approve",
            name="approve",
            description="Approve a denial from the latest run and retry it.",
            argument_hint="[review-id]",
        ),
        CommandDescriptor(
            id="builtin:fork",
            name="fork",
            description="Copy this conversation into an independent thread.",
        ),
        CommandDescriptor(
            id="builtin:compact",
            name="compact",
            description="Compact the conversation context.",
        ),
        CommandDescriptor(
            id="builtin:review",
            name="review",
            description="Review uncommitted changes, or changes against a branch.",
            argument_hint="[branch]",
        ),
        CommandDescriptor(
            id="builtin:init",
            name="init",
            description="Create project instructions in AGENTS.md.",
        ),
        CommandDescriptor(
            id="builtin:mcp",
            name="mcp",
            description="Inspect connected MCP servers.",
        ),
        CommandDescriptor(
            id="builtin:plan",
            name="plan",
            description="Enter or leave planning mode.",
            argument_hint="[on|off]",
        ),
        CommandDescriptor(
            id="builtin:reasoning",
            name="reasoning",
            description="Choose the reasoning effort.",
            argument_hint="[level]",
        ),
        CommandDescriptor(
            id="builtin:status",
            name="status",
            description="Inspect the conversation and its current settings.",
            requires_conversation=False,
        ),
    }
)
