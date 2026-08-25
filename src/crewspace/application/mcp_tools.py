"""Policy-bound discovery and execution for external MCP tools."""
from __future__ import annotations

import datetime as dt
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from jsonschema import ValidationError
from jsonschema.validators import validator_for

from ..domain.entities import AgentToolCall, McpConnection
from ..domain.ports import ToolRunner, UnitOfWork
from .run_policy import RunPolicy, evaluate_action
from .tools import (
    Tool,
    ToolPermissionDenied,
    ToolRegistry,
    _bounded_audit_text,
)


class McpToolExecutor(Protocol):
    async def call_tool(
        self, connection: McpConnection, tool_name: str, arguments: dict[str, Any]
    ) -> Any: ...


class UnavailableMcpToolExecutor:
    async def call_tool(
        self, connection: McpConnection, tool_name: str, arguments: dict[str, Any]
    ) -> Any:
        raise RuntimeError("External MCP tool execution is not configured")


async def build_unavailable_mcp_executor() -> McpToolExecutor:
    return UnavailableMcpToolExecutor()


@dataclass
class AgentToolRuntime:
    tools: list[Tool]
    runner: ToolRunner


class _CompositeAgentToolRunner:
    def __init__(
        self,
        native_runner: ToolRunner,
        uow: UnitOfWork,
        *,
        principal_id: str | None,
        agent_id: str,
        external: dict[str, tuple[McpConnection, str]],
        executor: McpToolExecutor,
        policy: RunPolicy | None = None,
        run_id: str | None = None,
        event_recorder: Any | None = None,
        approval_decision: Literal["granted", "denied", "expired", "requested"] | None = None,
    ) -> None:
        self._native_runner = native_runner
        self._uow = uow
        self._principal_id = principal_id
        self._agent_id = agent_id
        self._external = external
        self._executor = executor
        # Optional run-scoped approval policy (M6.5). When None, external MCP
        # tools are governed only by the existing default-deny agent/MCP policy
        # (backward compatible). When set, a consequential external MCP action
        # must clear the run policy or it is blocked fail-closed and a canonical
        # `approval` (requested) event is recorded before any side effect.
        self._policy = policy
        self._run_id = run_id
        self._event_recorder = event_recorder
        self._approval_decision = approval_decision

    async def run(self, tool_name: str, **args: Any) -> Any:
        target = self._external.get(tool_name)
        if target is None:
            return await self._native_runner.run(tool_name, **args)

        connection, unqualified_name = target
        started = time.monotonic()
        call = AgentToolCall(
            id=f"atc_{uuid.uuid4().hex}",
            agent_id=self._agent_id,
            initiator_id=self._principal_id,
            provider_type="mcp",
            provider_id=connection.id,
            tool_name=tool_name,
            status="allowed",
            arguments_redacted=_bounded_audit_text(args),
            created_at=dt.datetime.now(dt.timezone.utc),
        )
        self._uow.queue_agent_tool_call(call)

        # M6.5 run-scoped approval checkpoint: emit the canonical `approval`
        # (requested) event BEFORE any external side effect, and block the
        # action fail-closed unless the run policy resolves to granted.
        if self._policy is not None:
            checkpoint = evaluate_action(
                self._policy,
                action_class="external_mcp",
                run_id=self._run_id or "",
                principal_id=self._principal_id,
                approved_for={"external_mcp"} if "external_mcp" in self._policy._allowed else set(),
                prior_decision=self._approval_decision,
            )
            if self._event_recorder is not None:
                self._event_recorder(checkpoint.event)
            if not checkpoint.allowed:
                call.status = "blocked"
                call.error = "Run policy requires approval for external MCP action"
                call.duration_ms = int((time.monotonic() - started) * 1000)
                raise ToolPermissionDenied(
                    f"External MCP tool {tool_name!r} requires run approval"
                )

        try:
            active_connection = await self._uow.mcp_connections.get(connection.id)
            active_tool = await self._uow.mcp_connections.get_discovered_tool(
                connection.id, unqualified_name
            )
            grants = await self._uow.agent_policies.list_enabled_mcp_tools(
                self._agent_id
            )
            principal = (
                await self._uow.auth.get_member(self._principal_id)
                if self._principal_id else None
            )
            if (
                principal is None
                or principal["kind"] != "human"
                or
                active_connection is None
                or not active_connection.enabled
                or active_tool is None
                or active_tool.approval_state != "approved"
                or (connection.id, unqualified_name) not in grants
            ):
                call.status = "blocked"
                call.error = "MCP tool is not currently allowed"
                raise ToolPermissionDenied(
                    f"Tool {tool_name!r} is not allowed for agent {self._agent_id!r}"
                )
            encoded_arguments = json.dumps(
                args, separators=(",", ":"), ensure_ascii=False
            ).encode()
            if len(encoded_arguments) > 64_000:
                raise ValueError("MCP tool arguments are too large")
            try:
                validator_for(active_tool.input_schema)(
                    active_tool.input_schema
                ).validate(args)
            except ValidationError as exc:
                raise ValueError("MCP tool arguments do not match the approved schema") from exc
            try:
                result = await self._executor.call_tool(
                    active_connection, unqualified_name, args
                )
            except Exception as exc:
                raise RuntimeError("External MCP tool execution failed") from exc
        except ToolPermissionDenied:
            call.duration_ms = int((time.monotonic() - started) * 1000)
            raise
        except BaseException as exc:
            call.status = "failed"
            call.duration_ms = int((time.monotonic() - started) * 1000)
            call.error = _bounded_audit_text(f"{type(exc).__name__}: {exc}")
            raise
        call.status = "succeeded"
        call.duration_ms = int((time.monotonic() - started) * 1000)
        call.result_summary = _bounded_audit_text(result)
        return result


