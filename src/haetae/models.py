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
    # WO#68: codex 사용량/크레딧 소진으로 인한 graceful stop(외부 조건 — 크래시 아님, 재개 가능).
    stopped_credit = "stopped_credit"
    # WO#75: 사용자 stop/SIGINT(#43 graceful interrupt)로 멈춤 — "막힘(stuck)"과 의미 구분.
    #   (사용자 의도 중단 vs 진전 불가). 추가형 enum — 구버전 state read 무영향.
    stopped_interrupted = "stopped_interrupted"


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
    # WO#82 (A): run/sim:trace 기준이 *요구하는 증거 필드*의 구조화 선언(예:
    # [wall_crossings, overlap_pairs, route_cost_samples]). 합성기가 산문(desc/pass)과 *함께*
    # 명시한다 → #78 계약 추출이 이 슬롯을 *우선* 읽어 산문에도 견고(brittle no-op 해소, #81).
    # 없으면 #78이 desc/pass의 snake_case 스크레이프로 폴백(back-compat). 추가형·비파괴:
    # 바(성공 기준)가 *이미 요구*하는 증거의 구조화일 뿐 — 새 요구/완화 아님(anti-erosion).
    evidence_fields: list[str] = Field(default_factory=list)
    # WO#98: 이 run/행동 기준을 입증하려면 하니스가 *밟아야 할 시나리오 흐름*의 구조화 선언(순서
    # 있는 STEP 목록, 예: ["todo에 카드 생성", "*같은 카드*를 todo→doing 이동+위치확인",
    # "doing→done 이동+위치확인"]). evidence_fields(#82-A)가 *어떤 필드*를 낼지 명시하듯, 이건
    # 그 필드를 채우는 *시나리오 절차*를 명시한다(step→field로 연결). #92에서 필드는 다 emit됐지만
    # 시나리오 로직이 부실해(같은 카드 미이동·reload 前 삭제) 거짓 음성 증거가 나온 결함을 겨냥.
    # **빌더-측 유도 전용** — 합성기가 criteria서 *파생*해 명시하고 빌더 작업지시서에만 주입된다.
    # 게이트/적대 run-judge는 안 읽는다(*무엇을 구동할지* 유도지 *판정 완화* 아님 — 바 불변).
    # 추가형·비파괴(evidence_fields와 동형): 없으면 빈 리스트(무유도·기존 동작 그대로, back-compat).
    scenario_steps: list[str] = Field(default_factory=list)


class Assumption(BaseModel):
    id: str
    text: str
    confidence: float
    checkpoint: bool


