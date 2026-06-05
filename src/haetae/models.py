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
    run = "run"  # 산출물을 실행해 동적 행동(부팅/트레이스)을 캡처·판정 (WO#22)
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


class Verdict(str, Enum):
    # 'pass'는 파이썬 예약어라 멤버명은 pass_, 값만 "pass" (Check.pass_ 패턴과 동일)
    pass_ = "pass"
    fail_recoverable = "fail_recoverable"
    fail_replan = "fail_replan"
    ambiguous = "ambiguous"
    stuck = "stuck"
    budget = "budget"
    done = "done"


class Action(str, Enum):
    next_order = "next_order"
    retry = "retry"
    replan_approach = "replan_approach"
    propose_spec_change = "propose_spec_change"
    escalate = "escalate"
    stop = "stop"


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


class RunEvidence(BaseModel):
    """산출물을 *실행*해 캡처한 동적 행동 증거 (WO#22 — judge-runs-it).

    정적 체크(exit code/파일 읽기)가 못 보는 *실행 시 행동*을 잡는다. judge가 이 증거로
    "그냥 돌기만" vs "행동이 진짜 성립"을 적대적으로 판정한다. CheckReport에 실려 감사된다.

    booted:      크래시/타임아웃 없이 정상 종료(exit 0)했는가.
    exit_code:   프로세스 종료 코드(타임아웃/실행불가면 None).
    trace:       캡처한 stdout(구조화 트레이스면 JSON 텍스트 그대로). 상한 cap 적용.
    stderr_tail: stderr 끝부분(에러 진단용). 상한 cap 적용.
    timed_out:   타임아웃으로 강제 종료됐는가.
    duration_s:  벽시계 실행 시간(초).
    reason:      booted=False일 때 사유(타임아웃/실행 실패/예외). 정상이면 None.
    """

    booted: bool
    exit_code: int | None = None
    trace: str = ""
    stderr_tail: str = ""
    timed_out: bool = False
    duration_s: float = 0.0
    reason: str | None = None


class CheckReport(BaseModel):
    """gate가 ac 하나를 평가한 per-check 증거.

    gate(CheckRunner)가 무엇을(cmd) 돌려 어떤 결과(status/exit_code)를 얻었는지
    감사 추적으로 남긴다. verdict의 *근거*. Event.checks에 그대로 실린다.
    status는 pass | fail | skipped.

    run_evidence: run 타입 체크일 때 캡처한 실행 증거(그 외 타입은 None).
    """

    ac_id: str
    check_type: CheckType
    cmd: str | None = None
    status: str  # pass | fail | skipped
    exit_code: int | None = None
    detail: str | None = None
    run_evidence: RunEvidence | None = None


class GateResult(BaseModel):
    """gate.judge의 반환 계약 — verdict + 그 근거(per-check 증거).

    근거는 mutable 속성에서 루프가 몰래 꺼내는 게 아니라 반환에 동봉한다.
    미래의 judge-gate(LLM-as-judge)도 같은 계약(verdict + checks)으로 끼워진다.
    """

    verdict: Verdict
    checks: list[CheckReport] = Field(default_factory=list)


class Cost(BaseModel):
    tokens: int | None = None
    usd: float | None = None


class Event(BaseModel):
    seq: int
    unit: str | None = None
    work_order_ref: str | None = None
    result: str | None = None
    verdict: Verdict | None = None
    checks: list[CheckReport] = Field(default_factory=list)
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


# ──────────────────────── SpecCritique (적대적 spec 비평) ────────────────────────


class SpecGap(BaseModel):
    """비평가가 짚은 구체적 약점 하나.

    cheap_path: acceptance_criteria를 *싸구려로/trivial하게* 충족하는 구체 경로.
    strengthening: 그 cheap-path를 막으려면 기준을 어떻게 강화해야 하는지.
    막연한 지적은 금지 — area만 있고 cheap_path가 없으면 약한 신호다.
    """

    area: str
    cheap_path: str | None = None
    strengthening: str | None = None


class SpecCritique(BaseModel):
    """spec critic의 구조화 출력 + 오케스트레이션 결과(감사용).

    verdict: "adequate"(진짜 어려움을 잡음) | "soft"(싸구려 충족 경로 있음).
             파싱/평가 불가 시 견고성 차원에서 "adequate"로 흡수(진행 막지 않음).
    gaps:    구체적 약점 목록. soft인데 gaps가 비면 재합성을 트리거하지 않는다.
    note:    메타 기록(예: "평가 불가: ...", 재합성 폴백 사유). surface/감사용.
    resynthesized: 비평을 피드백으로 1회 재합성이 *실제로* 일어났는지(루프가 설정).
    """

    verdict: str
    gaps: list[SpecGap] = Field(default_factory=list)
    note: str | None = None
    resynthesized: bool = False


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
    # synthesize 직후 적대적 critic이 남긴 비평(opt-in; critic OFF면 None).
    spec_critique: SpecCritique | None = None

    @classmethod
    def from_yaml(cls, path: str | Path) -> "State":
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return cls.model_validate(data)


# ──────────────────────── Decision (replan 출력 계약) ────────────────────────
# prompts/replan.md 맨 아래 Decision 스키마를 그대로 모델로 옮긴 것.


class NextOrder(BaseModel):
    """action == next_order | retry 일 때 다음 work order."""

    unit: str
    goal: str
    scope: str | None = None
    context_refs: list[str] = Field(default_factory=list)
    local_checks: list[Check] = Field(default_factory=list)
    executor: str | None = None
    deliverable: str | None = None


class SpecChangeProposal(BaseModel):
    """action == propose_spec_change 일 때 spec 변경 제안.

    State.SpecChange(감사 로그 항목)와 다른 형태라 이름을 분리한다.
    """

    target: str
    from_: str | None = Field(default=None, alias="from")
    to: str | None = None
    reason: str
    evidence: str | None = None
    version_bump: bool = False

    model_config = {"populate_by_name": True}


class Escalation(BaseModel):
    """action == escalate 일 때 사람에게 올리는 질문."""

    question: str
    why_now: str | None = None


class Decision(BaseModel):
    """replan이 매 iteration 내리는 단 하나의 결정."""

    verdict: Verdict
    action: Action
    rationale: str
    next_order: NextOrder | None = None
    spec_change: SpecChangeProposal | None = None
    escalation: Escalation | None = None
