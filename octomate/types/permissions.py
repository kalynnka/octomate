from __future__ import annotations

from typing import Literal, TypeIs, get_args

from claude_agent_sdk import PermissionMode as ClaudePermissionMode

# The approval posture an agent works under. Each provider keeps its own vocabulary
# rather than sharing one: Claude has a single mode, Codex has three orthogonal SDK
# enums, and a scale borrowed from either says the wrong thing about the other.
#
# Here rather than on a schema because a project declares the posture its
# conversations start under, and a project cannot import the conversation that
# imports it.

# Claude's scale is the SDK's own `PermissionMode`, reused rather than restated, so the
# tentacle hands it over with no mapping table. Note this ties a stored value to the
# SDK: a release that drops a member makes an existing row fail validation.

# What Inkling implements, from Claude's scale:
#
# - `default`      park every deferral for a human, and wait
# - `dontAsk`      resolve questions in-process; the agent decides and says so
# - `bypassPermissions`  grant approvals without a card
#
# `plan` and `auto` are Claude Code behaviors with nothing to resolve to here.
# `acceptEdits` is absent because nothing distinguishes an edit: the harness scopes
# `FileSystem` and `Shell` with allow/deny lists rather than approval gates, so no
# Inkling tool sets `requires_approval` and there is no edit approval to accept.
InklingPermissionMode = Literal["default", "dontAsk", "bypassPermissions"]

# Codex's approval axis, and only that: who answers when the agent asks to step past
# its sandbox — the user, the SDK's own reviewer, or nobody. `CODEX_PERMISSION_PLANS`
# maps each onto the `AskForApprovalValue`/`ApprovalsReviewer` pair the SDK wants.
#
# The sandbox is deliberately not folded in. It is the enforcement boundary rather than
# the question of who is asked, it stays fixed for a run (`CodexConfig.sandbox`), and a
# posture that moved it would make one conversation's approval setting quietly rewrite
# what every command in that thread may touch.
CodexPermissionMode = Literal["user_review", "auto_review", "deny_all"]

# One status, whichever provider it came from. Which arm applies is decided by the
# row's `agent_tentacle_id`, not by a discriminator inside the value.
AgentPermissionMode = ClaudePermissionMode | CodexPermissionMode

# Which statuses each agent answers to, so a Codex posture on a Claude conversation is
# refused where it is written rather than where it is read. Derived from the literals
# above so there is one place to add a status.
PERMISSION_MODES: dict[str, frozenset[str]] = {
    "inkling": frozenset(get_args(InklingPermissionMode)),
    "claude": frozenset(get_args(ClaudePermissionMode)),
    "codex": frozenset(get_args(CodexPermissionMode)),
}


def check_mode(agent_tentacle_id: str, mode: AgentPermissionMode) -> None:
    """Raise unless `mode` is one `agent_tentacle_id` answers to.

    A posture only means something in the vocabulary of the agent that reads it, and
    the stored type is the union of every provider's scale, so this is the check that
    tells them apart. Both the project declaring a posture and the conversation
    carrying one run it, so a wrong-provider value is refused wherever it is written.

    An agent with no vocabulary registered is refused a posture rather than given a
    free one: nothing would read it.
    """
    allowed = PERMISSION_MODES.get(agent_tentacle_id)
    if allowed is None:
        raise ValueError(
            f"agent {agent_tentacle_id!r} has no permission modes, so it cannot be "
            f"given {mode!r}"
        )
    if mode not in allowed:
        raise ValueError(
            f"{mode!r} is not one of {agent_tentacle_id}'s modes "
            f"({'/'.join(sorted(allowed))})"
        )


# One stored column holds every provider's scale, so a tentacle reading it has to
# establish the arm is its own. `check_mode` already refuses a status its agent does
# not answer to, which makes these total in practice — they are how that invariant
# reaches the type checker, and what a tentacle falls back on if it ever does not hold.
def is_claude_mode(mode: AgentPermissionMode | None) -> TypeIs[ClaudePermissionMode]:
    return mode in PERMISSION_MODES["claude"]


def is_codex_mode(mode: AgentPermissionMode | None) -> TypeIs[CodexPermissionMode]:
    return mode in PERMISSION_MODES["codex"]