class DecompositionUnit(BaseModel):
    unit: str
    desc: str
    deps: list[str] = Field(default_factory=list)
    # WO#59 + WO#165(정식화): 이 유닛이 *배타적으로 소유*하는 파일/모듈 경로(소유 매니페스트).
    # **disjoint 불변식**: 형제(병렬) 유닛 간 scope 교집합 = ∅ — 각 파일은 *한 유닛만* 소유한다.
    # 그래야 동시 빌드 worktree 머지가 자명히 충돌-free(서로 다른 파일)고 직렬화 폴백(#21)이 거의
    # 안 걸린다(통합 벽 예방, #51의 형제 버전). 유닛이 서로 닿아야 하면 *파일 공유 금지* →
    # facade 계약(#160 — 한 유닛 export·다른 유닛 import)으로 결합해 소유권을 단일하게 유지하라.
    # 이 필드가 disjoint 보장이 떨어지는 지점이다 — scheduler.is_disjoint_from(#110)·intake scope-겹침
    # 재합성(#59)이 그대로 읽어 혜택받는다(새 필드 불요). 합성기가 명시하고, decomp-critic은
    # replan-time 형제 겹침을 잡아 재분해를 권고한다(#165). optional·비파괴: 없으면 빈 리스트
    # (기존 spec 무영향, deps/capability_requests 패턴).
    scope: list[str] = Field(default_factory=list)
    # WO#64: 반응형 tier 사다리의 *시작* 칸 힌트(예: "gpt-5.5:high"). 합성기가 "명백히
    # 어려운" 유닛(복잡 알고리즘 등)을 싼 tier 대신 더 센 tier에서 *probe*하게 한다.
    # 없으면 base=0(사다리 맨 앞=싼 tier). optional·비파괴(scope 패턴). 사다리 미지정이면
    # 무시된다(단일 tier). bar는 불변 — 시작 강도만 바꾼다(anti-erosion).
    start_tier: str = ""
    # WO#78: 이 유닛이 검증 하니스(sim:trace/헤드리스 트레이스 등)일 때, acceptance_criteria가
    # 요구하는 *증거 필드* 계약. 합성 후 결정적으로 criteria에서 **파생**해 부착한다(새 요구 추가/
    # 완화 아님 — 바가 *이미 요구하는* 증거를 명시화). 빌더 작업지시서에 "정확히 이 필드를 emit
    # 하라"로 주입되고, 게이트가 트레이스 출력(JSON)에 그 키가 다 있는지 *결정적*으로 검사한다
    # (누락→재빌드). **행동 판정(적대 run-judge)과 무관** — 적대 게이트에 *유효 증거*를 보장할 뿐.
    # optional·비파괴(scope/start_tier/reuse_of 패턴): 없으면 빈 리스트(무계약·기존 동작 그대로).
    evidence_contract: list[str] = Field(default_factory=list)
    # WO#71(②b 깊은 증분): 증분 합성(continue-from)에서 이 유닛이 *부모 run의 검증된 유닛*과
    # 동일(불변)함을 표식하는 부모 unit-id. 합성기가 단다(신규/변경 유닛은 생략). 루프가 부모
    # manifest와 acceptance_criteria·scope 동등성을 *직접 대조*해 검증한 뒤에만 done으로 시드해
    # 재빌드를 생략한다(라벨+가드 이중). **합성 라벨을 신뢰만 하지 않는다** — 바가 바뀌었으면
    # (다른 바) 무시하고 정상 빌드+gate(anti-erosion). optional·비파괴(scope/start_tier 패턴).
    reuse_of: str = ""


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


# ──────────────────────────── Facade 계약 (WO#160) ────────────────────────────


class FacadeContract(BaseModel):
    """WO#160: wire된 엔진의 *고정 facade 계약* — 합성기가 명시, 스캐폴드/트레이스/런타임-smoke가
    *동일* 계약을 참조해 추측을 제거(결정적). #158 진단: 빌더가 트레이스 import를 추측(createGameEngine
    vs 실제 GameEngine)해 ERR_MODULE_NOT_FOUND + 통합 런타임 계약 버그(Food.generate static/instance →
    빌드되나 크래시)로 미수렴. optional·비파괴: 없으면 placeholder import + smoke 미생성(기존 #157 동작).

    스캐폴드(인프라)지 단언/판정 아님 — run-judge의 #113 풀-사슬 트레이스 바는 불변(런타임-smoke는
    필요조건이지 충분조건 아님). 서버리스(#128): 헤드리스 node.
    """

    # wire된 엔진이 노출되는 workdir-상대 모듈 경로(예 "src/engine/engine.js").
    module_path: str
    # 그 모듈에서 import할 export 식별자(예 "GameEngine").
    export_name: str
    # named export(`import { X }`) 기본. class|factory|named 모두 named로 취급.
    export_kind: str = "class"
    # 인스턴스화 식(런타임-smoke용; 비면 export_kind로 추론: class→`new X()`, 그 외→`X()`).
    # 이름은 `construct_expr` — pydantic BaseModel.construct(deprecated) shadow 회피.
    construct_expr: str = ""
    # 1-tick 진행 식(런타임-smoke용; 비면 construct-only smoke — 인스턴스화 크래시만 검사).
    tick: str = ""


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
    # WO#160: wire된 엔진의 고정 facade 계약(선택). 있으면 스캐폴드가 트레이스 import를 선채움 +
    # 런타임-smoke 하니스를 생성해 빌드-passes-but-crashes(계약 불일치)를 통합서 조기 포착한다.
    # 없으면 기존 #157 동작(placeholder import·smoke 미생성). 비파괴(optional 패턴 — 라운드트립 안전).
    facade_contract: FacadeContract | None = None
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


