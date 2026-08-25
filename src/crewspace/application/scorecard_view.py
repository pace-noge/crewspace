"""M6.7 slice 6 — Pure view-model builder for the scorecard UI.

Turns a compute_scorecard result into a render-ready view where EACH metric
carries its numerator/denominator (matching METRIC_DEFINITIONS) AND the ids of the
supporting runs / change sets it was computed from, so the template can deep-link
every aggregate to the inspectable records behind it.

Kept free of DB/request dependencies so it is unit-testable and reusable from any
router (acceptance item 6: UI links every aggregate to inspectable supporting
runs).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from crewspace.dto.metrics import METRIC_BY_ID, MetricValue

# Canonical deep-link targets (real existing routes in the app).
RUN_HREF = "/api/coding/runs/{run_id}"
CHANGE_SET_HREF = "/management/change-sets/{change_set_id}"


@dataclass
class ScorecardMetricView:
    metric_id: str
    label: str
    unit: str
    value: float
    numerator: float
    denominator: float
    denominator_label: str
    supporting_run_ids: List[str] = field(default_factory=list)
    supporting_change_set_ids: List[str] = field(default_factory=list)

    @property
    def run_links(self) -> List[str]:
        return [RUN_HREF.format(run_id=r) for r in self.supporting_run_ids]

    @property
    def change_set_links(self) -> List[str]:
        return [CHANGE_SET_HREF.format(change_set_id=c) for c in self.supporting_change_set_ids]


@dataclass
class ScorecardView:
    metrics: List[ScorecardMetricView] = field(default_factory=list)


def build_scorecard_view(
    card: dict,
    *,
    runs: Sequence = (),
    change_sets: Sequence = (),
    verification_results: Sequence = (),
) -> ScorecardView:
    """Build a render-ready view of a scorecard.

    `card` is the dict produced by compute_scorecard (metric_id -> MetricValue).
    `runs` / `change_sets` are the records fed into the scorecard; their ids become
    the inspectable deep-link targets for the metrics that were derived from them.
    """
    run_ids = [getattr(r, "id", None) for r in runs]
    run_ids = [rid for rid in run_ids if rid]
    cs_ids = [getattr(c, "id", None) for c in change_sets]
    cs_ids = [cid for cid in cs_ids if cid]

    metrics: List[ScorecardMetricView] = []
    for metric_id, value in card.items():
        if not isinstance(value, MetricValue):
            continue
        definition = METRIC_BY_ID.get(metric_id)
        # A run-derived metric is backed by the runs in scope; a change-set-derived
        # metric is backed by the change sets in scope. This keeps every link a
        # real record, never a placeholder.
        is_change_set_metric = metric_id in (
            "verification_pass_rate",
            "change_set_approval_rate",
        )
        metrics.append(
            ScorecardMetricView(
                metric_id=metric_id,
                label=definition.label if definition else metric_id,
                unit=definition.unit if definition else "ratio",
                value=value.value,
                numerator=value.numerator,
                denominator=value.denominator,
                denominator_label=definition.denominator if definition else "",
                supporting_run_ids=list(run_ids) if not is_change_set_metric else [],
                supporting_change_set_ids=list(cs_ids) if is_change_set_metric else [],
            )
        )
    return ScorecardView(metrics=metrics)
