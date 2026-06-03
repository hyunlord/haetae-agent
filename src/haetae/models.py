"""haetae 데이터 레이어 — ProjectSpec(pinned) / State(mutable) 모델.

두 YAML 스키마(spec/projectspec.schema.yaml, spec/state.schema.yaml)를
pydantic v2 모델로 옮긴 것. enum은 자유 문자열을 막기 위해 강제한다.
LLM 호출·시크릿 없음. 순수 데이터 검증.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


# ──────────────────────────── enums ────────────────────────────


class TaskType(str, Enum):
    feature_impl = "feature_impl"
    research = "research"
    harness_build = "harness_build"
    infra = "infra"
    refactor = "refactor"
    investigation = "investigation"


class Verifiability(str, Enum):
    objective = "objective"
    judge = "judge"
    human_checkpoint = "human_checkpoint"


class Mode(str, Enum):
    fast = "fast"
    normal = "normal"
    slow = "slow"


class CheckType(str, Enum):
    test = "test"
    bench = "bench"
    lint = "lint"
    build = "build"
    schema = "schema"
    judge = "judge"
    human = "human"


class Status(str, Enum):
    running = "running"
    escalated = "escalated"
    done = "done"
    stopped_budget = "stopped_budget"
    stopped_stuck = "stopped_stuck"


class PlanState(str, Enum):
    pending = "pending"
    in_progress = "in_progress"
    done = "done"
    failed = "failed"


# ──────────────────────── ProjectSpec 하위 모델 ────────────────────────


class Check(BaseModel):
    """acceptance_criteria 항목의 검증 명령. type은 enum 강제."""

    type: CheckType
    cmd: str | None = None
    # ProjectSpec의 pass는 ">=10", "0" 같은 기대값 문자열(선택).
    pass_: str | None = Field(default=None, alias="pass")

    model_config = {"populate_by_name": True}


class AcceptanceCriterion(BaseModel):
    id: str
    desc: str
    check: Check


class Assumption(BaseModel):
    id: str
    text: str
    confidence: float
    checkpoint: bool


class DecompositionUnit(BaseModel):
    unit: str
    desc: str
    deps: list[str] = Field(default_factory=list)


# ──────────────────────────── ProjectSpec ────────────────────────────


class ProjectSpec(BaseModel):
    """north-star 아티팩트. 합성기가 만들고 이후 pinned 된다."""

    spec_id: str
    version: int
    order_raw: str
    goal: str
    task_type: TaskType
    verifiability: Verifiability
    mode: Mode
    constraints: list[str] = Field(default_factory=list)
    acceptance_criteria: list[AcceptanceCriterion]
    assumptions: list[Assumption] = Field(default_factory=list)
    non_goals: list[str]
    done_when: str
    decomposition: list[DecompositionUnit] = Field(default_factory=list)
    open_questions: list[Any] = Field(default_factory=list)

    @classmethod
    def from_yaml(cls, path: str | Path) -> "ProjectSpec":
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return cls.model_validate(data)


# ──────────────────────────── State 하위 모델 ────────────────────────────


class PlanItem(BaseModel):
    unit: str
    state: PlanState
    deps: list[str] | None = None


class EventCheck(BaseModel):
    """이벤트 로그에 기록된 검증 결과. type은 enum 강제, pass는 bool."""

    id: str | None = None
    type: CheckType
    result: str | None = None
    pass_: bool | None = Field(default=None, alias="pass")

    model_config = {"populate_by_name": True}


class Cost(BaseModel):
    tokens: int | None = None
    usd: float | None = None


class Event(BaseModel):
    seq: int
    unit: str | None = None
    work_order_ref: str | None = None
    result: str | None = None
    verdict: str | None = None
    checks: list[EventCheck] = Field(default_factory=list)
    learnings: str | None = None
    cost: Cost | None = None
    ts: str | None = None


class SpecChange(BaseModel):
    seq: int
    target: str
    reason: str
    evidence: str | None = None
    version: str | None = None


class Budget(BaseModel):
    spent: Cost = Field(default_factory=Cost)
    cap: Cost = Field(default_factory=Cost)


# ──────────────────────────── State ────────────────────────────


class State(BaseModel):
    """mutable 절반. replan이 읽고 매 iteration 갱신한다."""

    spec_ref: str
    spec_version: int
    status: Status
    plan: list[PlanItem] = Field(default_factory=list)
    events: list[Event] = Field(default_factory=list)
    spec_changes: list[SpecChange] = Field(default_factory=list)
    budget: Budget = Field(default_factory=Budget)
    pending_escalations: list[Any] = Field(default_factory=list)

    @classmethod
    def from_yaml(cls, path: str | Path) -> "State":
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return cls.model_validate(data)
