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
    """완료 기준 한 항목.

    unit: 이 기준을 *어디서* 검사하는지 결정하는 옵셔널 태그 (WO#26).
      - decomposition unit-id(예: "u1") → 그 유닛의 per-unit gate(worktree)에서 검사.
      - None 또는 "integration" → 통합 기준. 병렬 per-unit gate는 안 돌리고,
        전 유닛 머지 후 통합 gate(main)에서만 검사한다.
    병렬 모델 정합성: 기반 유닛이 *전체-시스템* 기준(풀 시뮬·교차 유닛)을 혼자 못 채워
    escalate하던 회귀를 막는다. 미태그(None)는 통합으로 흡수 = 후방호환.
    순차(N=1) 경로는 이 필드를 무시하고 전체 spec을 그대로 검사한다(무회귀).
    """

    id: str
    desc: str
    check: Check
    unit: str | None = None


class Assumption(BaseModel):
    id: str
    text: str
    confidence: float
    checkpoint: bool


class DecompositionUnit(BaseModel):
    unit: str
    desc: str
    deps: list[str] = Field(default_factory=list)
    # WO#59: 이 유닛이 *소유*하는 파일/모듈 영역(경로·glob 힌트). 병렬 형제 유닛이 서로
    # disjoint한 scope를 가지면 worktree 머지가 깨끗(통합 벽 예방, #51의 형제 버전).
    # optional·비파괴: 없으면 빈 리스트(기존 spec 무영향, deps/capability_requests 패턴).
    scope: list[str] = Field(default_factory=list)


# ──────────────────── 능력 획득 거버넌스 (WO#53 Phase F.1) ────────────────────
#
# 빌드가 *없는 능력*(라이브러리/툴)을 필요로 할 때, **거버넌스 하에** 발견·검증·채택한다:
#   요청(gap) → 큐레이션 레지스트리 발견(후보) → POC(증거) → 사람 승인(escalate) → 채택(+provenance)
# 안전 불변: 자동 채택 절대 없음(신뢰 결정은 사람), executor sandbox 무변경, 큐레이션 소스만,
# opt-in(플래그 OFF면 no-op). 아래는 그 *데이터 구조*(엔진-free, 직렬화 가능).


class CapabilityRequest(BaseModel):
    """능력 요청(gap) — 빌드가 필요로 하는 능력. spec/state에 기록된다.

    capability: 필요한 능력 키워드(예: "pathfinding"). unit: 어느 유닛이 필요로 하나(옵션).
    reason: 왜 필요한가(옵션). 합성기가 선언하거나(opt-in) 향후 executor가 요청.
    """

    capability: str
    unit: str | None = None
    reason: str | None = None


class CapabilityCandidate(BaseModel):
    """발견된 후보 — 큐레이션 레지스트리 엔트리에서 파생(무엇을·어디서·라이선스)."""

    capability: str
    identifier: str            # 패키지/툴 식별자(예: "pathfinding")
    ecosystem: str             # npm | pip | tool
    source: str                # 큐레이션 출처(예: "curated:npm/pathfinding")
    license: str
    install: list[str] = Field(default_factory=list)  # host-side 설치 명령(채택 실행=F.1b)


class CapabilityPOC(BaseModel):
    """POC 증거 — 후보가 작동하나/무엇을 import/무엇이 더 필요한지.

    ok: True(작동)/False(실패)/None(미실행 — 메타데이터만, 라이브 스모크는 주입 runner 필요).
    best-effort: POC 실패는 흡수되어 ok=False로 기록(루프 안 죽임).
    """

    identifier: str
    ok: bool | None = None
    imports: list[str] = Field(default_factory=list)
    needs: list[str] = Field(default_factory=list)
    detail: str | None = None


