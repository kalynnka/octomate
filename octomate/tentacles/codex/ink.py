"""Codex client lifecycle, discovery and native thread operations."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import replace
from functools import partial
from pathlib import Path

import anyio
from httpx import URL
from octomate_protocol.gateway import GatewayTool, gateway_tool
from openai_codex import AsyncThread, AsyncTurnHandle, InputItem
from openai_codex._approval_mode import _approval_mode_settings
from openai_codex._inputs import _normalize_run_input, _to_wire_input
from openai_codex._sandbox import _sandbox_mode, _sandbox_policy
from openai_codex.api import ApprovalMode, Sandbox
from openai_codex.errors import CodexError
from openai_codex.generated.v2_all import (
    ApprovalsReviewer,
    AskForApproval,
    AskForApprovalValue,
    CollaborationMode,
    CommandGuardianApprovalReviewAction,
    ConfigReadResponse,
    ExecveGuardianApprovalReviewAction,
    GuardianApprovalReviewStatus,
    IdleThreadStatus,
    ItemGuardianApprovalReviewCompletedNotification,
    ListMcpServerStatusResponse,
    ModeKind,
    Model,
    ModelListResponse,
    NetworkAccessGuardianApprovalReviewAction,
    NotLoadedThreadStatus,
    Personality,
    ReasoningEffort,
    ReasoningSummary,
    ReviewTarget,
    Settings,
    SkillsListEntry,
    SkillsListResponse,
    ThreadApproveGuardianDeniedActionResponse,
    ThreadClosedNotification,
    ThreadForkParams,
    ThreadResumeParams,
    ThreadStartParams,
    ThreadUnsubscribeResponse,
    TurnStartParams,
)
from pydantic import SecretStr, TypeAdapter
from pydantic.alias_generators import to_snake

from octomate.config.agents import CodexConfig
from octomate.mcp.gateway import CONVERSATION_HEADER
from octomate.telemetry import octomate_trace_environment
from octomate.tentacles.codex.adapter import json_object_adapter
from octomate.tentacles.codex.client import RequestHandler, SharedCodex
from octomate.tentacles.codex.schemas import (
    CodexModelCatalog,
    ThreadSettingsUpdateResponse,
)
from octomate.types.json import JsonObject
from octomate.utils import acquire_in_thread

logger = logging.getLogger(__name__)

DRIVEN_MCP_SERVER_NAME = "octomate_driven"
DRIVEN_CONFIG_OVERRIDES = (
    "features.hooks=false",
    "features.plugins=false",
    "features.apps=false",
    "notify=[]",
)


class CodexInk:
    """Own one app-server client and isolate SDK operations from the tentacle.

    The tentacle supplies approval handling and a catalog invalidation callback;
    the ink owns the process, notification reader and native thread operations.
    Conversation lookup and persistence remain with the tentacle.
    """

    client: SharedCodex
    agent_id: str
    mcp_url: URL
    notification_task: asyncio.Task[None] | None
    # TODO: Read applied MCP identity from the SDK when thread configuration is exposed.
    thread_bindings: dict[
        str, tuple[uuid.UUID, uuid.UUID | None]
    ]  # Conversation/key IDs.

    def __init__(
        self,
        config: CodexConfig,
        *,
        agent_id: str,
        mcp_url: URL,
        handler: RequestHandler,
    ) -> None:
        self.agent_id = agent_id
        self.mcp_url = mcp_url
        self.notification_task = None
        self.thread_bindings = {}
        env = dict(config.runtime.env or {})
        overrides = (*config.runtime.config_overrides, *DRIVEN_CONFIG_OVERRIDES)
        if config.instrument:
            trace_environment = octomate_trace_environment()
            if trace_environment is not None:
                env.update(trace_environment.as_env())
                overrides += (
                    "otel.trace_exporter.otlp-http.endpoint="
                    + json.dumps(trace_environment.endpoint),
                    'otel.trace_exporter.otlp-http.protocol="binary"',
                )
        self.client = SharedCodex(
            config=replace(config.runtime, env=env, config_overrides=overrides),
            handler=handler,
            instrument=config.instrument,
        )

    @property
    def running(self) -> bool:
        return self.notification_task is not None and not self.notification_task.done()

    async def start(self, *, invalidate_commands: Callable[[str], None]) -> None:
        starting = asyncio.create_task(self.client.__aenter__())
        try:
            await asyncio.shield(starting)
        except BaseException:
            with anyio.CancelScope(shield=True):
                await asyncio.gather(starting, return_exceptions=True)
                await self.close()
            raise
        self.notification_task = asyncio.create_task(
            self.watch_runtime(invalidate_commands)
        )

    async def close(self) -> None:
        # Closing the transport releases the SDK's blocking notification read.
        notification_task, self.notification_task = self.notification_task, None
        await self.client.close()
        if notification_task is not None:
            await notification_task

    async def watch_runtime(self, invalidate_commands: Callable[[str], None]) -> None:
        """Invalidate catalogs and forget bindings for closed native threads."""
        try:
            while True:
                notification = await self.client._client.next_notification()
                if notification.method == "skills/changed":
                    invalidate_commands(self.agent_id)
                if isinstance(notification.payload, ThreadClosedNotification):
                    self.thread_bindings.pop(notification.payload.thread_id, None)
                    invalidate_commands(self.agent_id)
        except CodexError:
            if self.notification_task is not None:
                logger.warning("Codex runtime disconnected", exc_info=True)
        finally:
            self.thread_bindings.clear()
            invalidate_commands(self.agent_id)

    async def skills(self, cwd: Path) -> SkillsListEntry:
        """Rescan native skills for one existing workspace."""
        response = await self.client._client.request(
            "skills/list",
            {"cwds": [str(cwd)], "forceReload": True},
            response_model=SkillsListResponse,
        )
        (entry,) = response.data
        if Path(entry.cwd) != cwd:
            raise ValueError("Codex returned skills for another workspace")
        return entry

    async def models(self) -> CodexModelCatalog:
        """Read the configured provider and all advertised, visible models."""
        settings = await self.client._client.request(
            "config/read",
            {"includeLayers": False},
            response_model=ConfigReadResponse,
        )
        provider = settings.config.model_provider or "openai"
        models: list[Model] = []
        cursor: str | None = None
        while True:
            page = await self.client._client.request(
                "model/list",
                {"includeHidden": False, "cursor": cursor},
                response_model=ModelListResponse,
            )
            models.extend(model for model in page.data if not model.hidden)
            cursor = page.next_cursor
            if cursor is None:
                break
        if not models:
            raise ValueError("Codex advertised no available models")
        default_model = settings.config.model or next(
            (model.model for model in models if model.is_default), None
        )
        return CodexModelCatalog(
            provider=provider,
            models=models,
            default_model=default_model,
            configured_effort=settings.config.model_reasoning_effort,
        )

    async def open_thread(
        self,
        *,
        thread_id: str | None,
        conversation_id: uuid.UUID,
        api_key_id: uuid.UUID | None,
        mcp_bearer: SecretStr | None,
        cwd: str,
        approval_mode: ApprovalMode | None,
        base_instructions: str | None,
        developer_instructions: str | None,
        ephemeral: bool | None,
        model: str | None,
        model_provider: str | None,
        personality: Personality | None,
        sandbox: Sandbox,
    ) -> tuple[AsyncThread, str | None]:
        """Read native liveness and apply the calling conversation's MCP identity."""
        if not self.running:
            raise RuntimeError("Codex runtime is not running")
        binding = (conversation_id, api_key_id)
        if thread_id is not None:
            thread = AsyncThread(self.client, thread_id)
            metadata = await thread.read(include_turns=False)
            loaded = not isinstance(metadata.thread.status.root, NotLoadedThreadStatus)
            # TODO: Use stable runtime authentication and resolve the current caller
            # and conversation in Octomate so thread reuse depends only on thread_id.
            if loaded and self.thread_bindings.get(thread_id) == binding:
                return thread, model or metadata.thread.model
            if loaded and not isinstance(metadata.thread.status.root, IdleThreadStatus):
                raise RuntimeError("Codex thread must be idle to change MCP identity")
            # An interrupted reconfiguration must not leave the old identity trusted.
            self.thread_bindings.pop(thread_id, None)
            if loaded:
                # Resume reapplies overrides to an idle thread without subscribers.
                await self.client._client.request(
                    "thread/unsubscribe",
                    {"threadId": thread_id},
                    response_model=ThreadUnsubscribeResponse,
                )
        thread_config = await self.thread_config(cwd, mcp_bearer, conversation_id)
        if thread_id is not None:
            thread, model_name = await self.resume_thread(
                thread_id=thread_id,
                approval_mode=approval_mode,
                base_instructions=base_instructions,
                config=thread_config,
                cwd=cwd,
                developer_instructions=developer_instructions,
                model=model,
                model_provider=model_provider,
                personality=personality,
                sandbox=sandbox,
            )
        else:
            thread, model_name = await self.start_thread(
                approval_mode=approval_mode,
                base_instructions=base_instructions,
                config=thread_config,
                cwd=cwd,
                developer_instructions=developer_instructions,
                ephemeral=ephemeral,
                model=model,
                model_provider=model_provider,
                personality=personality,
                sandbox=sandbox,
            )
        self.thread_bindings[thread.id] = binding
        return thread, model_name

    async def thread_config(
        self,
        cwd: str,
        mcp_bearer: SecretStr | None,
        conversation_id: uuid.UUID | None,
    ) -> JsonObject:
        settings = await self.client._client.request(
            "config/read",
            {"cwd": cwd, "includeLayers": False},
            response_model=ConfigReadResponse,
        )
        local_servers = TypeAdapter(dict[str, JsonObject]).validate_python(
            (settings.config.model_extra or {}).get("mcp_servers", {})
        )
        # Codex merges tables recursively; an empty map does not clear local servers.
        servers: JsonObject = {name: {"enabled": False} for name in local_servers}
        if mcp_bearer is None:
            return {"mcp_servers": servers}
        if DRIVEN_MCP_SERVER_NAME in local_servers:
            raise ValueError(
                f"MCP server name {DRIVEN_MCP_SERVER_NAME!r} is reserved for driven sessions"
            )
        # Keep the caller connection separate so no local transport or auth merges in.
        servers[DRIVEN_MCP_SERVER_NAME] = {
            "enabled": True,
            "url": str(self.mcp_url),
            "http_headers": {
                "Authorization": f"Bearer {mcp_bearer.get_secret_value()}",
                CONVERSATION_HEADER: str(conversation_id),
            },
            "tools": {gateway_tool(GatewayTool.TELEPORT): {"approval_mode": "prompt"}},
        }
        return {"mcp_servers": servers}

    async def start_thread(
        self,
        *,
        approval_mode: ApprovalMode | None,
        base_instructions: str | None,
        config: JsonObject,
        cwd: str | None,
        developer_instructions: str | None,
        ephemeral: bool | None,
        model: str | None,
        model_provider: str | None,
        personality: Personality | None,
        sandbox: Sandbox,
    ) -> tuple[AsyncThread, str]:
        approval_policy, reviewer = (
            _approval_mode_settings(approval_mode)
            if approval_mode is not None
            else (
                AskForApproval(root=AskForApprovalValue.on_request),
                ApprovalsReviewer.user,
            )
        )
        await self.client._ensure_initialized()
        started = await self.client._client.thread_start(
            ThreadStartParams(
                approval_policy=approval_policy,
                approvals_reviewer=reviewer,
                base_instructions=base_instructions,
                config=config,
                cwd=cwd,
                developer_instructions=developer_instructions,
                ephemeral=ephemeral,
                model=model,
                model_provider=model_provider,
                personality=personality,
                sandbox=_sandbox_mode(sandbox),
            )
        )
        return AsyncThread(self.client, started.thread.id), started.model

    async def resume_thread(
        self,
        *,
        thread_id: str,
        approval_mode: ApprovalMode | None,
        base_instructions: str | None,
        config: JsonObject,
        cwd: str | None,
        developer_instructions: str | None,
        model: str | None,
        model_provider: str | None,
        personality: Personality | None,
        sandbox: Sandbox,
    ) -> tuple[AsyncThread, str]:
        approval_policy, reviewer = (
            _approval_mode_settings(approval_mode)
            if approval_mode is not None
            else (
                AskForApproval(root=AskForApprovalValue.on_request),
                ApprovalsReviewer.user,
            )
        )
        await self.client._ensure_initialized()
        resumed = await self.client._client.thread_resume(
            thread_id,
            ThreadResumeParams(
                thread_id=thread_id,
                approval_policy=approval_policy,
                approvals_reviewer=reviewer,
                base_instructions=base_instructions,
                config=config,
                cwd=cwd,
                developer_instructions=developer_instructions,
                model=model,
                model_provider=model_provider,
                personality=personality,
                sandbox=_sandbox_mode(sandbox),
            ),
        )
        return AsyncThread(self.client, resumed.thread.id), resumed.model

    async def fork_thread(
        self, thread_id: str, *, cwd: Path, last_turn_id: str | None = None
    ) -> str:
        """Fork stored native history without carrying the source's MCP identity."""
        config = await self.thread_config(str(cwd), None, None)
        forked = await self.client._client.thread_fork(
            thread_id,
            ThreadForkParams(
                thread_id=thread_id,
                cwd=str(cwd),
                config=config,
                ephemeral=False,
                last_turn_id=last_turn_id,
            ),
        )
        if forked.thread.id == thread_id:
            raise ValueError("Codex fork returned the source thread id")
        return forked.thread.id

    async def approve_denied_action(
        self, thread_id: str, denial: ItemGuardianApprovalReviewCompletedNotification
    ) -> None:
        """Record an explicit approval; the caller starts the subsequent retry turn.

        This RPC accepts a legacy GuardianAssessmentEvent, whose action tags and
        field names use snake_case rather than the v2 notification's camelCase.
        """
        if (
            denial.thread_id != thread_id
            or denial.review.status != GuardianApprovalReviewStatus.denied
        ):
            raise ValueError("Approval requires a denial from this native thread.")
        action = json_object_adapter.validate_python(
            denial.action.model_dump(mode="json", by_alias=False, exclude_none=True)
        )
        action["type"] = to_snake(denial.action.root.type)
        native_action = denial.action.root
        if isinstance(
            native_action,
            CommandGuardianApprovalReviewAction | ExecveGuardianApprovalReviewAction,
        ):
            action["source"] = native_action.source.name
        if isinstance(native_action, NetworkAccessGuardianApprovalReviewAction):
            action["protocol"] = native_action.protocol.name
        await self.client._client.request(
            "thread/approveGuardianDeniedAction",
            {
                "threadId": thread_id,
                "event": {
                    "id": denial.review_id,
                    "turn_id": denial.turn_id,
                    "status": "denied",
                    "action": action,
                },
            },
            response_model=ThreadApproveGuardianDeniedActionResponse,
        )

    async def set_permission_mode(
        self,
        thread_id: str,
        *,
        conversation_id: uuid.UUID,
        approval_mode: ApprovalMode | None,
        sandbox: Sandbox,
    ) -> None:
        """Queue native permissions for a loaded thread owned by this conversation.

        thread/settings/update is experimental and available in the bundled
        runtime, but absent from the SDK's generated methods. It accepts changes
        during an active turn without starting or interrupting work; the active
        turn retains its policy and later turns use the updated settings.
        Unbound threads receive their settings when opened for the next run.
        """
        binding = self.thread_bindings.get(thread_id)
        if binding is None or binding[0] != conversation_id:
            return
        approval_policy, reviewer = (
            _approval_mode_settings(approval_mode)
            if approval_mode is not None
            else (
                AskForApproval(root=AskForApprovalValue.on_request),
                ApprovalsReviewer.user,
            )
        )
        sandbox_policy = _sandbox_policy(sandbox)
        await self.client._client.request(
            "thread/settings/update",
            {
                "threadId": thread_id,
                "approvalPolicy": approval_policy.model_dump(mode="json"),
                "approvalsReviewer": reviewer.value if reviewer is not None else None,
                "sandboxPolicy": sandbox_policy.model_dump(mode="json", by_alias=True)
                if sandbox_policy is not None
                else None,
            },
            response_model=ThreadSettingsUpdateResponse,
        )

    async def set_plan_mode(
        self,
        thread_id: str,
        *,
        enabled: bool,
        model: str,
        effort: ReasoningEffort | None,
    ) -> None:
        """Queue the native collaboration preset without submitting a turn.

        Null developer instructions select Codex's own preset. Model and effort
        are explicit because collaborationMode replaces that complete setting.
        """
        mode = CollaborationMode(
            mode=ModeKind.plan if enabled else ModeKind.default,
            settings=Settings(model=model, reasoning_effort=effort),
        )
        await self.client._client.request(
            "thread/settings/update",
            {
                "threadId": thread_id,
                "collaborationMode": mode.model_dump(mode="json"),
            },
            response_model=ThreadSettingsUpdateResponse,
        )

    async def mcp_status(self, thread_id: str) -> str:
        """Inspect only this thread's MCP connections, including paginated servers."""
        lines: list[str] = []
        cursor: str | None = None
        while True:
            page = await self.client._client.request(
                "mcpServerStatus/list",
                {"threadId": thread_id, "cursor": cursor},
                response_model=ListMcpServerStatusResponse,
            )
            lines.extend(
                f"{server.name}: {len(server.tools)} tools, "
                f"{len(server.resources)} resources; auth {server.auth_status.value}"
                for server in page.data
            )
            cursor = page.next_cursor
            if cursor is None:
                return "\n".join(lines) or "No MCP servers are connected."

    async def start_command(
        self, thread_id: str, review: ReviewTarget | None = None
    ) -> AsyncTurnHandle:
        """Retain command-start events and release an abandoned SDK subscription."""
        started = await acquire_in_thread(
            partial(self.client.transport.start_command, thread_id, review),
            discard=lambda started: started.subscription.close(),
        )
        return AsyncTurnHandle(
            self.client, thread_id, started.id, _subscription=started.subscription
        )

    async def start_turn(
        self,
        thread: AsyncThread,
        prompt: str | list[InputItem],
        *,
        approval_mode: ApprovalMode | None,
        sandbox: Sandbox,
        cwd: str,
        effort: ReasoningEffort | None,
        model: str | None,
        output_schema: JsonObject | None,
        personality: Personality | None,
        summary: ReasoningSummary | None,
        review: ReviewTarget | None = None,
    ) -> AsyncTurnHandle:
        # Apply both axes every turn, including on warm threads after a mode change.
        if review is not None:
            policy, reviewer = (
                _approval_mode_settings(approval_mode)
                if approval_mode is not None
                else (
                    AskForApproval(root=AskForApprovalValue.on_request),
                    ApprovalsReviewer.user,
                )
            )
            sandbox_policy = _sandbox_policy(sandbox)
            # review/start inherits thread settings instead of accepting turn overrides.
            await self.client._client.request(
                "thread/settings/update",
                {
                    "threadId": thread.id,
                    "cwd": cwd,
                    "approvalPolicy": policy.model_dump(mode="json"),
                    "approvalsReviewer": reviewer.value if reviewer else None,
                    "sandboxPolicy": sandbox_policy.model_dump(
                        mode="json", by_alias=True
                    )
                    if sandbox_policy is not None
                    else None,
                    "model": model,
                    "effort": effort.value if effort is not None else None,
                    "personality": personality.value
                    if personality is not None
                    else None,
                    "summary": summary.model_dump(mode="json")
                    if summary is not None
                    else None,
                },
                response_model=ThreadSettingsUpdateResponse,
            )
            return await self.start_command(thread.id, review)
        if approval_mode is not None:
            return await thread.turn(
                prompt,
                approval_mode=approval_mode,
                sandbox=sandbox,
                cwd=cwd,
                effort=effort,
                model=model,
                output_schema=output_schema,
                personality=personality,
                summary=summary,
            )
        # The public SDK cannot reset an auto reviewer back to the user.
        inputs = _to_wire_input(_normalize_run_input(prompt))
        turn, subscription = await self.client._client._start_turn(
            thread.id,
            inputs,
            params=TurnStartParams.model_validate(
                {
                    "thread_id": thread.id,
                    "input": inputs,
                    "approval_policy": AskForApproval(
                        root=AskForApprovalValue.on_request
                    ),
                    "approvals_reviewer": ApprovalsReviewer.user,
                    "sandbox_policy": _sandbox_policy(sandbox),
                    "cwd": cwd,
                    "effort": effort,
                    "model": model,
                    "output_schema": output_schema,
                    "personality": personality,
                    "summary": summary,
                }
            ),
            for_handle=True,
        )
        return AsyncTurnHandle(
            self.client, thread.id, turn.turn.id, _subscription=subscription
        )

    async def thread_name(self, thread: AsyncThread) -> str | None:
        """Read native metadata without loading the conversation history."""
        try:
            metadata = await thread.read(include_turns=False)
        except (CodexError, OSError):
            logger.warning(
                "Codex session name lookup failed for %s", thread.id, exc_info=True
            )
            return None
        return metadata.thread.name
