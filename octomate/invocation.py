"""The prepared resources core lends an agent for one invocation."""

from dataclasses import dataclass

from pydantic import UUID7

from octomate.habitat import Habitat, ManagedHabitat
from octomate.managers.workspaces.base import Workspace


@dataclass(frozen=True, kw_only=True)
class Invocation:
    """Core-authorized resources, valid only inside the core's owning contexts.

    Core prepares the workspace and acquires any habitat before constructing this
    value. Adapters borrow them for the call; core owns their lifetime and the
    cleanup required on completion, failure, or cancellation.
    """

    id: UUID7  # Allocated by core before acquisition, shared with lifecycle events.
    conversation_id: UUID7
    workspace: Workspace
    execution: Habitat  # Prepared environment, owned by core for this call.

    def __post_init__(self) -> None:
        if isinstance(self.execution, ManagedHabitat):
            if self.owner_id is None:
                raise ValueError("Habitat execution requires a registered user")
            if self.execution.user_id != self.owner_id:
                raise ValueError("Habitat and workspace owners do not match")

    @property
    def owner_id(self) -> UUID7 | None:
        return self.workspace.owner_id

    @property
    def thread_id(self) -> UUID7:
        return self.workspace.thread_id