class CapabilityProvenance(BaseModel):
    """채택 provenance — 무엇을·어디서·라이선스·누가·언제 승인. 채택 시 state에 기록.

    승인(allowlist)된 후보에만 생성된다 = 거버넌스 감사 기록. 미승인은 provenance 없음.
    """

    capability: str
    identifier: str
    source: str
    license: str
    approved_by: str
    approved_at: str
    poc_ok: bool | None = None


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
    # 능력 획득(WO#53 F.1, opt-in): 빌드가 필요로 하는 *없는 능력*(라이브러리/툴) 요청.
    # 합성기가 선언할 수 있으나 선택 — 기본 빈 리스트(기존 spec 무영향). 능력 플래그 OFF면 무시.
    capability_requests: list[CapabilityRequest] = Field(default_factory=list)

    @classmethod
    def from_yaml(cls, path: str | Path) -> "ProjectSpec":
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return cls.model_validate(data)

    def to_yaml(self) -> str:
        """검증된 spec을 YAML 문자열로 직렬화(사이드카 영속화·라운드트립용, WO#58).

        스키마 추가가 아니라 단순 덤프 헬퍼. from_yaml과 라운드트립 가능.
        """
        return yaml.safe_dump(
            self.model_dump(by_alias=True, mode="json"),
            allow_unicode=True,
            sort_keys=False,
        )


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


class Cost(BaseModel):
    """LLM 호출 비용 (WO#33 계측).

    tokens: 총 토큰(input+output) — 후방호환 필드(기존 schema/state가 쓰던 int).
    input/output: 토큰 분해(잡히면). usd: 가격표로 계산(모델 미상이면 None — 날조 금지).
    source: orchestration(합성/replan/critic/scaffold) | executor(codex 서브프로세스) |
            judge(gate 내부 judge/run-judge) | mixed.
    note: 못 잡는 비용을 *정직하게* 남기는 메모(예: "executor usage 미노출").
    새 필드는 전부 optional/None 기본 → 기존 Cost(tokens=…, usd=…) 그대로 유효(무회귀).
    """

    tokens: int | None = None
    usd: float | None = None
    input: int | None = None
    output: int | None = None
    source: str | None = None
    note: str | None = None


class GateResult(BaseModel):
    """gate.judge의 반환 계약 — verdict + 그 근거(per-check 증거).

    근거는 mutable 속성에서 루프가 몰래 꺼내는 게 아니라 반환에 동봉한다.
    미래의 judge-gate(LLM-as-judge)도 같은 계약(verdict + checks)으로 끼워진다.

    judge_cost(WO#34): 이 gate 호출에서 *judge/run-judge LLM*에 든 비용(계측). gate가
    자기 judge MeteredClient에서 읽어 반환 계약에 동봉한다(근거와 동일 철학 — mutable
    속성이 아닌 반환). 메터링 미적용/judge 부재면 None(날조 금지). 루프가 이걸 event.cost에
    합산하고 budget.spent에 누적한다. **verdict/checks(검증 행동)와 무관 — 비용 노출뿐.**
    """

    verdict: Verdict
    checks: list[CheckReport] = Field(default_factory=list)
    judge_cost: Cost | None = None


class Activity(BaseModel):
    """현재 in-flight 유닛의 라이브 스냅샷 (WO#33 Part B — 대시보드 폴링용).

    dispatch 시 stage=build로 추가, gate 진입 시 stage=verify로 갱신, 완료 시 제거.
    병렬이면 동시에 여러 개. started_at은 단계 진입 타임스탬프(ISO; 못 잡으면 None).
    """

    unit: str | None = None
    stage: str
    started_at: str | None = None


class StageTransition(BaseModel):
    """단계 전이 이력 한 항목 (WO#33 Part B — 타임라인용).

    각 단계 진입을 (stage, unit, ts)로 append-only 기록한다. 대시보드가
    "합성→코딩→검증→재계획" 흐름을 시간순으로 그린다. best-effort(기록 실패 흡수).
    """

    stage: str
    unit: str | None = None
    ts: str | None = None


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
    stage: str | None = None


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


