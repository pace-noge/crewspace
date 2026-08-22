"""Channel-scoped workflow validation, filtering, and ordered execution."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from croniter import croniter

from ..domain.entities import Workflow, WorkflowRun, WorkflowRunStatus
from ..domain.identifiers import BUILTIN_ASSISTANT_ID
from ..domain.ports import UnitOfWork
logger = logging.getLogger(__name__)

TRIGGER_TYPES = {"message_posted", "reaction_added", "diff_posted", "webhook", "schedule"}
ACTION_TYPES = {
    "send_message", "send_dm", "call_webhook", "call_mcp_tool", "request_approval",
    "add_reaction", "set_channel_topic", "delay",
}
UTC = dt.timezone.utc


class WorkflowWebhookExecutor(Protocol):
    async def call(
        self, *, url: str, method: str, body: Any, headers: dict[str, str],
        timeout_seconds: int,
    ) -> dict[str, Any]: ...


class WorkflowMcpToolExecutor(Protocol):
    async def call_tool(
        self, connection: Any, tool_name: str, arguments: dict[str, Any],
    ) -> Any: ...


def interval_seconds(value: str) -> int:
    match = re.fullmatch(r"\s*(\d+)\s*([smhd])\s*", value, re.IGNORECASE)
    if not match:
        raise ValueError("Interval must look like 30m, 1h, or 2d")
    amount = int(match.group(1))
    seconds = amount * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2).lower()]
    if seconds < 1:
        raise ValueError("Interval must be at least one second")
    return seconds


def render_template(value: str, context: dict[str, Any]) -> str:
    def replace(match: re.Match[str]) -> str:
        current: Any = context
        for part in match.group(1).strip().split("."):
            current = current.get(part, "") if isinstance(current, dict) else ""
        return str(current)
    return re.sub(r"{{\s*([^{}]+?)\s*}}", replace, value)


def _render_value(value: Any, context: dict[str, Any]) -> Any:
    if isinstance(value, str):
        return render_template(value, context)
    if isinstance(value, dict):
        return {str(key): _render_value(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [_render_value(item, context) for item in value]
    return value


def matches_filter(expression: str | None, event: dict[str, Any]) -> bool:
    if not expression or not expression.strip():
        return True
    value = expression.strip()
    match = re.fullmatch(r'contains\(([a-zA-Z_][\w.]*),\s*"([^"]*)"\)', value)
    if match:
        current: Any = event
        for part in match.group(1).split("."):
            current = current.get(part) if isinstance(current, dict) else None
        return match.group(2) in str(current or "")
    match = re.fullmatch(r'([a-zA-Z_][\w.]*)\s*(==|!=)\s*"([^"]*)"', value)
    if match:
        current: Any = event
        for part in match.group(1).split("."):
            current = current.get(part) if isinstance(current, dict) else None
        return (str(current or "") == match.group(3)) == (match.group(2) == "==")
    raise ValueError("Invalid filter expression. Use contains(field, \"value\"), ==, or !=")


def matches_step_condition(expression: str | None, event: dict[str, Any]) -> bool:
    if not expression or not expression.strip():
        return True
    normalized = re.sub(r"\btrigger_([a-zA-Z_]\w*)\b", r"\1", expression.strip())
    normalized = normalized.replace("str_contains(", "contains(", 1)
    return matches_filter(normalized, event)


class WorkflowService:
    def __init__(
        self, on_message: Callable[[Any], Awaitable[None]] | None = None,
        webhook_executor: WorkflowWebhookExecutor | None = None,
        mcp_executor: WorkflowMcpToolExecutor | None = None,
        on_progress: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        self._on_message = on_message
        self._webhook_executor = webhook_executor
        self._mcp_executor = mcp_executor
        self._on_progress = on_progress

    async def _emit_progress(
        self, run: WorkflowRun, *, step_index: int, step_id: str, action: str,
        status: str,
    ) -> None:
        if self._on_progress is None:
            return
        event = {
            "type": "workflow_run_progress",
            "run_id": run.id,
            "workflow_id": run.workflow_id,
            "channel_id": run.event.get("channel_id") if run.event else None,
            "step_index": step_index,
            "step_id": step_id,
            "action": action,
            "status": status,
            "current_step": run.current_step,
            "run_status": run.status.value,
        }
        try:
            await self._on_progress(event)
        except Exception:
            logger.exception("Workflow progress listener failed")

    async def _require_mcp_step_authorization(self, uow: UnitOfWork, owner_id: str) -> None:
        owner = await uow.auth.get_member(owner_id)
        if owner is None or owner["role"] != "superadmin":
            raise PermissionError("Only a superadmin may use external MCP tools in workflows")

    async def create(self, uow: UnitOfWork, *, creator_id: str, data: dict[str, Any]) -> Workflow:
        name = str(data.get("name", "")).strip()
        channel_id = str(data.get("channel_id", "")).strip()
        trigger_type = str(data.get("trigger_type", "")).strip()
        steps = data.get("steps") or []
        if any(step.get("action") == "call_mcp_tool" for step in steps):
            await self._require_mcp_step_authorization(uow, creator_id)
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise ValueError("Name must use lowercase letters, numbers, and underscores")
        if await uow.channels.get_channel(channel_id) is None:
            raise ValueError("Channel not found")
        if trigger_type not in TRIGGER_TYPES:
            raise ValueError("Unknown trigger type")
        matches_filter(data.get("filter_expression"), {})
        if not steps:
            raise ValueError("Add at least one step")
        seen: set[str] = set()
        for step in steps:
            step_id = str(step.get("id", "")).strip()
            if not step_id or step_id in seen:
                raise ValueError("Step IDs must be present and unique")
            seen.add(step_id)
            if step.get("action") not in ACTION_TYPES:
                raise ValueError(f"Unknown action: {step.get('action')}")
            timeout = step.get("timeout_seconds")
            if timeout is not None and int(timeout) < 1:
                raise ValueError("Step timeout must be positive")
            matches_step_condition(step.get("condition"), {})
        workflow = Workflow(
            id=f"wf_{uuid.uuid4().hex[:12]}", name=name,
            description=(str(data.get("description", "")).strip() or None),
            channel_id=channel_id, enabled=bool(data.get("enabled", True)),
            trigger_type=trigger_type, trigger_config=data.get("trigger_config") or {},
            filter_expression=(
                str(data["filter_expression"]).strip()
                if data.get("filter_expression") is not None
                and str(data["filter_expression"]).strip()
                else None
            ),
            steps=steps, creator_id=creator_id,
        )
        if trigger_type == "schedule":
            cron = str(workflow.trigger_config.get("cron", "")).strip()
            interval = str(workflow.trigger_config.get("interval", "")).strip()
            legacy_every = workflow.trigger_config.get("every_seconds")
            if bool(cron) == bool(interval) and legacy_every is None:
                raise ValueError("Provide either a cron expression or a simple interval")
            if cron:
                if len(cron.split()) != 5:
                    raise ValueError("Cron expression must contain five fields")
                if not croniter.is_valid(cron):
                    raise ValueError("Invalid cron expression")
            every = interval_seconds(interval) if interval else int(legacy_every or 0)
            if not cron and every < 1:
                raise ValueError("Provide either a cron expression or a simple interval")
            if cron:
                workflow.trigger_config = {"cron": cron}
                workflow.next_run_at = croniter(cron, dt.datetime.now(UTC)).get_next(dt.datetime)
            elif interval:
                workflow.trigger_config = {"interval": interval, "every_seconds": every}
                workflow.next_run_at = dt.datetime.now(UTC) + dt.timedelta(seconds=every)
            else:
                workflow.next_run_at = dt.datetime.now(UTC) + dt.timedelta(seconds=every)
        return await uow.workflows.create(workflow)

    async def update(
        self, uow: UnitOfWork, *, workflow: Workflow, data: dict[str, Any]
    ) -> Workflow:
        name = str(data.get("name", "")).strip()
        channel_id = str(data.get("channel_id", "")).strip()
        trigger_type = str(data.get("trigger_type", "")).strip()
        steps = data.get("steps") or []
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise ValueError("Name must use lowercase letters, numbers, and underscores")
        if await uow.channels.get_channel(channel_id) is None:
            raise ValueError("Channel not found")
        if trigger_type not in TRIGGER_TYPES:
            raise ValueError("Unknown trigger type")
        matches_filter(data.get("filter_expression"), {})
        if not steps:
            raise ValueError("Add at least one step")
        seen: set[str] = set()
        for step in steps:
            step_id = str(step.get("id", "")).strip()
            if not step_id or step_id in seen:
                raise ValueError("Step IDs must be present and unique")
            seen.add(step_id)
            if step.get("action") not in ACTION_TYPES:
                raise ValueError(f"Unknown action: {step.get('action')}")
            timeout = step.get("timeout_seconds")
            if timeout is not None and int(timeout) < 1:
                raise ValueError("Step timeout must be positive")
            matches_step_condition(step.get("condition"), {})

        if any(step.get("action") == "call_mcp_tool" for step in steps):
            await self._require_mcp_step_authorization(uow, workflow.creator_id)

        workflow.name = name
        workflow.description = str(data.get("description", "")).strip() or None
        workflow.channel_id = channel_id
        workflow.enabled = bool(data.get("enabled", workflow.enabled))
        workflow.trigger_type = trigger_type
        workflow.trigger_config = data.get("trigger_config") or {}
        workflow.filter_expression = (
            str(data["filter_expression"]).strip()
            if data.get("filter_expression") is not None
            and str(data["filter_expression"]).strip()
            else None
        )
        workflow.steps = steps
        workflow.next_run_at = None
        if trigger_type == "schedule":
            cron = str(workflow.trigger_config.get("cron", "")).strip()
            interval = str(workflow.trigger_config.get("interval", "")).strip()
            legacy_every = workflow.trigger_config.get("every_seconds")
            if bool(cron) == bool(interval) and legacy_every is None:
                raise ValueError("Provide either a cron expression or a simple interval")
            if cron:
                if len(cron.split()) != 5 or not croniter.is_valid(cron):
                    raise ValueError("Invalid cron expression")
                workflow.trigger_config = {"cron": cron}
                workflow.next_run_at = croniter(
                    cron, dt.datetime.now(UTC)
                ).get_next(dt.datetime)
            else:
                every = interval_seconds(interval) if interval else int(legacy_every or 0)
                if every < 1:
                    raise ValueError("Provide either a cron expression or a simple interval")
                workflow.trigger_config = (
                    {"interval": interval, "every_seconds": every}
                    if interval else {"every_seconds": every}
                )
                workflow.next_run_at = dt.datetime.now(UTC) + dt.timedelta(seconds=every)
        return await uow.workflows.update(workflow)

    async def dispatch(self, uow: UnitOfWork, *, channel_id: str, trigger_type: str,
                       event: dict[str, Any]) -> list[WorkflowRun]:
        runs = []
        scoped_event = {**event, "channel_id": channel_id}
        for workflow in await uow.workflows.list_enabled(channel_id, trigger_type):
            if not matches_filter(workflow.filter_expression, scoped_event):
                continue
            runs.append(await self.run(workflow, uow, scoped_event))
        return runs

    async def run(self, workflow: Workflow, uow: UnitOfWork, event: dict[str, Any],
                  *, start_step: int = 0, existing_run: WorkflowRun | None = None,
                  trigger_type: str | None = None,
                  context_results: list[dict[str, Any]] | None = None) -> WorkflowRun:
        now = dt.datetime.now(UTC)
        run = existing_run or WorkflowRun(
            id=f"wfr_{uuid.uuid4().hex[:12]}", workflow_id=workflow.id,
            trigger_type=trigger_type or workflow.trigger_type, event=event,
            status=WorkflowRunStatus.RUNNING,
            current_step=start_step, step_results=[], started_at=now,
        )
        if existing_run is None:
            await uow.workflows.start_run(run)
        await uow.commit()
        context = {
            **event,
            "event": event,
            "trigger": event,
            "workflow": {"id": workflow.id, "name": workflow.name},
        }
        for result in context_results or []:
            if result.get("status") == "succeeded":
                context[result["step_id"]] = result.get("output")
        try:
            for index in range(start_step, len(workflow.steps)):
                step = workflow.steps[index]
                if not matches_step_condition(step.get("condition"), event):
                    run.current_step = index + 1
                    run.step_results.append({
                        "step_id": step["id"], "status": "skipped",
                        "output": {"condition": step.get("condition")},
                    })
                    await uow.workflows.update_run(run)
                    await uow.commit()
                    await self._emit_progress(
                        run, step_index=index, step_id=step["id"],
                        action=step.get("action", ""), status="skipped",
                    )
                    continue
                timeout = int(step.get("timeout_seconds") or 300)
                await self._emit_progress(
                    run, step_index=index, step_id=step["id"],
                    action=step.get("action", ""), status="started",
                )
                try:
                    output = await asyncio.wait_for(
                        self._execute_action(workflow, step, run, uow, context),
                        timeout=timeout,
                    )
                except Exception as step_exc:
                    run.current_step = index
                    run.step_results.append({
                        "step_id": step["id"], "status": "failed",
                        "output": {"error": str(step_exc)},
                    })
                    await uow.workflows.update_run(run)
                    await uow.commit()
                    await self._emit_progress(
                        run, step_index=index, step_id=step["id"],
                        action=step.get("action", ""), status="failed",
                    )
                    raise
                run.current_step = index + 1
                run.step_results.append({"step_id": step["id"], "status": "succeeded", "output": output})
                context[step["id"]] = output
                await uow.workflows.update_run(run)
                await uow.commit()
                await self._emit_progress(
                    run, step_index=index, step_id=step["id"],
                    action=step.get("action", ""), status="succeeded",
                )
                if run.status == WorkflowRunStatus.WAITING:
                    await self._emit_progress(
                        run, step_index=run.current_step, step_id="", action="",
                        status="waiting",
                    )
                    return run
            run.status = WorkflowRunStatus.SUCCEEDED
            run.finished_at = dt.datetime.now(UTC)
        except Exception as exc:
            run.status = WorkflowRunStatus.FAILED
            run.error = str(exc)
            run.finished_at = dt.datetime.now(UTC)
        await uow.workflows.update_run(run)
        await uow.commit()
        await self._emit_progress(
            run, step_index=run.current_step, step_id="", action="",
            status="completed",
        )
        return run

    async def retry_failed(
        self, workflow: Workflow, failed: WorkflowRun, uow: UnitOfWork,
        *, initiated_by: str,
    ) -> WorkflowRun:
        if failed.workflow_id != workflow.id:
            raise ValueError("Workflow run not found")
        if failed.status != WorkflowRunStatus.FAILED:
            raise ValueError("Only failed workflow runs can be retried")
        if failed.current_step >= len(workflow.steps):
            raise ValueError("Failed step no longer exists in the workflow definition")
        root_run_id = failed.root_run_id or failed.id
        related = await uow.workflows.list_runs(workflow.id)
        lineage = sorted(
            (
                run for run in related
                if run.id == root_run_id or run.root_run_id == root_run_id
            ),
            key=lambda run: run.attempt,
        )
        attempt = max((run.attempt for run in lineage), default=1) + 1
        context_results = [
            result
            for ancestor in lineage
            if ancestor.attempt <= failed.attempt
            for result in ancestor.step_results
            if result.get("status") == "succeeded"
        ]
        retry = WorkflowRun(
            id=f"wfr_{uuid.uuid4().hex[:12]}",
            workflow_id=workflow.id,
            trigger_type=failed.trigger_type,
            event=failed.event,
            status=WorkflowRunStatus.RUNNING,
            current_step=failed.current_step,
            step_results=[],
            started_at=dt.datetime.now(UTC),
            parent_run_id=failed.id,
            root_run_id=root_run_id,
            attempt=attempt,
            retry_initiated_by=initiated_by,
        )
        await uow.workflows.start_run(retry)
        return await self.run(
            workflow,
            uow,
            failed.event,
            start_step=failed.current_step,
            existing_run=retry,
            context_results=context_results,
        )

    async def _execute_action(self, workflow: Workflow, step: dict, run: WorkflowRun,
                              uow: UnitOfWork, context: dict[str, Any]) -> dict[str, Any]:
        action = step["action"]
        config = step.get("config") or {}
        if action == "send_message":
            channel_id = config.get("channel_id") or workflow.channel_id
            text = render_template(str(config.get("text", "")), context).strip()
            if not text:
                raise ValueError("Send Message requires text")
            message = await uow.chat.add_message(channel_id, BUILTIN_ASSISTANT_ID, text)
            if self._on_message is not None:
                await uow.commit()
                await self._on_message(message)
            return {"message_id": message.id, "channel_id": channel_id, "text": text}
        if action == "send_dm":
            member_id = str(config.get("member_id", "")).strip()
            if await uow.auth.get_member(member_id) is None:
                raise ValueError("Send DM member not found")
            channel = await uow.channels.get_or_create_direct(BUILTIN_ASSISTANT_ID, member_id)
            text = render_template(str(config.get("text", "")), context).strip()
            message = await uow.chat.add_message(channel.id, BUILTIN_ASSISTANT_ID, text)
            if self._on_message is not None:
                await uow.commit()
                await self._on_message(message)
            return {"message_id": message.id, "channel_id": channel.id, "text": text}
        if action == "add_reaction":
            message_id = render_template(str(config.get("message_id", "")), context)
            emoji = render_template(str(config.get("emoji", "")), context)
            await uow.chat.toggle_reaction(message_id, workflow.creator_id, emoji)
            return {"message_id": message_id, "emoji": emoji}
        if action == "set_channel_topic":
            channel = await uow.channels.get_channel(config.get("channel_id") or workflow.channel_id)
            if channel is None:
                raise ValueError("Channel not found")
            channel.topic = render_template(str(config.get("topic", "")), context)
            await uow.channels.update_channel(channel)
            return {"channel_id": channel.id, "topic": channel.topic}
        if action == "delay":
            seconds = float(config.get("seconds", 0))
            if seconds < 0 or seconds > 86400:
                raise ValueError("Delay must be between 0 and 86400 seconds")
            await asyncio.sleep(seconds)
            return {"seconds": seconds}
        if action == "request_approval":
            run.status = WorkflowRunStatus.WAITING
            run.approval_token = uuid.uuid4().hex
            return {"prompt": render_template(str(config.get("prompt", "Approval required")), context)}
        if action == "call_mcp_tool":
            await self._require_mcp_step_authorization(uow, workflow.creator_id)
            connection_id = render_template(
                str(config.get("connection_id", "")), context
            ).strip()
            tool_name = render_template(
                str(config.get("tool_name", "")), context
            ).strip()
            if not connection_id or not tool_name:
                raise ValueError("Call MCP Tool requires connection and tool names")
            connection = await uow.mcp_connections.get(connection_id)
            if connection is None:
                raise ValueError("MCP connection not found")
            if not connection.enabled:
                raise ValueError("MCP connection is disabled")
            tool = await uow.mcp_connections.get_discovered_tool(
                connection.id, tool_name
            )
            if tool is None or tool.approval_state != "approved":
                raise PermissionError("MCP tool is not approved")
            if self._mcp_executor is None:
                raise RuntimeError("Workflow MCP tool transport is unavailable")
            arguments = _render_value(config.get("arguments") or {}, context)
            if not isinstance(arguments, dict):
                raise ValueError("MCP tool arguments must be a JSON object")
            result = await self._mcp_executor.call_tool(
                connection, tool_name, arguments
            )
            return {
                "connection_id": connection.id,
                "tool_name": tool_name,
                "result": result,
            }
        if action == "call_webhook":
            if self._webhook_executor is None:
                raise RuntimeError("Workflow webhook transport is unavailable")
            url = render_template(str(config.get("url", "")), context)
            method = str(config.get("method", "POST")).upper()
            body = _render_value(config.get("body", context), context)
            headers = _render_value(config.get("headers") or {}, context)
            return await self._webhook_executor.call(
                url=url,
                method=method,
                body=body,
                headers=headers,
                timeout_seconds=int(step.get("timeout_seconds") or 30),
            )
        raise ValueError(f"Action {action} is not implemented")

    async def approve(self, token: str, uow: UnitOfWork, approved: bool) -> WorkflowRun:
        run = await uow.workflows.get_run_by_approval(token)
        if run is None or run.status != WorkflowRunStatus.WAITING:
            raise ValueError("Approval not found or already resolved")
        workflow = await uow.workflows.get(run.workflow_id)
        if workflow is None:
            raise ValueError("Workflow not found")
        run.approval_token = None
        if not approved:
            run.status = WorkflowRunStatus.FAILED
            run.error = "Approval rejected"
            run.finished_at = dt.datetime.now(UTC)
            await uow.workflows.update_run(run)
            await uow.commit()
            return run
        run.status = WorkflowRunStatus.RUNNING
        return await self.run(workflow, uow, run.event, start_step=run.current_step, existing_run=run)


class WorkflowSchedulerLoop:
    def __init__(
        self, db, poll_seconds: int = 30,
        on_message: Callable[[Any], Awaitable[None]] | None = None,
        webhook_executor: WorkflowWebhookExecutor | None = None,
        mcp_executor: WorkflowMcpToolExecutor | None = None,
        on_progress: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        self._db = db
        self._poll_seconds = poll_seconds
        self._on_message = on_message
        self._webhook_executor = webhook_executor
        self._mcp_executor = mcp_executor
        self._on_progress = on_progress
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if not self._task or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def run_due_once(self) -> int:
        now = dt.datetime.now(UTC)
        count = 0
        async with self._db.uow() as uow:
            workflows = await uow.workflows.claim_due_schedules(
                now,
                claim_token=uuid.uuid4().hex,
                claim_until=now + dt.timedelta(minutes=5),
            )
            await uow.commit()
            for workflow in workflows:
                await WorkflowService(
                    on_message=self._on_message,
                    on_progress=self._on_progress,
                    webhook_executor=self._webhook_executor,
                    mcp_executor=self._mcp_executor,
                ).run(
                    workflow, uow,
                    {"channel_id": workflow.channel_id, "scheduled_at": now.isoformat(), "text": now.isoformat()},
                )
                if workflow.trigger_config.get("cron"):
                    workflow.next_run_at = croniter(
                        workflow.trigger_config["cron"], now
                    ).get_next(dt.datetime)
                else:
                    every = int(workflow.trigger_config["every_seconds"])
                    workflow.next_run_at = now + dt.timedelta(seconds=every)
                await uow.workflows.update(workflow)
                await uow.commit()
                count += 1
        return count

    async def _run(self) -> None:
        while True:
            try:
                await self.run_due_once()
            finally:
                await asyncio.sleep(self._poll_seconds)
