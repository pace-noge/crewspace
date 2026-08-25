"""M6.7 slice 1 — Scorecard metric definitions (pure DTO, no DB/framework).

Each metric carries an explicit documented denominator and a privacy/retention
note so item 1 (documented definitions) is enforced in code, not prose.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ScorecardMetric:
    """One documented reliability metric."""

    metric_id: str
    label: str
    unit: str  # ratio | count | ms | seconds
    denominator: str  # human-readable, audited
    privacy: str  # retention + cross-tenant note
    description: str = ""


# Documented metric catalog. Denominators are written to be auditable and are
# exactly what compute_scorecard uses (total = count of runs/tool-calls input).
METRIC_DEFINITIONS: tuple[ScorecardMetric, ...] = (
    ScorecardMetric(
        metric_id="success_rate",
        label="Success rate",
        unit="ratio",
        denominator="succeeded runs / total runs (all statuses in window)",
        privacy="Aggregate only; no message bodies, inst/repo ids retained beyond 90d.",
        description="Share of coding runs that reached a succeeded status.",
    ),
    ScorecardMetric(
        metric_id="failure_rate",
        label="Failure rate",
        unit="ratio",
        denominator="failed runs / total runs",
        privacy="Aggregate only; failure_reason retained 90d, not surfaced to other teams.",
        description="Share of coding runs that failed.",
    ),
    ScorecardMetric(
        metric_id="timeout_rate",
        label="Timeout rate",
        unit="ratio",
        denominator="timed_out runs / total runs",
        privacy="Aggregate only.",
        description="Share of coding runs that timed out.",
    ),
    ScorecardMetric(
        metric_id="cancellation_rate",
        label="Cancellation rate",
        unit="ratio",
        denominator="cancelled runs / total runs",
        privacy="Aggregate only; who cancelled retained 90d.",
        description="Share of coding runs cancelled by a human or disconnect.",
    ),
    ScorecardMetric(
        metric_id="mean_latency_seconds",
        label="Mean end-to-end latency",
        unit="seconds",
        denominator="sum(finished_at - started_at) / runs with both timestamps",
        privacy="Durations only; no content.",
        description="Mean wall-clock duration of runs that recorded start and finish.",
    ),
    ScorecardMetric(
        metric_id="tool_failure_rate",
        label="Tool failure rate",
        unit="ratio",
        denominator="tool calls with status=error / total tool calls",
        privacy="Aggregate only; redacted args retained 90d.",
        description="Share of agent tool calls that errored.",
    ),
    ScorecardMetric(
        metric_id="tool_mean_duration_ms",
        label="Mean tool duration",
        unit="ms",
        denominator="sum(duration_ms) / tool calls with duration_ms set",
        privacy="Durations only.",
        description="Mean duration of tool calls that reported a duration.",
    ),
    ScorecardMetric(
        metric_id="verification_pass_rate",
        label="Verification pass rate",
        unit="ratio",
        denominator="verification results with status=passed / total verification results in window",
        privacy="Aggregate only; result names retained 90d.",
        description="Share of change-set verification results that passed.",
    ),
    ScorecardMetric(
        metric_id="change_set_approval_rate",
        label="Change-set human approval rate",
        unit="ratio",
        denominator="change sets with status=reviewed (human accepted) / total captured change sets in window",
        privacy="Aggregate only; artifact metadata retained 90d.",
        description="Share of captured change sets a human reviewed/approved.",
    ),
)

METRIC_BY_ID = {m.metric_id: m for m in METRIC_DEFINITIONS}


@dataclass(frozen=True)
class MetricValue:
    """A computed aggregate with its explicit denominator (auditable)."""

    metric_id: str
    value: float
    numerator: float
    denominator: float

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, MetricValue):
            return NotImplemented
        return (
            self.metric_id == other.metric_id
            and self.value == other.value
            and self.numerator == other.numerator
            and self.denominator == other.denominator
        )

    def __hash__(self) -> int:  # frozen dataclass needs explicit hash for == override
        return hash((self.metric_id, self.value, self.numerator, self.denominator))

    @property
    def ratio(self) -> Optional[float]:
        return self.value if METRIC_BY_ID[self.metric_id].unit == "ratio" else None