class ArtifactDescriptor(BaseModel):
    """control/data-plane 분리(WO#102, OMC #3): 큰 산출물을 state.yaml에 인라인하지 않고
    data-plane 파일로 빼고 *참조*만 든다. 내용이 아니라 메타다.

    path:         run-dir 상대 경로(예: artifacts/trace/<id>.json).
    kind:         산출물 종류(trace/transcript/... — 2차서 cost/event/prompt 동형).
    content_hash: 무결성 검증용 해시("sha256:..."). reader가 파일-descriptor 드리프트 탐지.
    size_bytes:   원본 바이트 크기(표시·retention 판단).
    created:      생성 시각(ISO, 선택 — 결정성 위해 미주입 가능).
    retention:    보관 정책(keep/expire 등 — 후속 cleanup이 만료 prune, 이번엔 메타만).
    summary:      인라인 표시용 짧은 다이제스트(파일 미해소 환경서도 *무엇*인지 보이게).
    재사용 인프라 — reader는 resolve_artifact로 해소(해시 검증)해 *동일 내용*을 받으므로
    판정/표시 로직 불변. 추가형·비파괴(없으면 인라인 그대로 = back-compat).
    """

    path: str
    kind: str
    content_hash: str
    size_bytes: int
    created: str | None = None
    retention: str = "keep"
    summary: str = ""


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
    # WO#102: trace가 임계 초과면 *지속 시* data-plane 파일로 빼고 여기에 descriptor만(인라인 trace는 비움).
    # **판정 불변**: 행동 판정은 캡처 직후 *in-memory full trace*로 수행되고, 오프로드는 _save_state
    # 직렬화에서만 일어난다(판정 경로 무접촉). reader(대시보드·재개)는 evidence_trace로 해소 —
    # 인라인 우선, 없으면 descriptor 해소(해시 검증). 추가형·비파괴(없으면 인라인 그대로 = back-compat).
    trace_artifact: ArtifactDescriptor | None = None


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
    """LLM 호출 비용 (WO#33 계측 + WO#70 sub-attribution).

    tokens: 총 토큰(input+output) — 후방호환 필드(기존 schema/state가 쓰던 int).
    input/output: 토큰 분해(잡히면). usd: 가격표로 계산(모델 미상이면 None — 날조 금지).
    source: orchestration(합성/replan/scaffold) | executor(codex 서브프로세스) |
            judge(gate 내부 judge/run-judge) | critic(적대적 spec/decomp critic) | mixed.
    note: 못 잡는 비용을 *정직하게* 남기는 메모(예: "executor usage 미노출").
    새 필드는 전부 optional/None 기본 → 기존 Cost(tokens=…, usd=…) 그대로 유효(무회귀).

    sub-attribution(WO#70 — 44.8M이 *어디로* 갔나): 한 계측 호출(leaf)을 source 외에도
      tier/kind/unit으로 태그해 대시보드가 "u6 6.4M = xhigh 빌드 재시도 3.1M·OR 2.0M·
      run-judge 1.3M"처럼 분해해 보이게 한다. 전부 optional(미상이면 None — 날조 금지).
      tier:  실행 강도 라벨('model/effort', #64 사다리). orchestration/judge엔 보통 None.
      kind:  호출 종류 — synth|replan|scaffold|build|retry|OR|integration-OR|judge|critic.
      unit:  어느 유닛에 든 비용인지(전역 단계는 None).
      parts: 이 Cost가 여러 호출의 합(combine_costs)이면 그 *leaf*들(분해 가능 원천). 단일
             호출(leaf)이면 빈 리스트. 권위 total(tokens 등)은 그대로, parts는 분해용 추가형.
    """

    tokens: int | None = None
    usd: float | None = None
    input: int | None = None
    output: int | None = None
    source: str | None = None
    note: str | None = None
    # WO#70 sub-attribution(전부 추가형·optional — 기존 Cost 직렬화/read 무영향).
    tier: str | None = None
    kind: str | None = None
    unit: str | None = None
    parts: list["Cost"] = Field(default_factory=list)


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


# ──────────────────── ResearchBrief (분해 전 research 단계, WO#166) ────────────────────
#
# 분해 *전* director-측 research 패스(오케스트레이션 LLM = critic-model, executor 아님)가 내는
# brief — 합성기가 *입력*으로 소비해 더 정보-기반 분해(경계·계약·패턴)를 한다. **제안이지
# mandate 아님**(합성기 override 가능·적대 spec/decomp critic 그대로 작동). 판정 아님(분해 입력)
# — gate가 독립 판정. 오프라인(#32 스킬 레지스트리 + 의뢰 분석, 네트워크 0 — F.2 후속).


