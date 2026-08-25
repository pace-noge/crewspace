"""M6.7 slice 3 — Replayable benchmark fixture DTOs (pure, no DB/framework).

A BenchmarkFixture is a self-contained, FROZEN task definition. Its declared run
outcomes are the single source of truth for a replay, so materializing it never
reads a production workspace or repository — the fixture IS the isolated seed.
"""
from __future__ import annotations

from typing import Literal, Tuple

from pydantic import Field

from crewspace.dto.change_sets import FrozenDTO


class BenchmarkToolSpec(FrozenDTO):
    status: Literal["ok", "error"]
    duration_ms: int = 0


class BenchmarkVerificationSpec(FrozenDTO):
    status: Literal["passed", "failed"]


class BenchmarkRunSpec(FrozenDTO):
    status: Literal["succeeded", "failed", "timed_out", "cancelled"]
    latency_seconds: float = 0.0
    tools: Tuple[BenchmarkToolSpec, ...] = Field(default_factory=tuple)
    verification: Tuple[BenchmarkVerificationSpec, ...] = Field(default_factory=tuple)
    change_set_status: Literal["reviewed", "captured", None] = None


class BenchmarkFixture(FrozenDTO):
    fixture_id: str
    task: str
    agent_id: str
    model_version: str
    recorded_at: str  # ISO timestamp the fixture was captured
    runs: Tuple[BenchmarkRunSpec, ...] = Field(default_factory=tuple)