# ──────────────────── DecompCritique (분해 critic at replan, WO#40) ────────────────────


class DecompCritique(BaseModel):
    """분해 critic의 구조화 출력 + 오케스트레이션 결과(감사용).

    LEAP의 LLM 리뷰어처럼 *매 분해(replan이 낸 work order)*가 "유닛을 단순화/진전시키나
    vs 전체 goal/spec을 재진술만/헛도나"를 적대적으로 판정한다(verifier-side, 독립 client).

    verdict: "progress"(유닛을 단순화/진전 — dispatch OK) | "weak"(전체 goal/spec 재진술,
             분해 안 됨, gap 안 줄임, 직전 실패 접근 반복 = 무진전). 파싱/평가 불가 시
             견고성 차원에서 "progress"로 흡수(진행 막지 않음 — best-effort soft).
    reason:  판정 근거(한 줄). surface/감사용.
    unit:    어느 유닛 분해에 대한 판정인지.
    rejected: 이 분해가 weak로 reject돼 재replan을 트리거했는지(루프가 설정).
    """

    verdict: str
    reason: str | None = None
    unit: str | None = None
    rejected: bool = False


# ──────────────────── OR-node / approach 추적 (WO#41, Phase D) ────────────────────


class ApproachAttempt(BaseModel):
    """OR-node: 한 goal(유닛 또는 통합)에 대해 시도한 한 *접근*의 기록(append-only 감사).

    LEAP의 AND-OR DAG처럼, gate가 정상 재시도까지 소진하고도 실패하면 *같은 acceptance
    criteria/done_when을 둔 채* 근본적으로 다른 접근으로 갈아탄다. 그 시도 이력이다.

    **bar 불변(anti-erosion)**: 대안은 접근(알고리즘/구조)만 바꾼다 — criteria/done_when은
    절대 건드리지 않는다. evidence는 gate 실패 *증거*일 뿐, 기준 약화 신호가 아니다.

    scope:    "unit:<id>" | "integration".
    approach: 시도한 접근 요약(work order goal 또는 통합 접근 라벨).
    outcome:  "fail"(판정 실패) | "abandoned"(대안으로 갈아탐) | "exhausted"(대안 소진).
    evidence: gate 실패 증거 요약(독립 gate/run-judge가 뭐가 부족하다 했는지).
    index:    0=원본 접근, 1+=대안 순번.
    """

    scope: str
    approach: str | None = None
    outcome: str
    evidence: str | None = None
    index: int = 0


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
    # replan이 낸 분해(work order)에 대한 적대적 critic 판정 이력(WO#40, append-only 감사 로그).
    # weak로 reject→재replan했거나 재시도 소진 후 진행한 경우를 기록한다(critic OFF면 빈 리스트).
    decomp_critiques: list[DecompCritique] = Field(default_factory=list)
    # OR-node(WO#41): goal(유닛/통합)별 시도한 접근 이력(append-only). gate 실패로 다른
    # 접근으로 갈아탄 백트래킹을 기록한다(or_alternatives=0이면 빈 리스트 = 기존 동작).
    approaches: list[ApproachAttempt] = Field(default_factory=list)
    # 현재 in-flight 유닛 라이브 스냅샷(WO#33 Part B). 완료 시 비워진다.
    activity: list[Activity] = Field(default_factory=list)
    # 단계 전이 이력(WO#33 Part B) — append-only 타임라인.
    transitions: list[StageTransition] = Field(default_factory=list)
    # 능력 획득 거버넌스(WO#53 F.1) — append-only 감사. 능력 플래그 OFF면 둘 다 빈 리스트.
    #   requests: 이번 run이 선언한 능력 gap. provenance: *승인되어 채택된* 능력(무엇·출처·라이선스·승인).
    capability_requests: list[CapabilityRequest] = Field(default_factory=list)
    capability_provenance: list[CapabilityProvenance] = Field(default_factory=list)

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