class CandidateUnit(BaseModel):
    """research가 제안하는 후보 disjoint-scope 유닛(#165 직접 공급 — 제안이지 강제 아님).

    scope: 이 유닛이 *배타적으로 소유*할 후보 파일(형제 간 ∩=∅ 지향, #165). deps: 후보 의존.
    합성기가 이걸 출발점으로 쓰되 override 가능(브리프는 mandate 아님).
    """

    unit: str
    desc: str
    scope: list[str] = Field(default_factory=list)
    deps: list[str] = Field(default_factory=list)


class CandidateContract(BaseModel):
    """research가 제안하는 후보 facade 계약(#160 — 유닛 간 export/import 결합, *파일 공유 아님*)."""

    producer: str                 # 이 export를 소유/생산하는 유닛
    module_path: str              # 노출 모듈 경로(예: "src/engine/engine.js")
    export_name: str              # import할 export 식별자(예: "GameEngine")
    consumers: list[str] = Field(default_factory=list)  # 이걸 import하는 유닛들


class ResearchBrief(BaseModel):
    """분해 전 research 브리프(WO#166) — director-측 계획 *입력*(제안이지 mandate 아님).

    합성기가 소비해 더 정보-기반 분해를 한다. **판정 아님** — 적대 gate/spec critic/decomp critic은
    독립 작동(brief는 분해 입력일 뿐). 완전 best-effort: research 실패/파싱불가면 None(브리프 없이
    직접 synthesize = 기존 동작). 전부 optional·기본값 → 부분 brief도 유효(graceful).

    task_analysis: 필요 서브시스템/행동 분석. stack: 스택+규약(파일 레이아웃).
    patterns: 관련 패턴(#32 레지스트리 오프라인 매칭 — 스킬명/요지). candidate_units: 후보
    disjoint-scope 분해(#165). candidate_contracts: 후보 facade 계약(#160). note: 메타/감사.
    """

    task_analysis: str = ""
    stack: str = ""
    patterns: list[str] = Field(default_factory=list)
    candidate_units: list[CandidateUnit] = Field(default_factory=list)
    candidate_contracts: list[CandidateContract] = Field(default_factory=list)
    note: str | None = None


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


# ──────────────────── run 계보 (lineage, WO#167) ────────────────────


class Lineage(BaseModel):
    """run 계보 링크(WO#167) — state.yaml 옆 lineage.json 사이드카로 영속하는 *read-only 메타*.

    비싼 런(수 M 토큰)을 fix 후 이어가는 다-런 arc(런→fix→이어가기→fix…)를 추적 가능하게 한다.
    #91 resume(--continue-from)을 leverage — C는 *링크 기록*만 추가(resume 메커니즘 자체 무변경).

    parent_run_id: --continue-from 한 부모 run id(첫 런이면 None).
    fix_ref:       부모→자식 사이 적용된 commit/WO 참조(인자 또는 현재 HEAD commit; 없으면 None).
    **verdict는 여기 중복 저장하지 않는다** — 각 run의 state.status가 단일 출처(드리프트 방지).
    대시보드 lineage 트리가 노드별로 state.status에서 verdict를, budget에서 토큰을 읽어 표시한다
    (lineage = 기록 메타+표시지 *판정 아님* — verdict를 절대 바꾸지 않는다). 추가형·비파괴:
    기존 `{parent_run_id}` 사이드카에 fix_ref만 더한 것(parent_run_id 키 보존 — 구 read 무영향).
    """

    parent_run_id: str | None = None
    fix_ref: str | None = None


# ──────────────── 완전-로컬 자급 모드 — 약-judge 플래그 + shadow 관측 (WO#171) ────────────────
#
# 새 thesis: 강 모델 0, 약 로컬 모델 하나(빌더·judge·critic 전부 로컬). judge≠builder *독립 모델*
# 분리가 단일 로컬 모델에선 불가하므로 적대성 무게가 (A)인스턴스 분리 +(B)기계적 게이트로 이전한다.
# 아래는 그 *정직 표면화*용 read-only 메타 — gate/run_judge 판정 로직·#113 바·hollow #98 불변.


