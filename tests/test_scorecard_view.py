"""M6.7 slice 6 — UI links every aggregate to inspectable supporting runs.

build_scorecard_view turns a compute_scorecard result into a render-ready view
where EACH metric carries its numerator/denominator (from METRIC_DEFINITIONS) AND
the supporting run/change-set ids it was computed from, so the template can
deep-link every aggregate to the records behind it. Acceptance item 6.
"""
from __future__ import annotations

from crewspace.api.rendering import templates
from crewspace.application.metrics import MetricValue, compute_scorecard
from crewspace.application.scorecard_view import build_scorecard_view
from crewspace.dto.metrics import METRIC_BY_ID


def _run(rid, status, started=None, finished=None):
    class _R:
        pass
    r = _R()
    r.id = rid
    r.status = status
    r.started_at = started
    r.finished_at = finished
    return r


def _cs(cid, status):
    class _C:
        pass
    c = _C()
    c.id = cid
    c.status = status
    return c


def test_view_has_one_row_per_metric_with_value_and_denominator():
    from datetime import datetime, timedelta

    base = datetime(2026, 1, 1)
    runs = [
        _run("run_1", "succeeded", base, base + timedelta(seconds=10)),
        _run("run_2", "failed", base, base + timedelta(seconds=5)),
        _run("run_3", "cancelled", base, base),
    ]
    csets = [_cs("cs_1", "reviewed"), _cs("cs_2", "captured")]
    card = compute_scorecard(runs, change_sets=csets)
    view = build_scorecard_view(card, runs=runs, change_sets=csets)

    assert len(view.metrics) == len(METRIC_BY_ID)
    by_id = {m.metric_id: m for m in view.metrics}
    # value + numerator/denominator match the documented definitions + scorecard
    sr = by_id["success_rate"]
    assert sr.value == card["success_rate"].value
    assert sr.numerator == card["success_rate"].numerator
    assert sr.denominator == card["success_rate"].denominator
    assert sr.denominator_label == METRIC_BY_ID["success_rate"].denominator
    assert sr.unit == METRIC_BY_ID["success_rate"].unit


def test_each_metric_links_to_supporting_runs_and_change_sets():
    from datetime import datetime, timedelta

    base = datetime(2026, 1, 1)
    runs = [
        _run("run_1", "succeeded", base, base + timedelta(seconds=10)),
        _run("run_2", "failed", base, base + timedelta(seconds=5)),
        _run("run_3", "cancelled", base, base),
    ]
    csets = [_cs("cs_1", "reviewed"), _cs("cs_2", "captured")]
    card = compute_scorecard(runs, change_sets=csets)
    view = build_scorecard_view(card, runs=runs, change_sets=csets)
    by_id = {m.metric_id: m for m in view.metrics}

    # run-derived metrics link to the runs in scope
    assert set(by_id["success_rate"].supporting_run_ids) == {"run_1", "run_2", "run_3"}
    # change-set-derived metrics link to the change sets in scope
    assert set(by_id["change_set_approval_rate"].supporting_change_set_ids) == {"cs_1", "cs_2"}
    # every supporting id is a real record id (deep-link target), not a placeholder
    assert all(isinstance(x, str) and x for x in by_id["success_rate"].supporting_run_ids)


def test_template_renders_denominator_and_deep_link_per_metric():
    from datetime import datetime, timedelta

    base = datetime(2026, 1, 1)
    runs = [
        _run("run_1", "succeeded", base, base + timedelta(seconds=10)),
        _run("run_2", "failed", base, base + timedelta(seconds=5)),
        _run("run_3", "cancelled", base, base),
    ]
    csets = [_cs("cs_1", "reviewed")]
    card = compute_scorecard(runs, change_sets=csets)
    view = build_scorecard_view(card, runs=runs, change_sets=csets)
    html = templates.get_template("scorecard.html").render(view=view)

    # every aggregate shows its value + documented denominator label
    assert "success_rate" in html
    assert "succeeded runs / total runs (all statuses in window)" in html  # denominator label text
    # and deep-links to the inspectable supporting runs
    assert "/api/coding/runs/run_1" in html
    assert "/api/coding/runs/run_2" in html
    assert "/api/coding/runs/run_3" in html


def test_no_dead_links_when_no_supporting_change_sets():
    from datetime import datetime, timedelta

    base = datetime(2026, 1, 1)
    runs = [_run("run_1", "succeeded", base, base + timedelta(seconds=10))]
    card = compute_scorecard(runs)  # no change sets supplied
    view = build_scorecard_view(card, runs=runs, change_sets=())
    by_id = {m.metric_id: m for m in view.metrics}
    # metrics with no change-set input carry no change-set ids (no dead links)
    assert by_id["success_rate"].supporting_change_set_ids == []
    assert by_id["change_set_approval_rate"].supporting_change_set_ids == []
    html = templates.get_template("scorecard.html").render(view=view)
    # no management/change-sets link is emitted when there are no change sets
    assert "/management/change-sets/" not in html
