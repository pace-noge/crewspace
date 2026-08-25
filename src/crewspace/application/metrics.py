"""M6.7 — Deterministic scorecard computation (pure application logic + DB wiring).

compute_scorecard(runs, tool_calls=..., verification_results=..., change_sets=...)
is a PURE function over CodingRun-like, AgentToolCall-like, VerificationResultDTO,
and StoredChangeSet-like records. It is order-independent (deterministic) and
emits MetricValue aggregates whose (numerator, denominator) match
METRIC_DEFINITIONS. No DB, no framework — unit-testable and migration-safe.

compute_team_scorecard(uow, team_id) is the DB-backed wiring: it pulls the real
records via the repositories and delegates to compute_scorecard, so the same
deterministic aggregates are produced whether fed real rows or hand-built ones.
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable, Optional, Sequence

from crewspace.dto.change_sets import VerificationResultDTO
from crewspace.dto.metrics import METRIC_BY_ID, MetricValue


def _ratio(metric_id: str, numerator: int, denominator: int) -> MetricValue:
    value = (numerator / denominator) if denominator else 0.0
    return MetricValue(metric_id=metric_id, value=value, numerator=float(numerator), denominator=float(denominator))


def compute_scorecard(
    runs: Sequence[object],
    tool_calls: Optional[Iterable[object]] = None,
    verification_results: Optional[Iterable[object]] = None,
    change_sets: Optional[Iterable[object]] = None,
) -> dict[str, MetricValue]:
    """Compute deterministic aggregate reliability metrics.

    - `runs` are CodingRun-like (status:str, started_at/finished_at: datetime|None).
    - `tool_calls` are AgentToolCall-like (status:str, duration_ms:int|None, error:str|None).
    - `verification_results` are VerificationResultDTO-like (status:str).
    - `change_sets` are StoredChangeSet-like (status:str; "reviewed" = human accepted).
    """
    runs = list(runs)
    total = len(runs)

    succeeded = sum(1 for r in runs if getattr(r, "status", None) == "succeeded")
    failed = sum(1 for r in runs if getattr(r, "status", None) == "failed")
    timed_out = sum(1 for r in runs if getattr(r, "status", None) == "timed_out")
    cancelled = sum(1 for r in runs if getattr(r, "status", None) == "cancelled")

    # latency only over runs that recorded both timestamps
    latencies: list[float] = []
    for r in runs:
        start = getattr(r, "started_at", None)
        fin = getattr(r, "finished_at", None)
        if isinstance(start, datetime) and isinstance(fin, datetime) and fin >= start:
            latencies.append((fin - start).total_seconds())
    latency_sum = sum(latencies)
    latency_n = len(latencies)
    mean_latency = (latency_sum / latency_n) if latency_n else 0.0

    tools = list(tool_calls or [])
    tool_total = len(tools)
    tool_errors = sum(1 for t in tools if getattr(t, "status", None) == "error")
    durations = [d for t in tools if isinstance((d := getattr(t, "duration_ms", None)), (int, float)) and d is not None]
    duration_sum = sum(durations)
    duration_n = len(durations)
    mean_duration = (duration_sum / duration_n) if duration_n else 0.0

    # verification pass rate over change-set verification results
    vresults = list(verification_results or [])
    v_total = len(vresults)
    v_passed = sum(1 for v in vresults if getattr(v, "status", None) == "passed")

    # change-set human approval rate over captured change sets
    csets = list(change_sets or [])
    cs_total = len(csets)
    cs_approved = sum(1 for c in csets if getattr(c, "status", None) == "reviewed")

    return {
        "success_rate": _ratio("success_rate", succeeded, total),
        "failure_rate": _ratio("failure_rate", failed, total),
        "timeout_rate": _ratio("timeout_rate", timed_out, total),
        "cancellation_rate": _ratio("cancellation_rate", cancelled, total),
        "mean_latency_seconds": MetricValue(
            metric_id="mean_latency_seconds",
            value=mean_latency,
            numerator=latency_sum,
            denominator=float(latency_n),
        ),
        "tool_failure_rate": _ratio("tool_failure_rate", tool_errors, tool_total),
        "tool_mean_duration_ms": MetricValue(
            metric_id="tool_mean_duration_ms",
            value=mean_duration,
            numerator=float(duration_sum),
            denominator=float(duration_n),
        ),
        "verification_pass_rate": _ratio("verification_pass_rate", v_passed, v_total),
        "change_set_approval_rate": _ratio("change_set_approval_rate", cs_approved, cs_total),
    }


async def compute_team_scorecard(uow, team_id: str) -> dict[str, MetricValue]:
    """DB-backed scorecard: pull real records for a team and delegate to compute_scorecard.

    Run metrics + change-set approval rate come from the repositories (real query);
    tool/verification metrics are derived from the change-set verification records
    attached to the team's captured change sets. Returns the same deterministic
    aggregates as compute_scorecard fed the same records directly.
    """
    runs = await uow.coding_runs.list_for_team(team_id)
    stored_sets = await uow.change_sets.list_for_teams([team_id])
    verification_results: list[VerificationResultDTO] = []
    for stored in stored_sets:
        payload = stored.payload if isinstance(stored.payload, dict) else {}
        for v in payload.get("verification", ()):
            verification_results.append(v if isinstance(v, VerificationResultDTO) else VerificationResultDTO(**v))
    return compute_scorecard(
        runs,
        verification_results=verification_results,
        change_sets=stored_sets,
    )