async def build_agent_tool_runtime(
    native_registry: ToolRegistry,
    uow: UnitOfWork,
    *,
    principal_id: str | None,
    agent_id: str,
    executor: McpToolExecutor,
    policy: RunPolicy | None = None,
    run_id: str | None = None,
    event_recorder: Any | None = None,
    approval_decision: Literal["granted", "denied", "expired", "requested"] | None = None,
) -> AgentToolRuntime:
    native_grants = await uow.agent_policies.list_enabled_native_tools(agent_id)
    native_tools = [
        tool for tool in native_registry.list_tools() if tool.name in native_grants
    ]
    native_runner = native_registry.bind(
        uow,
        principal_id=principal_id,
        agent_id=agent_id,
        allowed_tools=native_grants,
    )
    enabled_external, all_external = await _load_external_catalog(uow, agent_id)
    return AgentToolRuntime(
        tools=[*native_tools, *enabled_external],
        runner=_CompositeAgentToolRunner(
            native_runner,
            uow,
            principal_id=principal_id,
            agent_id=agent_id,
            external=all_external,
            executor=executor,
            policy=policy,
            run_id=run_id,
            event_recorder=event_recorder,
            approval_decision=approval_decision,
        ),
    )


async def list_effective_agent_tools(
    native_registry: ToolRegistry, uow: UnitOfWork, agent_id: str,
) -> list[Tool]:
    native_grants = await uow.agent_policies.list_enabled_native_tools(agent_id)
    native_tools = [
        tool for tool in native_registry.list_tools() if tool.name in native_grants
    ]
    enabled_external, _ = await _load_external_catalog(uow, agent_id)
    return [*native_tools, *enabled_external]


async def _load_external_catalog(
    uow: UnitOfWork, agent_id: str,
) -> tuple[list[Tool], dict[str, tuple[McpConnection, str]]]:
    grants = await uow.agent_policies.list_enabled_mcp_tools(agent_id)
    all_external: dict[str, tuple[McpConnection, str]] = {}
    enabled_external: list[Tool] = []
    for connection in await uow.mcp_connections.list_connections():
        for discovered in await uow.mcp_connections.list_discovered_tools(connection.id):
            qualified_name = f"{connection.namespace}.{discovered.tool_name}"
            all_external[qualified_name] = (connection, discovered.tool_name)
            if (
                connection.enabled
                and discovered.approval_state == "approved"
                and (connection.id, discovered.tool_name) in grants
            ):
                enabled_external.append(
                    Tool(
                        name=qualified_name,
                        description=discovered.description,
                        input_schema=discovered.input_schema,
                        handler=_unreachable_external_handler,
                        provider=connection.id,
                        category="external",
                        mutability="write",
                        risk="high",
                    )
                )
    return enabled_external, all_external


async def _unreachable_external_handler(*args: Any, **kwargs: Any) -> Any:
    raise RuntimeError("External MCP tools must execute through the composite runner")
