"""Human-friendly scheduling and execution of channel instructions."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import uuid

from ..config import Settings
from ..domain.entities import ScheduleKind, ScheduledJob, ScheduledJobRun
from ..domain.ports import UnitOfWork
from .access import can_manage_team
from .services import ChatService
from .tools import build_registry

UTC = dt.timezone.utc
logger = logging.getLogger(__name__)


def next_run_for(
    schedule_kind: str,
    *,
    now: dt.datetime | None = None,
    interval_value: int | None = None,
    interval_unit: str | None = None,
    daily_time: str | None = None,
    run_at: str | None = None,
) -> dt.datetime:
    now = now or dt.datetime.now(UTC)
    if schedule_kind == ScheduleKind.ONCE.value:
        if not run_at:
            raise ValueError("Choose when this one-time job should run")
        value = dt.datetime.fromisoformat(run_at)
        return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC)
    if schedule_kind == ScheduleKind.DAILY.value:
        if not daily_time:
            raise ValueError("Choose a daily time")
        hour, minute = map(int, daily_time.split(":"))
        value = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return value if value > now else value + dt.timedelta(days=1)
    if schedule_kind != ScheduleKind.INTERVAL.value:
        raise ValueError("Unknown schedule type")
    if not interval_value or interval_value < 1:
        raise ValueError("Interval must be at least 1")
    units = {"minutes": "minutes", "hours": "hours", "days": "days"}
    if interval_unit not in units:
        raise ValueError("Choose minutes, hours, or days")
    return now + dt.timedelta(**{units[interval_unit]: interval_value})


def schedule_label(job: ScheduledJob) -> str:
    if job.schedule_kind == ScheduleKind.ONCE:
        return f"Once on {job.next_run_at:%Y-%m-%d at %H:%M} UTC"
    if job.schedule_kind == ScheduleKind.DAILY:
        return f"Daily at {job.daily_time} UTC"
    unit = job.interval_unit or "minutes"
    if job.interval_value == 1:
        unit = unit.removesuffix("s")
    return f"Every {job.interval_value} {unit}"


async def channel_team_id(channel_id: str, uow: UnitOfWork) -> str | None:
    channel = await uow.channels.get_channel(channel_id)
    workspace = await uow.workspaces.get_workspace(channel.workspace_id) if channel else None
    return workspace.team_id if workspace else None


async def can_manage_channel_job(user: dict, channel_id: str, uow: UnitOfWork) -> bool:
    team_id = await channel_team_id(channel_id, uow)
    return bool(team_id and await can_manage_team(user, team_id, uow))


async def can_create_channel_job(user: dict, channel_id: str, uow: UnitOfWork) -> bool:
    return await can_manage_channel_job(user, channel_id, uow) or await uow.channels.can_member_access(
        channel_id, user["id"]
    )


async def can_access_job(user: dict, job: ScheduledJob, uow: UnitOfWork) -> bool:
    if await can_manage_channel_job(user, job.channel_id, uow):
        return True
    return job.creator_id == user["id"] and await uow.channels.can_member_access(
        job.channel_id, user["id"]
    )


class ScheduledJobService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def create(
        self,
        uow: UnitOfWork,
        *,
        name: str,
        channel_id: str,
        instruction: str,
        schedule_kind: str,
        creator_id: str,
        interval_value: int | None = None,
        interval_unit: str | None = None,
        daily_time: str | None = None,
        run_at: str | None = None,
        description: str | None = None,
    ) -> ScheduledJob:
        name = name.strip()
        if not name:
            raise ValueError("Name is required")
        instruction = instruction.strip()
        if not instruction:
            raise ValueError("Instruction is required")
        if await uow.channels.get_channel(channel_id) is None:
            raise ValueError("Channel not found")
        now = dt.datetime.now(UTC)
        job = ScheduledJob(
            id=f"job_{uuid.uuid4().hex[:10]}",
            name=name,
            description=description.strip() if description and description.strip() else None,
            channel_id=channel_id,
            instruction=instruction,
            schedule_kind=ScheduleKind(schedule_kind),
            creator_id=creator_id,
            interval_value=interval_value,
            interval_unit=interval_unit,
            daily_time=daily_time,
            next_run_at=next_run_for(
                schedule_kind,
                now=now,
                interval_value=interval_value,
                interval_unit=interval_unit,
                daily_time=daily_time,
                run_at=run_at,
            ),
            created_at=now,
        )
        return await uow.scheduled_jobs.create(job)

    async def update(
        self,
        job: ScheduledJob,
        uow: UnitOfWork,
        *,
        name: str,
        channel_id: str,
        instruction: str,
        schedule_kind: str,
        interval_value: int | None = None,
        interval_unit: str | None = None,
        daily_time: str | None = None,
        run_at: str | None = None,
        description: str | None = None,
    ) -> ScheduledJob:
        name = name.strip()
        instruction = instruction.strip()
        if not name:
            raise ValueError("Name is required")
        if not instruction:
            raise ValueError("Instruction is required")
        if await uow.channels.get_channel(channel_id) is None:
            raise ValueError("Channel not found")
        job.name = name
        job.description = description.strip() if description and description.strip() else None
        job.channel_id = channel_id
        job.instruction = instruction
        job.schedule_kind = ScheduleKind(schedule_kind)
        job.interval_value = interval_value
        job.interval_unit = interval_unit
        job.daily_time = daily_time
        job.next_run_at = next_run_for(
            schedule_kind,
            interval_value=interval_value,
            interval_unit=interval_unit,
            daily_time=daily_time,
            run_at=run_at,
        )
        return await uow.scheduled_jobs.update(job)

    async def pause(self, job: ScheduledJob, uow: UnitOfWork) -> None:
        await uow.scheduled_jobs.set_enabled(job.id, enabled=False)

    async def resume(self, job: ScheduledJob, uow: UnitOfWork) -> None:
        next_run = next_run_for(
            job.schedule_kind.value,
            interval_value=job.interval_value,
            interval_unit=job.interval_unit,
            daily_time=job.daily_time,
            run_at=job.next_run_at.isoformat(),
        )
        await uow.scheduled_jobs.set_enabled(job.id, enabled=True, next_run_at=next_run)

    async def delete(self, job: ScheduledJob, uow: UnitOfWork) -> None:
        await uow.scheduled_jobs.delete(job.id)

    async def run(
        self, job: ScheduledJob, uow: UnitOfWork, *,
        trigger: str = "scheduled", initiated_by: str | None = None,
    ) -> list:
        now = dt.datetime.now(UTC)
        run = ScheduledJobRun(
            id=f"run_{uuid.uuid4().hex[:12]}", job_id=job.id, trigger=trigger,
            initiated_by=initiated_by, instruction=job.instruction,
            channel_id=job.channel_id, scheduled_for=job.next_run_at, started_at=now,
        )
        await uow.scheduled_jobs.start_run(run)
        await uow.commit()
        try:
            chat = ChatService(build_registry(), self._settings)
            messages = await chat.post_and_respond(
                job.channel_id, job.creator_id, job.instruction, uow
            )
            enabled = job.schedule_kind != ScheduleKind.ONCE
            next_run = job.next_run_at
            if enabled:
                next_run = next_run_for(
                    job.schedule_kind.value,
                    now=now,
                    interval_value=job.interval_value,
                    interval_unit=job.interval_unit,
                    daily_time=job.daily_time,
                )
            await uow.scheduled_jobs.record_run(
                job.id, next_run_at=next_run, enabled=enabled,
                status="succeeded", error=None, run_at=now,
            )
            finished = dt.datetime.now(UTC)
            await uow.scheduled_jobs.finish_run(
                run.id, status="succeeded", finished_at=finished,
                duration_ms=max(0, int((finished - now).total_seconds() * 1000)),
                message_ids=[message.id for message in messages], error=None,
                next_run_at=next_run,
            )
            return messages
        except Exception as exc:
            await uow.scheduled_jobs.record_run(
                job.id, next_run_at=job.next_run_at, enabled=job.enabled,
                status="failed", error=str(exc), run_at=now,
            )
            finished = dt.datetime.now(UTC)
            await uow.scheduled_jobs.finish_run(
                run.id, status="failed", finished_at=finished,
                duration_ms=max(0, int((finished - now).total_seconds() * 1000)),
                message_ids=[], error=str(exc), next_run_at=job.next_run_at,
            )
            raise


class SchedulerLoop:
    def __init__(self, db, settings: Settings, poll_seconds: int = 30) -> None:
        self._db = db
        self._service = ScheduledJobService(settings)
        self._poll_seconds = poll_seconds
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def run_due_once(self) -> int:
        executed = 0
        async with self._db.uow() as uow:
            now = dt.datetime.now(UTC)
            jobs = await uow.scheduled_jobs.claim_due(
                now,
                claim_token=uuid.uuid4().hex,
                claim_until=now + dt.timedelta(minutes=5),
            )
            # Publish claims before network or agent execution. Competing workers
            # then observe the lease and skip the same scheduled occurrence.
            await uow.commit()
            for job in jobs:
                try:
                    await self._service.run(job, uow)
                    executed += 1
                except Exception:
                    logger.exception("Scheduled instruction %s failed", job.id)
        return executed

    async def _run(self) -> None:
        while True:
            try:
                await self.run_due_once()
            except Exception:
                logger.exception("Scheduled instruction poll failed")
            finally:
                await asyncio.sleep(self._poll_seconds)