class JudgeProfile(BaseModel):
    """이 run의 judge/critic/빌더/brain *실행자 정체성* + 약-judge 정직 표기(WO#171-C, read-only).

    "약-judge 런(judge_executor=local)은 강 독립 judge와 무결성 보장이 *다르다*"를 state/대시보드에
    *은폐 아니라 표면화*한다. **판정이 아니라 표기** — verdict를 절대 바꾸지 않는다(기존 gate/run_judge
    로직 불변). weak_judge=True면 적대성이 (A)빌더≠judge 인스턴스 분리 +(B)기계적 게이트로 이전됐다는
    뜻(shadow로 그 약함을 *측정* 가능). 전부 optional·기본값 → 구버전 state read 무영향(추가형·비파괴).

    brain/builder/judge/critic_executor: 각 역할의 실행자("codex"|"local"|"human").
    judge_model:  judge가 쓴 모델명(local이면 로컬 서빙 모델, codex면 --judge-model/codex 기본).
    weak_judge:   judge_executor=="local" (강 독립 judge가 아님 — 무결성 보장 다름).
    shadow_judge: shadow 비교 judge 실행자("codex"|None). None=shadow OFF(100% 로컬·codex 흔적 0).
    note:         사람용 정직 한 줄(예: "약-judge 런: 적대성=기계적 게이트+인스턴스 분리").
    """

    brain_executor: str = "codex"
    builder_executor: str = "human"
    judge_executor: str = "codex"
    critic_executor: str = "codex"
    judge_model: str | None = None
    weak_judge: bool = False
    shadow_judge: str | None = None
    note: str | None = None


class ShadowComparison(BaseModel):
    """약 judge(적용 verdict 권위) vs 강 shadow judge(기록만)의 한 게이트 비교(WO#171-shadow, 적용 0).

    `--shadow-judge codex`(opt-in)일 때만 누적된다. 약 judge가 *같은 산출물*을 판정해 verdict를
    적용하고, codex가 *같은 workdir*를 shadow 판정해 **나란히 기록만** 된다(적용 0 — verdict 권위는
    언제나 약 judge). 검증역전(inverted: 약=pass인데 강=fail)이 *약 self-judge가 어디서 봐주는지*
    데이터다. locus는 그 역전이 LLM 판정 갭(흥미로운 신호)인지 기계 체크 차이(드문 비결정/flaky)인지
    구분한다(#113 기계 신호는 결정적이라 보통 일치 — 역전은 LLM 쪽). shadow OFF면 빈 리스트(codex 0).

    unit:            어느 게이트인지(유닛-id, None=통합/순차).
    primary_verdict: 적용된 약(로컬) judge verdict.
    shadow_verdict:  codex shadow judge verdict(기록만 — 적용 0).
    inverted:        약=pass & 강=fail (검증역전 — 약 judge 관대 지점).
    locus:           역전/차이 위치 "llm"|"mechanical"|"mixed"|None(차이 없음).
    detail:          차이 난 check ac_id/요지(감사용 한 줄).
    """

    unit: str | None = None
    primary_verdict: str
    shadow_verdict: str
    inverted: bool = False
    locus: str | None = None
    detail: str | None = None


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
    # WO#70 비용 분해 ledger — *모든* 계측 leaf(synth/scaffold/replan/build/retry/OR/judge/
    # critic)를 budget.spent에 누적하는 *그 자리에서* append한다(단일 chokepoint=account).
    #   → Σcost_parts.tokens == budget.spent.tokens 가 구성적으로 보장(정합 by construction).
    #   대시보드가 이걸 source×tier×kind로 집계해 "토큰이 어디로 갔나"를 드릴다운한다.
    #   추가형·append-only — 기존 read 무영향, 부재(구버전 state)면 빈 리스트(graceful).
    cost_parts: list[Cost] = Field(default_factory=list)
    # WO#171-C: 약-judge 정직 표기(judge/critic/빌더/brain 실행자 정체성 + weak_judge·shadow). read-only
    #   메타 — verdict를 절대 바꾸지 않는다(기존 gate/run_judge 로직 불변). 부재(구버전/강-judge 런)면 None.
    judge_profile: JudgeProfile | None = None
    # WO#171-shadow: 약=적용·강=기록만(적용 0) 검증역전 누적. `--shadow-judge` opt-in일 때만 채워진다.
    #   shadow OFF(기본)면 빈 리스트(100% 로컬·codex 흔적 0). 추가형·비파괴(구버전 state read 무영향).
    shadow_comparisons: list[ShadowComparison] = Field(default_factory=list)

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
