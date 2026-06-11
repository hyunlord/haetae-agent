"""loop driver — synthesize → replan → dispatch → gate → 반복.

손으로 돌리던 director 루프(1~6번)의 코드판. 이번 WO는 executor/gate를 주입 가능한
Protocol + mock으로 두고 *오케스트레이션 흐름*만 증명한다.
(budget/stuck 정식 처리, governed spec-change 적용, 실제 executor/gate 어댑터는 이후 WO.)
"""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol, runtime_checkable

import yaml

from haetae import intake, replan as replan_mod, scaffold as scaffold_mod, spec_critic as critic_mod
from haetae.capability import PocRunner, governed_capability_preflight
from haetae.deps import Runner as DepsRunner, ensure_deps
from haetae.executors import Tier, tier_label
from haetae.intake import (
    SynthesisError,
    nudge_disjoint_scope,
    nudge_integration_deps,
    synthesize,
    unit_bar_signature,
)
from haetae.llm import CodexStalled, CodexUsageLimitError, LLMClient
from haetae.metering import (
    MeteredClient,
    accumulate,
    combine_costs,
    cost_from_usage,
    cost_leaves,
    tag_cost,
)
from haetae.scaffold import (
    Scaffold,
    commit_scaffold,
    generate_scaffold,
    prepare_worktree_deps,
    write_scaffold,
)
from haetae.models import (
    Action,
    Activity,
    ApproachAttempt,
    CheckReport,
    Cost,
    DecompCritique,
    Event,
    GateResult,
    NextOrder,
    PlanItem,
    PlanState,
    ProjectSpec,
    SpecCritique,
    StageTransition,
    State,
    Status,
    Verdict,
)


def _utcnow_iso() -> str:
    """현재 UTC를 ISO 8601(초 단위, Z)로. 단계/이벤트 타임스탬프 기본값."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# 단계 이름(WO#33 Part B). 전이 이력/activity/event.stage에 쓰는 canonical 문자열.
STAGE_SYNTHESIZE = "synthesize"
STAGE_SCAFFOLD = "scaffold"
STAGE_BUILD = "build"
STAGE_VERIFY = "verify"
STAGE_REPLAN = "replan"
STAGE_DONE = "done"
STAGE_ESCALATE = "escalate"
STAGE_DECOMP_REJECT = "decomp-reject"  # WO#40: 분해 critic이 무진전 work order를 reject→재replan
STAGE_OR_ALTERNATIVE = "or-alternative"  # WO#41: gate 실패 소진 → 다른 접근으로 백트래킹·재시도
STAGE_AUTO_CONFIG = "auto-config"  # WO#65: --auto가 해석한 운영 config(사다리·critic·scaffold 등) 기록
STAGE_REUSE = "reuse"  # WO#71: continue-from서 검증된 부모 유닛을 done으로 시드(재빌드 생략)
STAGE_REBUILD = "rebuild"  # WO#71: reuse_of 있으나 바 변경/부모 미검증 → 재사용 거부·정상 빌드(anti-erosion)
from haetae.decomp_critic import (
    DEFAULT_DECOMP_CRITIC_PROMPT_PATH,
    build_decomp_feedback,
    critique_decomposition,
    is_weak,
)
from haetae.or_node import (
    build_alternative_feedback,
    build_integration_feedback,
    summarize_gate_evidence,
)
from haetae.replan import ReplanError, replan
from haetae.scheduler import all_done, ready_units
from haetae.skills import Skill, inject_skills, load_skills, match_skills
from haetae.spec_change import apply_spec_change
from haetae.spec_critic import synthesize_with_critique
from haetae.worktree import WorktreeError, WorktreeManager

# 병렬 모드에서 worktree 경로를 받아 그 경로에 묶인 Executor/Gate를 만드는 팩토리.
# (Executor/Gate는 생성 시 workdir이 고정되므로 unit마다 worktree로 다시 묶어야 한다.)
#
# WO#64: executor_factory는 *선택적으로* tier 인자를 받는다 — `(wt)` 또는 `(wt, tier)`.
# 2-arg 팩토리면 루프가 그 시도의 Tier(model/effort)를 넘겨 그 강도의 executor를 만든다.
# 1-arg 팩토리(기존 전부)면 wt만 받는다(단일 tier·후방호환 — 661 무회귀). _build_executor가
# 팩토리 arity를 보고 알아서 분기하므로 호출부는 항상 tier를 *제공만* 하면 된다.
ExecutorFactory = Callable[..., "Executor"]
GateFactory = Callable[[Path], "Gate"]


def _factory_accepts_tier(factory: ExecutorFactory) -> bool:
    """팩토리가 tier(2번째 위치 인자)를 받는지 시그니처로 판정(best-effort).

    `(wt, tier)`/`*args`면 True, `(wt)`면 False. 시그니처를 못 읽으면(빌트인 등) False로
    안전 폴백(기존 1-arg 경로). 이 덕에 기존 1-arg 팩토리(661 테스트)는 그대로 동작한다.
    """
    import inspect

    try:
        params = list(inspect.signature(factory).parameters.values())
    except (TypeError, ValueError):
        return False
    if any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in params):
        return True
    positional = [
        p for p in params
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    return len(positional) >= 2


def _build_executor(factory: ExecutorFactory, wt: Path, tier: Tier | None) -> "Executor":
    """팩토리로 executor를 만든다. tier-aware(2-arg)면 tier를 넘기고, 아니면 wt만(후방호환).

    tier=None(사다리 미지정)이거나 1-arg 팩토리면 항상 `factory(wt)` — 기존 동작 그대로.
    """
    if tier is not None and _factory_accepts_tier(factory):
        return factory(wt, tier)
    return factory(wt)


def resolve_start_tier(start_tier: str, ladder: list[Tier]) -> int:
    """유닛의 start_tier 힌트를 사다리 *시작 인덱스*로 해석한다(WO#64).

    매칭 규칙(관대): start_tier 문자열이 어떤 tier의 'model:effort' / 'model/effort' /
    'model'과 같으면 그 인덱스. 못 맞추거나 비어있으면 0(맨 앞=싼 tier — 비파괴).
    cap = len(ladder)-1(상한 초과 금지). 사다리가 단일이면 항상 0.
    """
    s = (start_tier or "").strip()
    if not s or len(ladder) <= 1:
        return 0
    for i, t in enumerate(ladder):
        labels = {
            f"{t.model}:{t.reasoning_effort}",
            f"{t.model or '-'}/{t.reasoning_effort or '-'}",
        }
        if t.model:
            labels.add(t.model)
        if s in labels:
            return min(i, len(ladder) - 1)
    return 0


# ──────────────────── 검증된 유닛 재사용 (②b 깊은 증분, WO#71) ────────────────────


class ReuseDecision:
    """한 유닛의 재사용/재빌드 결정(이벤트·transition 기록용). 가벼운 값 객체."""

    __slots__ = ("unit", "parent", "reused", "reason")

    def __init__(self, unit: str, parent: str, reused: bool, reason: str):
        self.unit = unit
        self.parent = parent
        self.reused = reused
        self.reason = reason


def evaluate_reuse(
    spec: ProjectSpec,
    reuse_manifest: dict | None,
    *,
    reuse_on: bool = True,
) -> list[ReuseDecision]:
    """새 spec의 `reuse_of` 유닛을 부모 done-manifest와 대조해 재사용/재빌드를 결정한다.

    anti-erosion 가드(검증 우회 금지):
      - reuse_on=False(--no-reuse)거나 manifest 없으면 → 빈 리스트(전부 정상 빌드, 무변경).
      - 새 유닛에 reuse_of가 없으면 → 결정 없음(신규/변경 유닛은 정상 빌드).
      - reuse_of=<pid>인데 부모 manifest에 pid가 없으면(부모서 non-done/없음 = 검증 불가) →
        **재사용 거부·재빌드**(crashed-parent graceful).
      - pid가 있어도 acceptance_criteria·scope 지문이 *다르면*(바 변경) → **재사용 거부·재빌드+
        재gate**. 합성 라벨을 신뢰만 하지 않고 지문을 직접 대조한다(라벨+가드 이중).
      - 부모 done + 지문 불변일 때만 reused=True(done 시드 대상).
    순수 함수(IO/LLM 없음) — 결정만 돌려준다. 시드/이벤트는 호출부가 한다.
    """
    decisions: list[ReuseDecision] = []
    # manifest=None = continue-from 아님(무변경). 빈 dict {}는 *continue-from인데 부모 done 0*
    # (crashed/half) — 그땐 reuse_of 유닛마다 '부모 미검증→재빌드' 결정을 내야 하므로 통과시킨다.
    if not reuse_on or reuse_manifest is None:
        return decisions
    for u in spec.decomposition:
        pid = (getattr(u, "reuse_of", "") or "").strip()
        if not pid:
            continue  # 신규/변경 유닛 — 정상 빌드(결정 없음)
        parent_sig = reuse_manifest.get(pid)
        if parent_sig is None:
            decisions.append(ReuseDecision(
                u.unit, pid, False,
                f"부모 '{pid}' 미검증(non-done/없음) → 재빌드 (crashed-parent graceful)"))
            continue
        new_sig = unit_bar_signature(spec, u.unit)
        if new_sig == parent_sig:
            decisions.append(ReuseDecision(
                u.unit, pid, True, f"부모 '{pid}' 재사용 — acceptance_criteria·scope 불변"))
        else:
            decisions.append(ReuseDecision(
                u.unit, pid, False,
                f"criteria/scope 변경(vs 부모 '{pid}') → 재빌드+재gate (anti-erosion)"))
    return decisions


# ──────────────────────────── 주입 인터페이스 ────────────────────────────


@runtime_checkable
class Executor(Protocol):
    """work order를 실행하고 결과 요약 텍스트를 반환한다."""

    def run(self, order: NextOrder) -> str: ...


@runtime_checkable
class Gate(Protocol):
    """실행 결과를 spec 기준으로 판정해 verdict + 근거(GateResult)를 반환한다.

    근거(per-check 증거)는 mutable 속성이 아니라 반환 계약이다 — 루프가 그대로
    Event.checks에 실어 state 파일을 진짜 감사 로그로 만든다.

    unit (WO#26): per-unit gate(worktree)는 unit=그 유닛-id로 호출돼 *그 유닛 태그된
    기준만* 검사한다. 통합 gate·순차 경로는 unit=None(기본)으로 전체 spec을 검사한다.
    """

    def judge(self, result: str, spec: ProjectSpec, unit: str | None = None) -> GateResult: ...


class MockExecutor:
    """테스트용. 스크립트된 결과를 순서대로(소진 시 마지막을 반복) 반환."""

    def __init__(self, results: list[str] | str):
        self._results = [results] if isinstance(results, str) else list(results)
        self._i = 0
        self.calls: list[NextOrder] = []

    def run(self, order: NextOrder) -> str:
        self.calls.append(order)
        r = self._results[min(self._i, len(self._results) - 1)]
        self._i += 1
        return r


class MockGate:
    """테스트용. 스크립트된 Verdict를 순서대로(소진 시 마지막을 반복) GateResult로 반환.

    checks: 매 judge 호출이 동봉할 per-check 증거(선택). 모든 호출에 같은 리스트를
    재사용한다. 기본 None=빈 근거. 근거가 Event.checks까지 흐르는지 검증용.
    """

    def __init__(
        self,
        verdicts: list[Verdict] | Verdict,
        checks: list[CheckReport] | None = None,
    ):
        self._v = [verdicts] if isinstance(verdicts, Verdict) else list(verdicts)
        self._checks = checks
        self._i = 0
        self.calls: list[str] = []

    def judge(self, result: str, spec: ProjectSpec, unit: str | None = None) -> GateResult:
        self.calls.append(result)
        v = self._v[min(self._i, len(self._v) - 1)]
        self._i += 1
        return GateResult(verdict=v, checks=list(self._checks or []))


# ──────────────────────────── 내부 헬퍼 ────────────────────────────


def _init_state(spec: ProjectSpec) -> State:
    plan = [
        PlanItem(unit=u.unit, state=PlanState.pending, deps=(u.deps or None))
        for u in spec.decomposition
    ]
    return State(
        spec_ref=spec.spec_id,
        spec_version=spec.version,
        status=Status.running,
        plan=plan,
    )


def _escalated_no_spec(reason: str, raw_response: str | None) -> State:
    """spec이 없을 때(합성 실패) 구성하는 최소 escalated State.

    spec_ref는 placeholder. 진짜 원인은 pending_escalations에 raw와 함께 남긴다.
    """
    note: dict = {"reason": reason}
    if raw_response is not None:
        note["raw_response"] = raw_response
    return State(
        spec_ref="(synthesis-failed)",
        spec_version=0,
        status=Status.escalated,
        pending_escalations=[note],
    )


def _advance_done(state: State, unit: str) -> None:
    """직전 작업 유닛 수용 표시(brain advance = done). 단, failed 유닛은 보존한다.

    전체-spec gate라 중간 유닛은 개별 done 신호를 못 받는다(WO#25 #3). brain이 다음
    유닛으로 넘어가는 것 자체가 직전 유닛을 수용한 신호 → done으로 올린다.
    """
    for item in state.plan:
        if item.unit == unit:
            if item.state != PlanState.failed:
                item.state = PlanState.done
            return


def _set_plan_state(state: State, unit: str, plan_state: PlanState) -> None:
    """병렬 스케줄러용 직접 plan 상태 설정(verdict 매핑 없이 — 스케줄러가 직접 제어).

    재dispatch는 failed/in_progress를 다시 pending으로 되돌려 frontier에 재진입시킨다.
    """
    for item in state.plan:
        if item.unit == unit:
            item.state = plan_state
            return


def _executor_cost(executor: "Executor", pricing) -> Cost | None:
    """executor의 last_usage를 읽어 executor-source Cost로 변환(읽기만, best-effort).

    - usage 있음 → cost_from_usage(source=executor).
    - usage-capable인데(last_usage 속성 보유) 값이 None → 정직하게 null+note.
    - 비-LLM executor(속성 없음, 예: human relay/mock) → None(귀속할 비용 없음).
    예외는 흡수(None) — 계측이 run을 죽이지 않는다.
    """
    try:
        if not hasattr(executor, "last_usage"):
            return None
        usage = getattr(executor, "last_usage", None)
        if usage is not None:
            return cost_from_usage(usage, source="executor", pricing=pricing)
        return Cost(source="executor", note="executor usage 미노출")
    except Exception:  # noqa: BLE001 — best-effort 계측
        return None


def _exec_and_gate(
    executor: "Executor", gate: "Gate", order: NextOrder, spec: ProjectSpec,
    pricing=None, heartbeat=None, build_kind: str = "빌드",
) -> tuple[str, GateResult, Cost | None]:
    """ThreadPoolExecutor 워커 — 느린 부분(executor 실행 + unit gate)만 병렬화한다.

    brain(work order 생성)과 worktree 생성/머지는 main 스레드에서 직렬·결정적으로
    처리하므로 여기엔 mock 시퀀스 race가 없다. 예외는 fut.result()로 전파된다.

    WO#33: executor 비용은 *그 워커 전용* executor 인스턴스의 last_usage에서 읽으므로
    스레드 안전(공유 상태 변이 없음)하다. 반환에 동봉해 main 스레드가 이벤트에 귀속한다.

    WO#26: per-unit gate는 order.unit으로 호출돼 *그 유닛 태그된 기준만* 검사한다
    (전체-spec 기준은 통합 gate로 연기 → 기반 유닛이 전체-시스템 기준 때문에
    escalate하던 회귀 해소). 통합 gate는 main에서 unit 없이(전체) 호출된다.
    """
    # WO#55: 빌드/judge codex 호출을 *이 워커 스레드*에 라이브 컨텍스트로 깐다(best-effort).
    # 워커별 executor/gate 인스턴스라 스레드당 한 번에 한 호출 → 컨텍스트 race 없음.
    if heartbeat is not None:
        try:
            heartbeat.set_context(build_kind, order.unit)  # WO#64: tier 라벨이 실린 build_kind
        except Exception:  # noqa: BLE001
            pass
    result = executor.run(order)
    exec_cost = _executor_cost(executor, pricing)
    if heartbeat is not None:
        try:
            heartbeat.set_context("judge", order.unit)
        except Exception:  # noqa: BLE001
            pass
    return result, gate.judge(result, spec, unit=order.unit), exec_cost


def _save_state(state: State, state_path: str | Path) -> None:
    Path(state_path).write_text(
        yaml.safe_dump(
            state.model_dump(by_alias=True, mode="json"),
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _save_spec(spec: ProjectSpec, state_path: str | Path) -> None:
    """검증된 spec을 state.yaml *옆* spec.yaml에 기록(WO#58 사이드카 토대).

    state.yaml 스키마는 불변 — spec은 별 파일. 이어가기(②a)의 부모 컨텍스트 원천이자,
    대시보드 spec 보강(goal/done_when/유닛 desc)의 자동탐지 대상. 호출부에서 best-effort로 감싼다.
    """
    Path(Path(state_path).parent / "spec.yaml").write_text(
        spec.to_yaml(), encoding="utf-8"
    )


# ──────────────────────────── graceful stop (WO#43) ────────────────────────────

# 웹 stop(#37)이 보내는 SIGINT/Ctrl-C가 KeyboardInterrupt로 올라올 때 남기는 한 줄.
_INTERRUPT_MSG = "중단됨 (사용자 stop/SIGINT)"


def _finalize_interrupt(
    state: State,
    emit: Callable[[str], None],
    save: Callable[[], None],
) -> None:
    """KeyboardInterrupt(웹 stop/SIGINT) 공통 마무리 — best-effort, **2차 크래시 금지**.

    멈춤을 깔끔히 닫는다: traceback 없이 "중단됨" 한 줄 + 종료 상태 봉인 + 부분 진행 저장.
      1) 로그 한 줄(_INTERRUPT_MSG) — 호출부가 raw traceback 대신 이걸 남긴다.
      2) running이면 stopped(stopped_stuck)로 종료 상태 봉인 → 대시보드가 "중단됨"으로
         보이고 "running"으로 오해하지 않는다(이미 terminal이면 그 값을 보존).
      3) 현재 state 저장(#18) — 그때까지의 부분 진행을 감사 로그/대시보드에 남긴다.
    정리/저장 중 추가 예외도 전부 흡수한다(인터럽트 처리가 또 다른 크래시가 되면 안 됨).
    worktree/산출물 정리(#21 cleanup_all)는 호출부(_parallel_loop)의 finally가 보장한다.
    """
    try:
        emit(_INTERRUPT_MSG)
    except Exception:  # noqa: BLE001 — best-effort, 로그 실패가 정리를 막지 않는다
        pass
    try:
        if state is not None and state.status == Status.running:
            state.status = Status.stopped_stuck
    except Exception:  # noqa: BLE001
        pass
    try:
        save()
    except Exception:  # noqa: BLE001 — 저장 실패가 2차 크래시가 되면 안 된다
        pass


# ──────────────────────────── 진행 표시 헬퍼 ────────────────────────────


def _truncate(text: str, limit: int = 60) -> str:
    """여러 줄/공백을 한 줄로 접고 limit자로 자른다(진행 메시지용)."""
    one_line = " ".join((text or "").split())
    return one_line if len(one_line) <= limit else one_line[: limit - 1] + "…"


def _summarize_gate(gr: GateResult) -> str:
    """gate 판정을 한 줄로: verdict + 체크 요약(첫 실패 체크의 cmd/exit 포함)."""
    v = gr.verdict.value
    checks = gr.checks
    failed = [c for c in checks if c.status == "fail"]
    if failed:
        c = failed[0]
        what = c.cmd or c.ac_id or c.check_type.value
        ec = "" if c.exit_code is None else f" (exit {c.exit_code})"
        return f"gate: {v} — {_truncate(what, 40)}{ec}"
    if checks:
        passed = sum(1 for c in checks if c.status == "pass")
        return f"gate: {v} ({passed}/{len(checks)} 통과)"
    return f"gate: {v}"


def _critique_label(crit: SpecCritique) -> str:
    """spec critic 비평을 한 줄 progress로: 재합성/평가불가/soft/adequate."""
    if crit.resynthesized:
        return "spec critic: soft — 1회 재합성"
    if crit.note and "평가 불가" in crit.note:
        return "spec critic: (평가 불가)"
    if crit.verdict == "soft":
        # soft지만 재합성이 안 일어남(구체 gap 없음 or 재합성 폴백) → 원본 유지.
        return "spec critic: soft — 원본 유지"
    return "spec critic: adequate"


def _final_label(state: State) -> str:
    """종료 라벨: escalated면 직전 escalation 사유를 한 줄로 덧붙인다."""
    if state.status == Status.escalated and state.pending_escalations:
        last = state.pending_escalations[-1]
        reason = None
        if isinstance(last, dict):
            reason = last.get("reason") or last.get("question")
        else:
            reason = str(last)
        if reason:
            return f"종료: {state.status.value} — {_truncate(str(reason))}"
    return f"종료: {state.status.value}"


# ──────────────────────────── 루프 ────────────────────────────


def run_loop(
    order: str,
    client: LLMClient,
    executor: Executor,
    gate: Gate,
    *,
    critic_client: LLMClient | None = None,
    max_iters: int = 30,
    replan_retries: int = 2,
    decomp_critic: bool = True,
    decomp_retries: int = 1,
    or_alternatives: int = 1,
    prompt_dir: str | Path | None = None,
    state_path: str | Path | None = None,
    progress: Callable[[str], None] | None = None,
    max_parallel: int = 1,
    workdir: str | Path | None = None,
    executor_factory: ExecutorFactory | None = None,
    gate_factory: GateFactory | None = None,
    unit_retries: int = 1,
    tier_ladder: list[Tier] | None = None,
    auto_config_note: str | None = None,
    worktree_manager: WorktreeManager | None = None,
    scaffold_client: LLMClient | None = None,
    install_deps: bool = True,
    deps_runner: DepsRunner | None = None,
    synth_context: str | None = None,
    seeded: bool = False,
    reuse_manifest: dict | None = None,
    reuse: bool = True,
    skills_dir: str | Path | None = None,
    pricing: dict | None = None,
    clock: Callable[[], str] | None = None,
    activity_observer: Callable[[list["Activity"]], None] | None = None,
    heartbeat=None,
    capabilities_on: bool = False,
    capability_registry_path: str | Path | None = None,
    capability_allowlist: list[str] | None = None,
    capability_poc_runner: PocRunner | None = None,
    capability_searcher=None,
    max_tokens: int | None = None,
    unit_attempt_budget: int | None = None,
    unit_token_budget: int | None = None,
) -> State:
    """주문 한 줄에서 종료 상태까지 루프를 돈다. 최종 State를 반환(필요시 저장).

    계측(WO#33, best-effort — 계측 실패는 절대 run을 죽이지 않는다):
      - 토큰/코스트: brain client(합성/replan/critic/scaffold)와 codex executor의
        token usage를 캡처해 state.budget.spent에 누적하고, 그 유닛 처리에 든 비용을
        해당 event.cost로 귀속한다(source=orchestration|executor|mixed). usd는
        pricing(model→단가)으로 계산하되 모델 미상이면 None(날조 금지).
      - 단계/activity: synthesize/scaffold/build/verify/replan 진입을 state.transitions에
        타임스탬프와 함께 기록하고, 현재 in-flight 유닛을 state.activity에 라이브로 둔다
        (dispatch→build, gate→verify, 완료→제거). activity_observer가 주어지면 변화 시
        스냅샷을 흘려준다(대시보드/테스트용).
      pricing/clock/activity_observer 기본값은 무해(None) → 기존 동작·테스트 불변.
      clock: 타임스탬프 생성기(테스트 주입용). 기본 None=실제 UTC ISO.

    스킬 주입(WO#32, Phase B): skills_dir가 주어지면 읽기전용 패턴 문서를 로드해
      각 유닛 work order에 *executor 넘기기 직전* 매칭 주입한다(빌더 전용). judge/
      gate/critic 경로엔 절대 주입하지 않는다(검증 독립성). skills_dir=None이거나
      매칭 0이면 no-op(기존 동작 불변).

    내성(WO#12): LLM 출력 하나로 루프가 crash하지 않는다.
      - 합성(synthesize) 실패: traceback 대신 escalated State 반환.
      - replan 출력 검증 실패: 최대 replan_retries회 재시도(직전 에러를 피드백으로
        얹어 self-correction 유도), 소진 시 escalate하고 루프 종료(예외 전파 안 함).

    progress: 단계 진입 시 한 줄 상태를 받는 콜백(WO#13). 기본 None=no-op이라
      테스트엔 아무것도 새지 않는다. CLI는 stderr로 찍는 함수를 주입해 느린 codex
      호출이 "행"으로 안 보이게 한다.

    병렬 실행(WO#21): max_parallel>1이면 git worktree 격리 + 결정적 DAG 스케줄러로
      ready unit들을 동시에 굴린다. **max_parallel<=1이면 기존 순차 경로 그대로**
      (worktree·동시성 미사용 → 무회귀). 선형 deps DAG는 자연히 직렬화되어
      순차와 동일한 최종 결과를 낸다.
      - workdir: 통합(main) 브랜치가 사는 git 루트. worktree들이 여기서 분기/머지.
      - executor_factory/gate_factory: worktree 경로를 받아 그 경로에 묶인 unit용
        executor/gate를 만든다(기본: 주입된 executor/gate를 경로 무시하고 재사용).
        통합 gate(머지된 main 1회 검사)는 항상 인자로 받은 `gate`를 쓴다.
      - unit_retries: gate 실패/머지 충돌 시 그 unit을 재dispatch하는 최대 횟수.
      - 이벤트는 완료 타이밍과 무관하게 (unit-id, attempt)로 정렬해 결정성을 봉인.
    """

    def emit(msg: str) -> None:
        if progress is not None:
            progress(msg)

    def hb(call_kind: str, unit: str | None = None) -> None:
        """WO#55: codex 호출 직전 라이브 하트비트 컨텍스트(종류·유닛)를 *이 스레드*에 깐다.

        codex 클라이언트가 _run 시작 시 이 컨텍스트를 읽어 활성으로 표면화한다. heartbeat가
        없으면 no-op. best-effort(컨텍스트 설정 실패가 run을 죽이지 않는다 — 순수 텔레메트리).
        """
        if heartbeat is None:
            return
        try:
            heartbeat.set_context(call_kind, unit)
        except Exception:  # noqa: BLE001
            pass

    def try_save() -> None:
        """비치명적 + 증분 저장. write 실패해도 run을 죽이지 않고 경고만 흘린다.

        이벤트 append/spec 변경/종료마다 호출 → Ctrl-C나 후반 실패에도 그때까지의
        감사 로그가 파일에 남는다. state_path가 없으면 no-op.
        """
        if state_path is None:
            return
        try:
            _save_state(state, state_path)
        except Exception as e:  # noqa: BLE001 — 저장 실패는 run을 죽이면 안 된다
            emit(f"⚠ state 저장 실패: {state_path} ({e}) — run은 정상 완료됨")

    def save_spec(spec_obj: ProjectSpec) -> None:
        """검증된 spec을 spec.yaml 사이드카로 best-effort 기록(WO#58). 실패해도 run 진행.

        합성 직후 + governed spec-change 후 호출 → 이어가기(②a) 부모 컨텍스트·대시보드
        spec 보강의 원천. state_path 없으면 no-op(스키마/state 동작 불변).
        """
        if state_path is None:
            return
        try:
            _save_spec(spec_obj, state_path)
        except Exception as e:  # noqa: BLE001 — 사이드카 쓰기 실패는 run을 죽이지 않는다
            emit(f"⚠ spec.yaml 사이드카 저장 실패 ({e}) — run은 정상 진행")

    # ── 계측 헬퍼(WO#33) — 전부 best-effort: 어떤 예외도 run을 죽이지 않는다 ──────
    def now() -> str | None:
        try:
            return clock() if clock is not None else _utcnow_iso()
        except Exception:  # noqa: BLE001
            return None

    def account(cost: Cost | None) -> None:
        """budget.spent에 누적 + 분해 ledger(state.cost_parts)에 leaf append(best-effort).

        WO#70: account가 budget.spent로 가는 *유일한* 길목이라, 여기서 같은 cost의 leaf를
        ledger에 적재하면 Σledger.tokens == budget.spent.tokens 가 구성적으로 보장된다
        (정합 by construction). leaf는 이미 호출부에서 source/tier/kind/unit 태그됨.
        """
        try:
            accumulate(state.budget.spent, cost)
            for leaf in cost_leaves(cost):
                state.cost_parts.append(leaf)
        except Exception:  # noqa: BLE001
            pass

    def over_global_budget() -> bool:
        """WO#68 (B): 누적 토큰(#33 계측)이 --max-tokens를 넘었나(opt-in, 미설정=무제한·무회귀).

        외부(codex) 컷오프 전에 *의도적*으로 멈추기 위한 안전 그물. best-effort(읽기 실패=미초과).
        """
        if max_tokens is None:
            return False
        try:
            return (state.budget.spent.tokens or 0) > max_tokens
        except Exception:  # noqa: BLE001
            return False

    def record_transition(stage: str, unit: str | None = None) -> None:
        try:
            state.transitions.append(StageTransition(stage=stage, unit=unit, ts=now()))
        except Exception:  # noqa: BLE001
            pass

    def observe_activity() -> None:
        if activity_observer is None:
            return
        try:
            activity_observer([a.model_copy() for a in state.activity])
        except Exception:  # noqa: BLE001
            pass

    def activity_start(unit: str | None, stage: str) -> None:
        try:
            state.activity.append(Activity(unit=unit, stage=stage, started_at=now()))
        except Exception:  # noqa: BLE001
            pass
        observe_activity()

    def activity_set_stage(unit: str | None, stage: str) -> None:
        try:
            for a in state.activity:
                if a.unit == unit:
                    a.stage = stage
                    break
        except Exception:  # noqa: BLE001
            pass
        observe_activity()

    def activity_end(unit: str | None) -> None:
        try:
            state.activity = [a for a in state.activity if a.unit != unit]
        except Exception:  # noqa: BLE001
            pass
        observe_activity()

    # brain/critic/scaffold 호출을 metering으로 감싼다(orchestration source). inner의
    # complete 반환은 그대로 통과하므로 호출부·테스트는 불변. drain()으로 적립분을 꺼낸다.
    m_client = MeteredClient(client, source="orchestration", pricing=pricing)
    # WO#70: critic(적대적 spec/decomp critic)은 *독립 source*로 귀속 — orchestration과 분리해
    # "검증 측 비용이 얼마나 드나"를 따로 본다(적대 분리의 비용 가시화). 판정 행동 불변.
    m_critic = (
        MeteredClient(critic_client, source="critic", pricing=pricing)
        if critic_client is not None
        else None
    )
    m_scaffold = (
        MeteredClient(scaffold_client, source="orchestration", pricing=pricing)
        if scaffold_client is not None
        else None
    )

    syn_prompt = (
        Path(prompt_dir) / "synthesizer.md" if prompt_dir else intake.DEFAULT_PROMPT_PATH
    )
    rep_prompt = (
        Path(prompt_dir) / "replan.md" if prompt_dir else replan_mod.DEFAULT_PROMPT_PATH
    )
    critic_prompt = (
        Path(prompt_dir) / "spec_critic.md"
        if prompt_dir
        else critic_mod.DEFAULT_CRITIC_PROMPT_PATH
    )
    decomp_critic_prompt = (
        Path(prompt_dir) / "decomp_critic.md"
        if prompt_dir
        else DEFAULT_DECOMP_CRITIC_PROMPT_PATH
    )
    scaffold_prompt = (
        Path(prompt_dir) / "scaffold.md"
        if prompt_dir
        else scaffold_mod.DEFAULT_SCAFFOLD_PROMPT_PATH
    )

    # 스킬 레지스트리(빌더 전용) — best-effort 로드. 매칭 주입은 executor dispatch 직전에만.
    skills: list[Skill] = load_skills(skills_dir) if skills_dir else []
    if skills:
        emit(f"스킬 로드: {len(skills)}개")

    def apply_skills(order: NextOrder) -> NextOrder:
        """work order에 매칭 스킬을 주입한 *복사본*을 반환(executor에만 전달).

        매칭은 유닛 작업지시서 텍스트(goal/scope/deliverable/context_refs)에 대해,
        주입은 scope 필드에 `## 참고 패턴 (스킬)` 섹션으로. 원본 order는 불변이라
        Event/gate로 흘러가는 값엔 스킬이 새지 않는다(분리 보존).
        """
        if not skills:
            return order
        match_text = "\n".join(
            t for t in [order.goal, order.scope, order.deliverable, *order.context_refs] if t
        )
        matched = match_skills(skills, match_text)
        if not matched:
            return order
        return order.model_copy(update={"scope": inject_skills(order.scope or "", matched)})

    # 분해 critic(WO#40, Phase C): replan이 낸 work order의 *진전성*을 독립 critic이 판정.
    # **적대적 분리**: 독립 client(critic-model)만 쓰고, 스킬 미주입 *원본* order를 본다
    #   (apply_skills는 executor로 가는 복사본에만 — critic은 raw order). 빌더/검증 분리.
    # **기본 on, opt-out**: decomp_critic=False(--no-decomp-critic)면 OFF. critic_client가
    #   없으면(=--critic-model 미설정) 돌릴 독립 모델이 없으므로 자동 OFF(미설정→진행).
    decomp_critic_on = decomp_critic and critic_client is not None

    def run_decomp_critic(no: NextOrder, last: str) -> DecompCritique | None:
        """원본 work order(스킬 미주입)를 독립 critic으로 판정. OFF면 None.

        m_critic(독립 critic-model의 metered 래퍼)로 호출 → 비용은 호출부가 drain/account.
        best-effort: critique_decomposition 자체가 절대 raise하지 않는다(progress로 흡수).
        """
        if not decomp_critic_on or m_critic is None:
            return None
        return critique_decomposition(
            no, spec, state, m_critic, last_result=last, prompt_path=decomp_critic_prompt
        )

    # graceful stop(WO#43): 웹 stop(#37)이 보내는 SIGINT(KeyboardInterrupt)를 잡아
    #   traceback 없이 정리·저장·클린 종료한다. 합성 전 인터럽트도 잡도록 placeholder
    #   state를 먼저 둔다(정상 경로에서 _init_state/_escalated_no_spec가 덮어쓴다).
    state = State(spec_ref="(interrupted)", spec_version=0, status=Status.running)
    try:
        # WO#65: --auto가 해석한 운영 config를 한 줄 이벤트(emit)로 즉시 노출. state transition
        # 기록은 진짜 state(_init_state) 생성 후에 한다(placeholder는 곧 교체되므로). 거버넌스
        # 게이트는 여기서 안 건드린다(운영 knob 가시화만).
        if auto_config_note:
            emit(auto_config_note)
        emit("합성 중…")
        hb("합성")
        critique: SpecCritique | None = None
        try:
            if critic_client is not None:
                # opt-in 적대적 critic: 비평 surface + 구체 gap이면 바운드 1회 재합성.
                # synth_context(이어가기 ②a)는 *합성기에만* 주입 — critique_spec엔 안 감(적대 분리).
                spec, critique = synthesize_with_critique(
                    order, m_client, m_critic,
                    context=synth_context,
                    syn_prompt_path=syn_prompt, critic_prompt_path=critic_prompt,
                )
            else:
                spec = synthesize(order, m_client, context=synth_context, prompt_path=syn_prompt)
        except SynthesisError as e:
            state = _escalated_no_spec(
                "spec 합성 실패 (synthesize 출력 검증 불통과)", e.raw_response
            )
            record_transition(STAGE_SYNTHESIZE)
            # 합성 실패라도 거기까지 든 비용은 정직하게 누적(state 생성 후 — budget 존재).
            account(tag_cost(combine_costs(m_client.drain()), kind="synth"))
            if m_critic is not None:
                account(tag_cost(combine_costs(m_critic.drain()), kind="critic"))
            emit(_final_label(state))
            try_save()
            return state

        # WO#51: 통합 유닛 deps 추론 넛지 — 통합 성격 유닛(대시보드·진입점·e2e·트레이스)이
        # 자기가 엮는 빌더 유닛에 의존하도록 deps를 교정한다. 과소 지정 시에만 #31 재합성
        # 피드백 채널로 *바운드 1회* 보정 → DAG 말단 직렬화 → 통합 시점 머지 충돌 근본 차단.
        # deps만 바꾸고 criteria/done_when 불변. best-effort(실패→원본 진행). 비용은 아래 drain.
        hb("합성")
        spec = nudge_integration_deps(
            order, spec, m_client, context=synth_context, prompt_path=syn_prompt
        )

        # WO#59: disjoint-scope 넛지(#51의 형제) — 병렬 형제 유닛이 *같은 파일 scope*를 공유하면
        # bounded 1회 재합성으로 disjoint하게 재배치(머지 충돌 선제 예방). bar 불변 가드로 채택
        # (criteria 변경이면 reject·원본 유지). 겹침/미선언 없으면 no-op(추가 호출 0). advisory.
        hb("합성")
        spec = nudge_disjoint_scope(
            order, spec, m_client, context=synth_context, prompt_path=syn_prompt
        )

        state = _init_state(spec)
        save_spec(spec)  # WO#58: 검증된 spec을 spec.yaml 사이드카로(이어가기·대시보드 보강 원천)
        # WO#65: auto 해석 config를 진짜 state에 transition으로 기록(투명성 — 대시보드/감사).
        if auto_config_note:
            record_transition(STAGE_AUTO_CONFIG)
        record_transition(STAGE_SYNTHESIZE)
        # 합성/critic/통합-deps 넛지(전역 단계) 비용을 budget에 누적(특정 유닛 event 아님).
        account(tag_cost(combine_costs(m_client.drain()), kind="synth"))
        if m_critic is not None:
            account(tag_cost(combine_costs(m_critic.drain()), kind="critic"))
        if critique is not None:
            state.spec_critique = critique  # 감사 기록(재합성 발생 여부 포함)
            emit(_critique_label(critique))
            try_save()

        # ── 능력 획득 거버넌스(WO#53 F.1, opt-in) — dispatch 전 pre-flight ──
        # 플래그 OFF(기본)면 *완전 no-op*(기존 동작 불변). ON이고 spec이 능력을 요청하면:
        #   발견(큐레이션 후보) → POC(증거) → 승인(allowlist)된 건 provenance 기록 후 진행,
        #   미승인은 *escalate*(후보+증거+provenance, 사람 검토 대기 → 승인 후 재실행 시 채택).
        # **자동 채택 없음**·executor sandbox 무변경·큐레이션 소스만·best-effort(절대 raise 안 함).
        if capabilities_on:
            requests = list(spec.capability_requests or [])
            state.capability_requests = requests
            if requests:
                outcome = governed_capability_preflight(
                    requests,
                    registry_dir=capability_registry_path,
                    allowlist=capability_allowlist,
                    approved_at=now() or "",
                    poc_runner=capability_poc_runner,
                    searcher=capability_searcher,  # F.2: opt-in 인터넷 발견(off면 None=큐레이션-only)
                )
                # 승인되어 채택된 능력의 provenance를 감사 기록(미승인은 없음 — 자동 채택 X).
                state.capability_provenance.extend(outcome.provenance)
                if outcome.escalation is not None:
                    # 미승인 능력 → run 멈춤(사람 승인 대기). 기존 escalate→사람→재run 경로 재사용.
                    state.pending_escalations.append(outcome.escalation)
                    state.status = Status.escalated
                    emit(_final_label(state))
                    try_save()
                    return state

        # ── ②b 깊은 증분(WO#71): 검증된 부모 유닛 명시적 재사용 — done 시드로 재빌드 생략 ──
        # continue-from에서만(reuse_manifest 있을 때). 합성기가 단 `reuse_of` 라벨을 부모
        # done-manifest와 acceptance_criteria·scope로 *대조*해(라벨+가드 이중) 불변일 때만 done으로
        # 시드한다 → 스케줄러가 자연히 skip(delta DAG). 바 변경/부모 미검증이면 재사용 거부·정상
        # 빌드+gate(anti-erosion — 부모 통과로 도장 금지). 통합 gate는 재사용 유닛도 머지된 main
        # 통합 스코프에 포함돼 최종 결과에 항상 실행된다(개별 재사용≠통합 생략). 결정은 이벤트+transition.
        reuse_events: list[Event] = []
        for d in evaluate_reuse(spec, reuse_manifest, reuse_on=reuse):
            record_transition(STAGE_REUSE if d.reused else STAGE_REBUILD, d.unit)
            emit(("재사용: " if d.reused else "재빌드: ") + f"{d.unit} — {d.reason}")
            if d.reused:
                _set_plan_state(state, d.unit, PlanState.done)
                reuse_events.append(Event(
                    seq=0, unit=d.unit, work_order_ref=f"reuse_of={d.parent}",
                    result=d.reason, verdict=Verdict.pass_, learnings=d.reason,
                    ts=now(), stage=STAGE_REUSE,
                ))
        if reuse_events or reuse_manifest:
            try_save()  # 증분: 재사용/재빌드 결정을 즉시 감사 로그에 보존

        # 선제 스캐폴드(WO#27): executor *dispatch 전에* host가 진짜 스택을 깐다.
        # scaffold_client 없으면 None → 모든 신규 경로 no-op(기존 동작 불변, critic 패턴과 동형).
        # generate_scaffold는 best-effort라 dep 스택 불필요/실패면 None을 돌려준다.
        scaffold: Scaffold | None = None
        if scaffold_client is not None:
            emit("scaffold 생성 중…")
            record_transition(STAGE_SCAFFOLD)
            hb("scaffold")
            scaffold = generate_scaffold(spec, m_scaffold, prompt_path=scaffold_prompt)
            # scaffold(전역 단계) 비용 누적 + kind 태그(분해 ledger에 'scaffold'로 보임).
            account(tag_cost(combine_costs(m_scaffold.drain()), kind="scaffold"))
            emit(
                f"scaffold: {len(scaffold.files)}개 파일 (install={scaffold.install})"
                if scaffold is not None
                else "scaffold: 불필요 — 스킵(no-op)"
            )

        # 병렬 모드: worktree 격리 + 결정적 DAG 스케줄러로 분기.
        # max_parallel<=1은 아래 순차 경로 그대로(현행 동작 불변 — 무회귀).
        if max_parallel > 1:
            return _parallel_loop(
                spec,
                state,
                m_client,
                integration_gate=gate,
                executor_factory=executor_factory or (lambda wt: executor),
                gate_factory=gate_factory or (lambda wt: gate),
                wm=worktree_manager or WorktreeManager(workdir or "."),
                max_parallel=max_parallel,
                max_iters=max_iters,
                replan_retries=replan_retries,
                unit_retries=unit_retries,
                tier_ladder=tier_ladder,
                rep_prompt=rep_prompt,
                emit=emit,
                try_save=try_save,
                scaffold=scaffold,
                install_deps=install_deps,
                deps_runner=deps_runner,
                seeded=seeded,
                apply_skills=apply_skills,
                pricing=pricing,
                now=now,
                account=account,
                record_transition=record_transition,
                activity_start=activity_start,
                activity_end=activity_end,
                heartbeat=heartbeat,
                decomp_retries=decomp_retries,
                run_decomp_critic=run_decomp_critic,
                account_decomp_cost=(
                    (lambda: account(tag_cost(combine_costs(m_critic.drain()), kind="critic")))
                    if m_critic is not None
                    else (lambda: None)
                ),
                or_alternatives=or_alternatives,
                max_tokens=max_tokens,
                unit_attempt_budget=unit_attempt_budget,
                unit_token_budget=unit_token_budget,
                reuse_events=reuse_events,  # WO#71: 재사용 시드 이벤트(materialize가 포함)
            )

        # WO#71: 순차 경로는 materialize가 없어 재사용 이벤트를 여기서 직접 events에 싣는다.
        for ev in reuse_events:
            ev.seq = len(state.events) + 1
            state.events.append(ev)

        # 순차(N=1): worktree 없음 → workdir에 직접 scaffold 쓰기 + host-install(커밋 불필요).
        # scaffold=None이면 no-op → 기존 순차 경로 불변.
        if scaffold is not None:
            wd = Path(workdir or ".")
            written = write_scaffold(scaffold, wd)
            emit(f"scaffold 적용: {len(written)}개 파일 (workdir, executor 전)")
            if scaffold.install and install_deps:
                res = ensure_deps(wd, runner=deps_runner)
                emit(f"scaffold host-install: {res.manager} ok={res.ok}")
        elif seeded and install_deps:
            # 이어가기(②a): scaffold는 스킵하되 *시딩된 package.json*에 deps는 host-install
            # (node_modules는 시딩 안 됨). #23 패턴 — sandbox 불변, non-fatal.
            res = ensure_deps(Path(workdir or "."), runner=deps_runner)
            emit(f"이어가기 host-install(시딩된 deps): {res.manager} ok={res.ok}")

        last_result = "(시작 — 아직 실행 없음)"
        # WO#25 Part A: 직전에 dispatch한 작업 유닛. brain이 다른 유닛으로 넘어가면(advance)
        # 이 유닛을 done으로 수용하고, escalate면 이 유닛을 failed로 표시한다.
        worked_unit: str | None = None

        iters = 0
        while iters < max_iters and state.status == Status.running:
            iters += 1

            # WO#68 (B): 전역 예산 cap — 다음(replan/빌드) 호출 *전*에 누적 토큰이 --max-tokens
            #   초과면 clean stop(외부 codex 컷오프 전에 의도적). 미설정=무제한(무회귀).
            #   anti-erosion 무관 — 바 불변, 그냥 돈을 그만 쓴다. #58로 재개 가능.
            if over_global_budget():
                state.status = Status.stopped_budget
                state.pending_escalations.append({
                    "reason": (
                        f"예산 초과 — 누적 토큰 {state.budget.spent.tokens} > "
                        f"--max-tokens {max_tokens}. 충전/상한 조정 후 --continue-from으로 재개"
                    ),
                })
                break

            # replan(비결정 LLM 출력 → 검증 실패 흡수) + 분해 critic(WO#40).
            # 바깥 루프 = 분해 critic 재계획(bounded decomp_retries), 안쪽 = replan 파싱 재시도.
            # weak 판정 → 피드백 주고 재replan; 재시도 소진 후에도 weak면 *진행*(데드락 금지)+기록.
            record_transition(STAGE_REPLAN)
            hb("replan")
            decision = None
            last_err: ReplanError | None = None
            decomp_feedback: str | None = None  # weak 판정 시 다음 replan에 얹는 피드백
            for _decomp_attempt in range(decomp_retries + 1):
                decision = None
                feedback: str | None = decomp_feedback
                for _attempt in range(replan_retries + 1):
                    if _attempt == 0:
                        emit(
                            "replan 중…" if decomp_feedback is None
                            else f"replan(분해 재계획): {_truncate(decomp_feedback)}"
                        )
                    else:
                        emit(
                            f"replan 재시도 {_attempt}: "
                            f"{_truncate(feedback or 'Decision 검증 실패')}"
                        )
                    try:
                        decision = replan(
                            spec, state, last_result, m_client,
                            prompt_path=rep_prompt, feedback=feedback,
                        )
                        break
                    except ReplanError as e:
                        last_err = e
                        feedback = e.message  # raw는 빼고 검증 메시지만 다시 태운다
                # 파싱 소진 / 분해 critic 비대상 action(next_order/retry 아님) → 그대로 채택.
                if decision is None or decision.action not in (Action.next_order, Action.retry):
                    break
                no_candidate = decision.next_order
                if no_candidate is None:
                    break  # 본문 없음 → 아래 action 처리에서 방어 escalate
                crit = run_decomp_critic(no_candidate, last_result)  # 독립 critic, 스킬 미주입 원본
                if crit is None or not is_weak(crit):
                    break  # progress(또는 OFF/평가불가) → 이 분해 채택
                # weak: 재시도 남았으면 reject→재replan, 소진이면 진행(데드락 금지).
                if _decomp_attempt < decomp_retries:
                    crit.rejected = True
                    state.decomp_critiques.append(crit)
                    record_transition(STAGE_DECOMP_REJECT, no_candidate.unit)
                    emit(
                        f"분해 critic: weak → reject·재계획 ({no_candidate.unit}): "
                        f"{_truncate(crit.reason or '')}"
                    )
                    decomp_feedback = build_decomp_feedback(crit)
                    try_save()  # 증분: reject 판정도 즉시 감사 로그에 보존
                else:
                    state.decomp_critiques.append(crit)  # 소진 — rejected=False(진행함)
                    emit(
                        f"분해 critic: weak이나 재시도 소진 → 진행 ({no_candidate.unit}): "
                        f"{_truncate(crit.reason or '')}"
                    )
            # 이 iteration의 replan(재계획 재시도 포함) orchestration 비용을 꺼낸다.
            # kind="replan" 태그(unit은 next_order 채택 시 채움 — fill-if-None).
            replan_cost = tag_cost(combine_costs(m_client.drain()), kind="replan")
            # 분해 critic(verifier-side) 비용도 정직하게 누적(코스트 패널에 보임).
            if m_critic is not None:
                account(tag_cost(combine_costs(m_critic.drain()), kind="critic"))
            if decision is None:
                account(replan_cost)  # 실패한 replan도 비용은 정직하게 누적
                state.status = Status.escalated
                state.pending_escalations.append(
                    {
                        "reason": f"replan 출력 {replan_retries + 1}회 검증 실패",
                        "raw_response": last_err.raw_response if last_err else None,
                    }
                )
                break

            action = decision.action

            if action in (Action.next_order, Action.retry):
                no = decision.next_order
                if no is None:
                    # 방어: next_order/retry인데 본문이 없음 → 사람 tier로 올림
                    state.status = Status.escalated
                    state.pending_escalations.append(
                        {"reason": "next_order 본문 없음", "action": action.value}
                    )
                    break
                # WO#25 Part A: brain이 다른 유닛으로 넘어가면 직전 작업 유닛을 수용(done).
                # dispatch하는 유닛은 in_progress. (gate/replan/종료 로직은 불변 — plan state만.)
                if worked_unit is not None and worked_unit != no.unit:
                    _advance_done(state, worked_unit)
                _set_plan_state(state, no.unit, PlanState.in_progress)
                worked_unit = no.unit
                # WO#33: dispatch=build 단계 진입 → 라이브 activity + 전이 이력.
                activity_start(no.unit, STAGE_BUILD)
                record_transition(STAGE_BUILD, no.unit)
                emit(f"작업 실행 중: {no.unit} — {_truncate(no.goal)}")
                try_save()  # 증분: in-flight activity가 대시보드 폴링에 보이도록
                hb("빌드", no.unit)  # WO#55: 빌드 codex 호출 라이브 표면화
                result = executor.run(apply_skills(no))  # 스킬 주입은 executor에만(빌더 전용)
                exec_cost = _executor_cost(executor, pricing)  # 읽기만(best-effort)
                # WO#70: 빌더 비용 태그 — retry면 kind=retry, 아니면 build. 순차 경로는 단일
                # tier라 tier=None(날조 금지). unit 귀속.
                tag_cost(
                    exec_cost,
                    kind=("retry" if action == Action.retry else "build"),
                    unit=no.unit,
                )
                # WO#33: gate 진입=verify 단계로 갱신.
                activity_set_stage(no.unit, STAGE_VERIFY)
                record_transition(STAGE_VERIFY, no.unit)
                emit("gate 검사 중…")
                hb("judge", no.unit)  # WO#55: judge codex 호출 라이브 표면화
                gr = gate.judge(result, spec)
                verdict = gr.verdict
                emit(_summarize_gate(gr))
                activity_end(no.unit)  # 완료 → 라이브 activity에서 제거
                # 이 유닛 처리 비용 = replan(orchestration) + executor + judge(gate) 귀속.
                tag_cost(replan_cost, unit=no.unit)  # replan에 유닛 채움(kind은 이미 replan)
                tag_cost(gr.judge_cost, kind="judge", unit=no.unit)
                event_cost = combine_costs([replan_cost, exec_cost, gr.judge_cost])
                account(event_cost)
                state.events.append(
                    Event(
                        seq=len(state.events) + 1,
                        unit=no.unit,
                        work_order_ref=no.goal,
                        result=result,
                        verdict=verdict,
                        checks=gr.checks,
                        cost=event_cost,
                        ts=now(),
                        stage=STAGE_BUILD,
                    )
                )
                try_save()  # 증분: 매 이벤트마다 감사 로그 보존(비치명적)
                last_result = f"unit={no.unit} verdict={verdict.value} :: {result}"
                if verdict == Verdict.done:
                    state.status = Status.done

            elif action == Action.stop:
                account(replan_cost)  # event 없는 종료 — replan 비용은 budget에만
                record_transition(STAGE_DONE)
                state.status = Status.done

            elif action == Action.escalate:
                account(replan_cost)
                record_transition(STAGE_ESCALATE, worked_unit)
                state.status = Status.escalated
                # WO#25 Part A: brain이 작업 중이던 유닛을 포기(escalate) → 그 유닛 failed.
                if worked_unit is not None:
                    _set_plan_state(state, worked_unit, PlanState.failed)
                if decision.escalation is not None:
                    state.pending_escalations.append(
                        decision.escalation.model_dump(by_alias=True, mode="json")
                    )

            elif action == Action.replan_approach:
                account(replan_cost)
                # executor 호출 없이 다음 루프 — 다음 replan이 계획을 다시 짠다.
                last_result = "(approach reset — 이전 접근 폐기, 재계획 요청됨)"

            elif action == Action.propose_spec_change:
                # governed 적용(mutability gradient): assumptions+evidence는 자율 적용,
                # 성공 정의(goal/criteria/done_when)·anchor(order_raw)는 자율 변경 불가.
                proposal = decision.spec_change
                if proposal is None:
                    state.status = Status.escalated
                    state.pending_escalations.append(
                        {"reason": "propose_spec_change인데 spec_change 본문 없음"}
                    )
                    break
                outcome = apply_spec_change(spec, state, proposal)
                if outcome.applied:
                    account(replan_cost)
                    # 감사 이벤트만 남기고 루프 계속 — 다음 replan이 갱신된 spec을 본다.
                    state.events.append(
                        Event(
                            seq=len(state.events) + 1,
                            verdict=decision.verdict,
                            result=f"spec-change applied: {outcome.reason}",
                            learnings=outcome.reason,
                            cost=replan_cost,
                            ts=now(),
                            stage=STAGE_REPLAN,
                        )
                    )
                    emit(f"spec 변경 적용: {proposal.target} (v{spec.version})")
                    try_save()  # 증분: spec 변경도 즉시 보존(비치명적)
                    save_spec(spec)  # WO#58: 갱신된 spec도 사이드카에 반영(라운드트립 최신 유지)
                    last_result = f"(spec-change applied — {outcome.reason})"
                else:
                    account(replan_cost)
                    state.status = Status.escalated
                    state.pending_escalations.append(outcome.note)
                    emit(f"spec 변경 escalate: {_truncate(outcome.reason)}")

            else:  # 방어: 미지원 action
                account(replan_cost)
                state.status = Status.escalated
                state.pending_escalations.append({"reason": f"미지원 action: {action.value}"})

        # WO#25 Part A: 종료가 done이면 작업된(=pending 아닌) 모든 유닛을 done으로.
        # done = 전체 spec 통과 = 모든 작업 유닛 반영됨 → "전부 in_progress 고착" 해소.
        if state.status == Status.done:
            for item in state.plan:
                if item.state != PlanState.pending:
                    item.state = PlanState.done

        # max_iters 도달 등으로 여전히 running이면 임시로 stopped_stuck.
        if state.status == Status.running:
            state.status = Status.stopped_stuck

        emit(_final_label(state))
        try_save()

        return state
    except CodexUsageLimitError as e:
        # WO#68 (A): codex 사용량/크레딧 소진(알려진 외부 조건) — 어떤 codex 경로(합성·replan·
        #   빌드·critic·judge)에서 와도 traceback 크래시 대신 *graceful stop*. 완료 유닛은 보존,
        #   상태를 stopped_credit로 봉인(명확 사유) → 충전 후 #58 --continue-from으로 재개.
        #   (#43/#54 패턴 재사용 — best-effort, 기록/저장 중 추가 예외도 흡수해 2차 크래시 금지.)
        try:
            record_transition(STAGE_ESCALATE)
        except Exception:  # noqa: BLE001
            pass
        try:
            if state is not None and state.status == Status.running:
                state.status = Status.stopped_credit
                state.pending_escalations.append({
                    "reason": "codex 크레딧 소진 — 충전 후 `--continue-from`으로 재개(완료 유닛 보존)",
                    "detail": str(e),
                })
        except Exception:  # noqa: BLE001
            pass
        try:
            emit(_final_label(state))
        except Exception:  # noqa: BLE001
            pass
        try_save()
        return state
    except CodexStalled as e:
        # WO#54: *필수* codex 호출(합성·replan·빌드)이 bounded 재시도 후에도 무진행(멈춤).
        #   가짜 진행 금지 — 정직하게 사람 tier로 escalate한다(무한 hang 대신 클린 종료).
        #   (병렬 경로의 worktree 정리는 _parallel_loop의 finally가 보장한다.)
        #   best-effort — 기록/저장 중 추가 예외도 흡수(2차 크래시 금지).
        try:
            record_transition(STAGE_ESCALATE)
        except Exception:  # noqa: BLE001
            pass
        try:
            if state is not None and state.status == Status.running:
                state.status = Status.escalated
                state.pending_escalations.append({
                    "reason": "codex 무진행(idle) — bounded 재시도 후에도 멈춤(stalled), escalate",
                    "detail": str(e),
                })
        except Exception:  # noqa: BLE001
            pass
        try:
            emit(_final_label(state))
        except Exception:  # noqa: BLE001
            pass
        try_save()
        return state
    except KeyboardInterrupt:
        # 웹 stop/SIGINT: 순차 경로는 worktree 미사용이라 state 저장만으로 충분.
        #   (병렬 경로의 worktree 정리는 _parallel_loop의 finally가 보장한다.)
        #   best-effort — 정리/저장 중 추가 예외도 흡수(2차 크래시 금지).
        _finalize_interrupt(state, emit, try_save)
        return state


# ──────────────────────────── 병렬 루프 (WO#21) ────────────────────────────


def _parallel_loop(
    spec: ProjectSpec,
    state: State,
    client: LLMClient,
    *,
    integration_gate: Gate,
    executor_factory: ExecutorFactory,
    gate_factory: GateFactory,
    wm: WorktreeManager,
    max_parallel: int,
    max_iters: int,
    replan_retries: int,
    unit_retries: int,
    rep_prompt: str | Path,
    tier_ladder: list[Tier] | None = None,
    emit: Callable[[str], None],
    try_save: Callable[[], None],
    scaffold: Scaffold | None = None,
    install_deps: bool = True,
    deps_runner: DepsRunner | None = None,
    seeded: bool = False,
    apply_skills: Callable[[NextOrder], NextOrder] = lambda o: o,
    pricing: dict | None = None,
    now: Callable[[], str | None] = lambda: None,
    account: Callable[[Cost | None], None] = lambda c: None,
    record_transition: Callable[[str, str | None], None] = lambda s, u=None: None,
    activity_start: Callable[[str | None, str], None] = lambda u, s: None,
    activity_end: Callable[[str | None], None] = lambda u: None,
    heartbeat=None,
    decomp_retries: int = 1,
    run_decomp_critic: Callable[[NextOrder, str], "DecompCritique | None"] = lambda no, last: None,
    account_decomp_cost: Callable[[], None] = lambda: None,
    or_alternatives: int = 1,
    max_tokens: int | None = None,
    unit_attempt_budget: int | None = None,
    unit_token_budget: int | None = None,
    reuse_events: list[Event] | None = None,
) -> State:
    """결정적 DAG 스케줄러 + git worktree 격리로 ready unit들을 동시에 굴린다.

    설계 요점:
      - 결정성: ready set은 unit-id 정렬. brain(work order 생성)·worktree 생성·머지는
        **main 스레드에서 직렬** 처리 → mock 시퀀스 race 없음, 선형 DAG=순차 동일.
      - 병렬: 느린 executor 실행 + unit gate만 ThreadPoolExecutor로 동시 실행.
      - 머지 직렬화: 완료된 future를 main 스레드가 (unit-id 순으로) 하나씩 처리하며
        머지하므로 자연히 직렬. 충돌이면 unit을 pending으로 되돌려 갱신된 main 위에
        재dispatch(바운드). 소진 시 escalate.
      - 뒷정리: try/finally로 모든 종료 경로(done/escalate/예외)에서 cleanup_all().
      - 이벤트: (unit-id, attempt)로 정렬해 완료 타이밍 비결정성을 봉인.
    """
    def hb(call_kind: str, unit: str | None = None) -> None:
        """WO#55: codex 호출 직전 *이 스레드*에 라이브 하트비트 컨텍스트를 깐다(best-effort)."""
        if heartbeat is None:
            return
        try:
            heartbeat.set_context(call_kind, unit)
        except Exception:  # noqa: BLE001
            pass

    # ── 상태 봉투(closure로 공유) ──────────────────────────────────────────
    terminal: str | None = None  # None | "done"(brain stop) | "escalated" | "stuck"
    last_result = "(시작 — 아직 실행 없음)"
    total_attempts = 0
    attempts_of: dict[str, int] = {u.unit: 0 for u in spec.decomposition}
    buf: list[dict] = []  # 결정적 정렬 전 이벤트 수집 버퍼
    # gen_order(main 스레드)가 유닛별 replan(orchestration) 비용을 여기 적립 → 머지 시 귀속.
    orch_cost_of: dict[str, Cost | None] = {}
    # ── OR-node(WO#41) 봉투 ──────────────────────────────────────────────
    # alt_count: 유닛별 시도한 대안 수(0=원본만). alt_feedback: 다음 gen_order에 줄
    # "다른 접근" 지시(bar 불변). last_approach: 유닛별 직전 접근 요약(반복 회피).
    alt_count: dict[str, int] = {u.unit: 0 for u in spec.decomposition}
    alt_feedback: dict[str, str] = {}
    last_approach: dict[str, str] = {}
    integration_alt = 0  # 통합 gate OR 대안 시도 수(bounded by or_alternatives)
    # WO#48: 유닛별 머지 충돌 → *통합 적응 재빌드* 시도 이력(정직한 escalation 첨부용).
    # 각 항목 {attempt, conflict_files, merged_siblings} — 재빌드 경로가 무엇을 시도했는지.
    integ_adapt: dict[str, list[dict]] = {}
    # ── 반응형 tier 사다리(WO#64) 봉투 ─────────────────────────────────────
    # ladder: 사다리(미지정/빈 리스트면 단일 tier 1칸 = 후방호환). escalation_of: 유닛별
    # *지금까지 한 escalation 수*(gate 실패/머지 충돌/OR 대안 재dispatch마다 ++, 단조 비감소).
    # start_base_of: 유닛별 시작 인덱스(start_tier 힌트). 시도 tier = ladder[min(base+esc, top)].
    ladder: list[Tier] = list(tier_ladder) if tier_ladder else [Tier()]
    tier_top = len(ladder) - 1
    escalation_of: dict[str, int] = {u.unit: 0 for u in spec.decomposition}
    start_base_of: dict[str, int] = {
        u.unit: resolve_start_tier(getattr(u, "start_tier", "") or "", ladder)
        for u in spec.decomposition
    }

    def tier_for(unit: str) -> Tier:
        """이 유닛의 *이번 시도* tier — base(시작 힌트) + escalation, top에서 cap(bounded)."""
        idx = min(start_base_of.get(unit, 0) + escalation_of.get(unit, 0), tier_top)
        return ladder[idx]

    # WO#70: 유닛별 *이번 dispatch* tier 라벨(비용 태그용). dispatch 시 기록, 머지/판정 시 읽음.
    # 다중 tier(tier_top>0)일 때만 라벨(단일이면 None — 날조 금지). 한 유닛 동시 1시도라 안전.
    tier_label_of: dict[str, str | None] = {}

    def cost_kind_for(unit: str) -> str:
        """이 시도의 빌더 비용 kind — 어느 *재시도층*에서 났나(#68/#41/#48 stacking 가시화).

        통합 OR 재빌드 중이면 integration-OR, OR 대안 접근이면 OR, gate-실패 재시도면 retry,
        최초면 build. (카운터는 handle_outcome가 ++하기 *전*에 읽으므로 방금 돈 시도를 가리킨다.)
        """
        if integration_alt > 0:
            return "integration-OR"
        if alt_count.get(unit, 0) > 0:
            return "OR"
        if attempts_of.get(unit, 0) > 0:
            return "retry"
        return "build"

    # ── WO#68 비용 거버넌스 봉투 ───────────────────────────────────────────
    # (C) 유닛별 *누적* 토큰(재시도+OR+통합OR 층 합산) — 누적 ceiling 판정에 사용.
    #   escalation_of[unit]가 이미 *층 합산 재dispatch 수*(gate실패/충돌/OR마다 ++, 안 내림)라
    #   그걸 누적 시도 수로 그대로 읽는다(별도 카운터 불필요 — 단조 비감소 보장됨).
    unit_tokens_of: dict[str, int] = {u.unit: 0 for u in spec.decomposition}

    def add_unit_tokens(unit: str, cost: Cost | None) -> None:
        try:
            t = getattr(cost, "tokens", None)
            if t:
                unit_tokens_of[unit] = unit_tokens_of.get(unit, 0) + t
        except Exception:  # noqa: BLE001
            pass

    def over_global_budget() -> bool:
        """(B) 전역 토큰 cap 초과? 미설정=무제한(무회귀). best-effort."""
        if max_tokens is None:
            return False
        try:
            return (state.budget.spent.tokens or 0) > max_tokens
        except Exception:  # noqa: BLE001
            return False

    def unit_ceiling_hit(unit: str) -> bool:
        """(C) 이 유닛이 누적 수렴 ceiling(시도 수/토큰)을 넘었나? 미설정=off(무회귀).

        escalation_of=층 합산 재dispatch 수. 더 던지기 전에 사람에게 넘기는 신호 — 바는 안 낮춘다.
        """
        if unit_attempt_budget is not None and escalation_of.get(unit, 0) >= unit_attempt_budget:
            return True
        if unit_token_budget is not None and unit_tokens_of.get(unit, 0) >= unit_token_budget:
            return True
        return False

    def escalate_unit_unconverged(
        unit: str, order: NextOrder, result: str, gr: GateResult, event_cost: Cost | None
    ) -> None:
        """(C) 안 수렴한 유닛을 *사람에게* escalate(다음 OR로 안 던짐). 바 자동 미완화(anti-erosion).

        criteria/done_when 불변 — 사유에 유닛·누적 시도/토큰을 명시하고 사람이 governed로 결정
        (바 조정/수용/대안). 폐기 시도 비용은 정직하게 누적, 유닛은 failed로 봉인(#58 재개 시 복귀).
        """
        nonlocal terminal
        account(event_cost)
        record(unit, order.goal, result, gr.verdict, gr.checks, cost=event_cost, ts=now())
        _set_plan_state(state, unit, PlanState.failed)
        terminal = "escalated"
        state.pending_escalations.append({
            "reason": (
                f"unit {unit} — {escalation_of.get(unit, 0)} 시도(층 합산)·"
                f"{unit_tokens_of.get(unit, 0)} 토큰 후 미수렴. 사람 결정 필요: "
                f"바 조정(governed)/수용/대안 (자동 완화 없음)"
            ),
            "unit": unit,
            "attempts": escalation_of.get(unit, 0),
            "tokens": unit_tokens_of.get(unit, 0),
        })
        persist()

    def record(unit: str, goal: str | None, result: str, verdict: Verdict,
               checks: list[CheckReport], cost: Cost | None = None,
               ts: str | None = None) -> None:
        buf.append({
            "unit": unit, "attempt": attempts_of.get(unit, 0), "goal": goal,
            "result": result, "verdict": verdict, "checks": list(checks),
            "cost": cost, "ts": ts,
        })

    def materialize(integration_ev: Event | None = None) -> None:
        ordered = sorted(buf, key=lambda e: (e["unit"], e["attempt"]))
        # WO#71: 재사용 시드 이벤트(시드 시점·spec 순서로 결정적)를 맨 앞에 둔다.
        evs: list[Event] = list(reuse_events or [])
        for e in ordered:
            evs.append(Event(
                seq=0, unit=e["unit"], work_order_ref=e["goal"],
                result=e["result"], verdict=e["verdict"], checks=e["checks"],
                cost=e.get("cost"), ts=e.get("ts"), stage=STAGE_BUILD,
            ))
        if integration_ev is not None:
            evs.append(integration_ev)
        for i, ev in enumerate(evs, start=1):  # 전체 재번호(재사용+빌드+통합) — 결정적 seq
            ev.seq = i
        state.events = evs

    def persist() -> None:
        materialize()
        try_save()

    def gen_order(unit: str) -> NextOrder | None:
        """brain(replan/Decision 머신 재사용)으로 이 unit의 work order를 만든다.

        스케줄러가 unit을 권위적으로 정했으므로 결과 next_order.unit을 unit으로 고정.
        escalate/stop/replan-소진은 terminal을 세팅하고 None을 반환(이 unit 미dispatch).
        """
        nonlocal terminal, last_result
        record_transition(STAGE_REPLAN, unit)
        hb("replan", unit)  # WO#55: replan codex 호출 라이브(director-side도 활성 표기)
        ctx = (
            f"스케줄러가 unit '{unit}'를 ready로 선택했다(deps 충족). "
            f"이 unit의 work order만 생성하라(action=next_order, unit={unit}).\n"
            f"# 직전 진행\n{last_result}"
        )
        # replan(파싱 재시도) + 분해 critic(WO#40, bounded decomp_retries). weak → 재계획,
        # 소진 후에도 weak면 진행(데드락 금지)+기록. critic은 독립 client·스킬 미주입 원본.
        decision = None
        last_err: ReplanError | None = None
        # OR 대안(WO#41): 이 유닛에 대기 중인 "다른 접근" 지시가 있으면 첫 replan에 seed.
        decomp_feedback: str | None = alt_feedback.pop(unit, None)
        for _decomp_attempt in range(decomp_retries + 1):
            decision = None
            feedback: str | None = decomp_feedback
            for attempt in range(replan_retries + 1):
                if attempt == 0:
                    emit("replan 중…" if decomp_feedback is None
                         else f"replan(대안/재계획): {_truncate(decomp_feedback)}")
                else:
                    emit(f"replan 재시도 {attempt}: {_truncate(feedback or 'Decision 검증 실패')}")
                try:
                    decision = replan(spec, state, ctx, client,
                                      prompt_path=rep_prompt, feedback=feedback)
                    break
                except ReplanError as e:
                    last_err = e
                    feedback = e.message
            if decision is None or decision.action not in (Action.next_order, Action.retry):
                break
            no_candidate = decision.next_order
            if no_candidate is None:
                break
            no_candidate.unit = unit  # 스케줄러 권위 — critic도 올바른 unit으로 본다
            crit = run_decomp_critic(no_candidate, last_result)  # 독립 critic, 스킬 미주입
            if crit is None or not is_weak(crit):
                break  # progress(또는 OFF/평가불가) → 채택
            if _decomp_attempt < decomp_retries:
                crit.rejected = True
                state.decomp_critiques.append(crit)
                record_transition(STAGE_DECOMP_REJECT, unit)
                emit(f"분해 critic: weak → reject·재계획 ({unit}): {_truncate(crit.reason or '')}")
                decomp_feedback = build_decomp_feedback(crit)
            else:
                state.decomp_critiques.append(crit)  # 소진 — rejected=False(진행)
                emit(f"분해 critic: weak이나 재시도 소진 → 진행 ({unit}): {_truncate(crit.reason or '')}")
        # 이 unit replan(재계획 재시도 포함) orchestration 비용 적립(머지 시 event에 귀속).
        # WO#70: kind="replan"·unit 태그 → 분해 ledger에서 orchestration/replan/유닛으로 보임.
        orch_cost_of[unit] = tag_cost(combine_costs(client.drain()), kind="replan", unit=unit)
        account_decomp_cost()  # 분해 critic(verifier-side) 비용 누적(코스트 패널에 보임)
        if decision is None:
            account(orch_cost_of.pop(unit, None))  # event 없음 → budget에만
            terminal = "escalated"
            state.pending_escalations.append({
                "reason": f"unit {unit} replan {replan_retries + 1}회 검증 실패",
                "raw_response": last_err.raw_response if last_err else None,
            })
            return None

        action = decision.action
        if action in (Action.next_order, Action.retry):
            no = decision.next_order
            if no is None:
                account(orch_cost_of.pop(unit, None))
                terminal = "escalated"
                state.pending_escalations.append(
                    {"reason": "next_order 본문 없음", "unit": unit})
                return None
            no.unit = unit  # 스케줄러 권위 — 어떤 unit인지는 스케줄러가 정한다
            last_approach[unit] = no.goal  # OR 접근 추적(다음 실패 시 폐기/대안 피드백에 사용)
            emit(f"작업 실행 중: {unit} — {_truncate(no.goal)}")
            return no
        if action == Action.stop:
            account(orch_cost_of.pop(unit, None))
            terminal = "done"
            return None
        if action == Action.escalate:
            account(orch_cost_of.pop(unit, None))
            terminal = "escalated"
            if decision.escalation is not None:
                state.pending_escalations.append(
                    decision.escalation.model_dump(by_alias=True, mode="json"))
            return None
        # parallel v1 바운드: 똑똑한 in-flight replan/spec-change 저글링은 안 한다
        account(orch_cost_of.pop(unit, None))
        terminal = "escalated"
        state.pending_escalations.append(
            {"reason": f"parallel v1 미지원 action: {action.value}", "unit": unit})
        return None

    def handle_outcome(unit: str, order: NextOrder, result: str, gr: GateResult,
                       exec_cost: Cost | None = None) -> None:
        """unit gate 결과를 처리: 성공→머지, 충돌/실패→바운드 재dispatch 또는 escalate."""
        nonlocal terminal, last_result
        verdict = gr.verdict
        # WO#33: gate 통과=verify 단계 종료 → 전이 이력 + 라이브 activity 제거.
        record_transition(STAGE_VERIFY, unit)
        activity_end(unit)
        # 이 시도 비용 = replan(orchestration) + executor + judge(per-unit gate).
        # judge_cost는 worker가 gate에서 받은 GateResult에 실려 main으로 돌아온다(스레드 안전).
        # WO#70: 빌더 비용에 kind(build/retry/OR/integration-OR)·tier(#64)·unit 태그, judge에
        # kind=judge·unit 태그(카운터 ++ 전이라 방금 돈 시도를 정확히 가리킴) → 분해 가능.
        tag_cost(exec_cost, kind=cost_kind_for(unit), tier=tier_label_of.get(unit), unit=unit)
        tag_cost(gr.judge_cost, kind="judge", unit=unit)
        event_cost = combine_costs([orch_cost_of.pop(unit, None), exec_cost, gr.judge_cost])
        add_unit_tokens(unit, event_cost)  # WO#68 (C): 유닛 누적 토큰(ceiling 판정용)
        emit(_summarize_gate(gr))

        if verdict in (Verdict.pass_, Verdict.done):
            outcome = wm.merge(unit)
            if outcome == "ok":
                account(event_cost)
                record(unit, order.goal, result, verdict, gr.checks,
                       cost=event_cost, ts=now())
                _set_plan_state(state, unit, PlanState.done)
                wm.cleanup(unit)
                last_result = f"unit={unit} verdict={verdict.value} merged"
                persist()
                return
            # 머지 충돌 → *통합 적응 재빌드*(WO#48): 충돌 유닛을 갱신된(머지된) main 위에서
            # 재분기(wm.create가 이미 main 기준 분기)하고, 작업지시서에 **통합 피드백**을 주입해
            # stale 재생성이 아니라 현재 통합 상태에 *적응*하게 한다 → 같은 충돌 재발 방지.
            # 충돌 파일(겹친 파일)은 abort 전에 wm가 캡처해둔다.
            conflict_files = list(getattr(wm, "last_conflict_files", []) or [])
            merged_siblings = [
                p.unit for p in state.plan
                if p.state == PlanState.done and p.unit != unit
            ]
            wm.discard(unit)
            # WO#68 (C): 누적 수렴 ceiling 초과면 더 재빌드 안 던지고 사람에게 escalate(바 불변).
            if unit_ceiling_hit(unit):
                escalate_unit_unconverged(unit, order, result, gr, event_cost)
                return
            if attempts_of[unit] < unit_retries:
                attempts_of[unit] += 1
                escalation_of[unit] += 1  # WO#64: 충돌 재빌드 = 한 tier 상향(다음 dispatch가 읽음)
                account(event_cost)  # 폐기된 시도도 비용은 정직하게 budget에 누적
                # 통합 피드백을 기존 feedback 채널(alt_feedback)로 주입 — replan 프롬프트 무변경.
                # gen_order가 다음 dispatch에서 pop해 replan feedback으로 태운다(#41과 동일 채널).
                alt_feedback[unit] = build_integration_feedback(
                    unit, conflict_files, merged_siblings)
                integ_adapt.setdefault(unit, []).append({
                    "attempt": attempts_of[unit],
                    "conflict_files": conflict_files,
                    "merged_siblings": merged_siblings,
                })
                emit(f"머지 충돌 → 통합 적응 재빌드(최신 main 재분기 + 통합 피드백): "
                     f"{unit} (재시도 {attempts_of[unit]})"
                     + (f" · 충돌 파일 {', '.join(conflict_files)}" if conflict_files else ""))
                _set_plan_state(state, unit, PlanState.pending)
            else:
                account(event_cost)
                record(unit, order.goal, result, Verdict.fail_replan, gr.checks,
                       cost=event_cost, ts=now())
                _set_plan_state(state, unit, PlanState.failed)
                terminal = "escalated"
                # 정직: 시도한 통합 적응 이력(재빌드 라운드별 충돌 파일/형제)을 첨부.
                state.pending_escalations.append(
                    {"reason": f"unit {unit} 머지 충돌 {unit_retries}회 통합 적응 재빌드 후 미해소 — escalate",
                     "unit": unit,
                     "integration_adaptations": integ_adapt.get(unit, [])})
                persist()
            return

        # gate 실패 → 바운드 *재시도*(같은 접근, 재dispatch). 재시도 소진 후엔 OR 대안.
        wm.discard(unit)  # 백트래킹: 실패 접근 worktree 정리(#21 보장 cleanup)
        # WO#68 (C): 누적 수렴 ceiling 초과면 재시도/OR로 더 던지지 않고 사람에게 escalate(바 불변).
        #   재시도+OR+통합OR 층 stacking이 무한히 돈을 빨아먹지 않게 *누적*에 천장을 둔다.
        if unit_ceiling_hit(unit):
            escalate_unit_unconverged(unit, order, result, gr, event_cost)
            return
        if attempts_of[unit] < unit_retries:
            attempts_of[unit] += 1
            escalation_of[unit] += 1  # WO#64: gate 실패 재시도 = 한 tier 상향(top에서 cap)
            account(event_cost)  # 폐기된 시도 비용도 누적
            emit(f"unit gate 실패({verdict.value}) → 재시도: {unit} ({attempts_of[unit]})")
            _set_plan_state(state, unit, PlanState.pending)
            return
        # 재시도 소진 → OR 대안(WO#41): 대안 남으면 *다른 접근*으로 갈아타고 백트래킹·재시도.
        account(event_cost)
        evidence = summarize_gate_evidence(gr)  # gate 실패 *증거*(bar 불변 — 기준 약화 아님)
        if alt_count[unit] < or_alternatives:
            # 폐기한 접근 기록 + "다른 접근" 피드백(criteria/done_when 불변) → pending 리셋.
            state.approaches.append(ApproachAttempt(
                scope=f"unit:{unit}", approach=last_approach.get(unit) or order.goal,
                outcome="abandoned", evidence=evidence, index=alt_count[unit]))
            alt_count[unit] += 1
            alt_feedback[unit] = build_alternative_feedback(
                last_approach.get(unit) or order.goal, evidence, scope="unit")
            attempts_of[unit] = 0  # 새 접근엔 재시도 카운터 리셋(worktree는 이미 discard됨)
            escalation_of[unit] += 1  # WO#64: 다른 접근도 재dispatch → tier 상향(단조, 안 내림)
            record_transition(STAGE_OR_ALTERNATIVE, unit)
            emit(f"OR 대안 접근: {unit} (대안 {alt_count[unit]}/{or_alternatives}) — bar 불변")
            _set_plan_state(state, unit, PlanState.pending)
            persist()
            return
        # 대안 소진(또는 OR OFF) → escalate. OR>0이면 시도 접근 첨부, 0이면 기존 동작(후방호환).
        record(unit, order.goal, result, verdict, gr.checks, cost=event_cost, ts=now())
        _set_plan_state(state, unit, PlanState.failed)
        terminal = "escalated"
        if or_alternatives > 0:
            state.approaches.append(ApproachAttempt(
                scope=f"unit:{unit}", approach=last_approach.get(unit) or order.goal,
                outcome="exhausted", evidence=evidence, index=alt_count[unit]))
            tried = [a.model_dump(mode="json") for a in state.approaches if a.scope == f"unit:{unit}"]
            state.pending_escalations.append(
                {"reason": f"unit {unit} gate {unit_retries}회 실패 + OR 대안 {or_alternatives} 소진 — escalate",
                 "unit": unit, "approaches_tried": tried})
        else:  # OR OFF → 기존 escalate(접근 추적 없음, 후방호환)
            state.pending_escalations.append(
                {"reason": f"unit {unit} gate {unit_retries}회 실패 — escalate", "unit": unit})
        persist()

    # ── 실행: 모든 경로에서 cleanup_all 보장(try/finally) ─────────────────
    try:
        try:
            wm.ensure_repo()
        except WorktreeError as e:
            state.status = Status.escalated
            state.pending_escalations.append({"reason": f"git repo 준비 실패: {e}"})
            materialize()
            emit(_final_label(state))
            try_save()
            return state

        # 선제 스캐폴드(WO#27): worktree 분기 *전에* main에 스택을 깐다 → worktree가 상속.
        # 순서: write(main) → ensure_deps(main; .gitignore+node_modules) → commit(scaffold+
        # .gitignore staged, node_modules 제외). scaffold=None이면 전부 no-op(기존 경로 불변).
        if scaffold is not None:
            written = write_scaffold(scaffold, wm.workdir)
            emit(f"scaffold 적용: {len(written)}개 파일 (main, worktree 분기 전)")
            if scaffold.install and install_deps:
                res = ensure_deps(wm.workdir, runner=deps_runner)
                emit(f"scaffold host-install(main): {res.manager} ok={res.ok}")
            commit_scaffold(wm.workdir)  # worktree가 분기 시 상속(node_modules는 gitignore 제외)
        elif seeded:
            # WO#71(②b): 이어가기 — 시딩된 부모 코드를 main에 커밋해야 worktree가 분기 시
            # 상속한다(재사용 유닛 코드 + 하류 빌드 base). ensure_repo의 초기 커밋은 빈 트리라,
            # 커밋 안 하면 worktree가 빈 main에서 분기해 재사용 코드를 못 본다(delta DAG의 전제).
            # commit_scaffold = git add -A + commit(best-effort·멱등; node_modules는 미시딩).
            if commit_scaffold(wm.workdir, message="haetae: seeded parent (continue-from)"):
                emit("이어가기: 시딩된 부모 코드를 main에 커밋 (worktree 분기 base)")

        if not state.plan:
            state.status = Status.escalated
            state.pending_escalations.append(
                {"reason": "병렬 실행에는 decomposition(units)이 필요하다 — 비어있음"})
            emit(_final_label(state))
            try_save()
            return state

        # dispatch 한 라운드(ThreadPoolExecutor)를 함수로 — 통합 OR 재시도 시 재호출한다.
        # 같은 스케줄러/봉투(state.plan/attempts/alt_*)를 재사용하므로 결정성/격리 불변.
        def run_round() -> None:
            nonlocal terminal, total_attempts
            in_flight: set[str] = set()
            with ThreadPoolExecutor(max_workers=max_parallel) as pool:
                futures: dict[Future, tuple[str, NextOrder, Path]] = {}

                def dispatch_ready() -> None:
                    nonlocal terminal
                    if terminal:
                        return
                    # WO#68 (B): 전역 예산 cap — 새 unit dispatch *전*에 누적 토큰이 --max-tokens
                    #   초과면 더 안 던지고 clean stop(외부 컷오프 전 의도적). 미설정=무제한(무회귀).
                    if over_global_budget():
                        terminal = "budget"
                        state.pending_escalations.append({
                            "reason": (
                                f"예산 초과 — 누적 토큰 {state.budget.spent.tokens} > "
                                f"--max-tokens {max_tokens}. 충전/상한 조정 후 --continue-from으로 재개"
                            ),
                        })
                        return
                    for u in ready_units(state.plan, in_flight):
                        if terminal or len(futures) >= max_parallel:
                            break
                        order = gen_order(u)  # main 스레드 — 직렬·결정적
                        if order is None:  # escalate/stop/replan-소진 → terminal 세팅됨
                            break
                        wt = wm.create(u)
                        # 선제 스캐폴드(또는 이어가기 시딩): executor dispatch *전에* node_modules를
                        # worktree에 준비. (gitignore라 git 상속 못 함 → main에서 symlink/copy,
                        # 없으면 host-install 폴백.) WO#58: seeded면 부모 package.json 기준 동일 준비.
                        if ((scaffold is not None and scaffold.install) or seeded) and install_deps:
                            how = prepare_worktree_deps(
                                wm.workdir, wt,
                                ensure_deps_fn=lambda p: ensure_deps(p, runner=deps_runner),
                            )
                            emit(f"worktree node_modules 준비: {u} ({how})")
                        _set_plan_state(state, u, PlanState.in_progress)
                        in_flight.add(u)
                        # WO#33: dispatch=build 단계 → 라이브 activity + 전이 이력(main 스레드).
                        activity_start(u, STAGE_BUILD)
                        record_transition(STAGE_BUILD, u)
                        # WO#64: 이 시도의 tier(base+escalation, top cap)를 *빌더 팩토리에만* 넘긴다.
                        # gate_factory엔 tier 미전달(_build_executor는 executor 전용) → judge/critic
                        # 모델 불변(적대 분리). 사다리 미지정/1-arg 팩토리면 tier 무시(후방호환).
                        tier = tier_for(u)
                        build_kind = "빌드"
                        # WO#70: 이번 시도 tier 라벨을 기록(머지/판정 시 빌더 비용 태그용).
                        # 단일 tier(tier_top==0)면 None(날조 금지 — tier 의미 없음).
                        tier_label_of[u] = tier_label(tier) if tier_top > 0 else None
                        if tier_top > 0:  # 다중 tier일 때만 하트비트/이벤트에 tier 표면화
                            label = tier_label(tier)
                            build_kind = f"빌드(재시도 {escalation_of.get(u, 0)} · tier={label})"
                            emit(f"{u} 빌드 dispatch (재시도 {escalation_of.get(u, 0)} · tier={label})")
                        # 스킬 주입은 executor로 가는 order에만. gate는 order.unit만 읽으므로
                        # 증강 order를 _exec_and_gate에 줘도 gate엔 스킬이 새지 않는다(분리 보존).
                        fut = pool.submit(
                            _exec_and_gate, _build_executor(executor_factory, wt, tier),
                            gate_factory(wt),
                            apply_skills(order), spec, pricing, heartbeat, build_kind)
                        futures[fut] = (u, order, wt)

                dispatch_ready()
                while futures and not terminal:
                    done_set, _ = wait(list(futures), return_when=FIRST_COMPLETED)
                    # 완료분을 unit-id 순으로 처리 → 머지 직렬화 + 처리 순서 결정적
                    for fut in sorted(done_set, key=lambda f: futures[f][0]):
                        unit, order, _wt = futures.pop(fut)
                        in_flight.discard(unit)
                        total_attempts += 1
                        exec_cost: Cost | None = None
                        try:
                            result, gr, exec_cost = fut.result()
                        except CodexUsageLimitError:
                            # WO#68 (A): 빌드 worker의 크레딧 소진은 *유닛 실패*가 아니라 run 전체
                            #   graceful stop이다 — 일반 예외로 흡수(재시도/OR)하지 말고 전파해
                            #   _parallel_loop의 top-level 핸들러가 stopped_credit로 봉인하게 한다.
                            raise
                        except Exception as e:  # noqa: BLE001 — executor/gate 예외=그 unit 실패
                            result = f"(executor/gate 예외: {e})"
                            gr = GateResult(verdict=Verdict.fail_recoverable)
                        handle_outcome(unit, order, result, gr, exec_cost)
                        if total_attempts >= max_iters and not terminal:
                            terminal = "stuck"
                    if terminal:
                        break
                    dispatch_ready()

        # ── 통합 OR 루프(WO#41): 라운드 완주 → 통합 gate. 실패+대안 남으면 *다른 접근*으로 재라운드. ──
        # bar 불변: 통합 대안도 같은 criteria/done_when을 둔 채 접근만 바꾼다(같은 독립 gate가 판정).
        integration_ev: Event | None = None
        # WO#52: 통합 백트래킹 체크포인트 — 첫 OR 진입 시 기록한 all-merged 깨끗 상태(ref).
        # 통합 대안 사이/escalate 시 main을 *이 ref*로 git-reset해 오염된 대안을 폐기한다.
        checkpoint_C: str | None = None
        while True:
            run_round()
            if terminal:  # 유닛-level done(brain stop)/escalate/stuck → 아래 매핑.
                break
            if all_done(state.plan):
                # 통합 gate: 머지된 main에서 spec 체크 1회 → cross-unit 깨짐 포착(판정 로직 불변).
                emit("통합 gate 검사 중…")
                record_transition(STAGE_VERIFY, None)
                hb("통합 judge")  # WO#55: 통합 gate codex 호출 라이브 표면화
                igr = integration_gate.judge("(integration — 머지된 main 통합 검사)", spec)
                emit(_summarize_gate(igr))
                # WO#70: 통합 judge 비용 — kind=judge·unit=None(통합/run-level). budget+ledger.
                tag_cost(igr.judge_cost, kind="judge")
                account(igr.judge_cost)  # 통합 gate judge 비용을 budget에 누적
                integration_ev = Event(
                    seq=0, unit=None, work_order_ref="(integration)",
                    result="통합 gate(머지된 main)", verdict=igr.verdict, checks=igr.checks,
                    cost=igr.judge_cost, ts=now(), stage=STAGE_VERIFY)
                if igr.verdict in (Verdict.pass_, Verdict.done):
                    state.status = Status.done
                    break
                # 통합 실패 + 대안 남음 → 영향 유닛을 다른 접근으로 백트래킹·재계획(bounded).
                if integration_alt < or_alternatives:
                    # WO#52: 진짜 백트래킹 — 통합 대안 사이에 main을 *깨끗한 all-merged
                    # 체크포인트*로 git-reset해 직전 실패 대안의 오염을 폐기한다.
                    #  · 첫 OR 진입(integration_alt==0): 지금 main(=all-units-merged 클린)을 C로 기록.
                    #  · 이후 대안 전(첫 대안 제외): reset_main_to(C)로 직전 대안 커밋 폐기 후 재시작.
                    # best-effort: 기록/reset 실패 시 #41 동작(현재 main 위 재dispatch)으로 폴백.
                    if integration_alt == 0:
                        checkpoint_C = wm.checkpoint()
                    elif checkpoint_C is not None and wm.reset_main_to(checkpoint_C):
                        emit("통합 백트래킹: main을 깨끗한 all-merged 체크포인트로 reset")
                    elif checkpoint_C is not None:
                        emit("통합 백트래킹 reset 불가 — #41 폴백(현재 main 위 재dispatch)")
                    evidence = summarize_gate_evidence(igr)
                    state.approaches.append(ApproachAttempt(
                        scope="integration", approach=f"통합 접근 {integration_alt}",
                        outcome="abandoned", evidence=evidence, index=integration_alt))
                    integration_alt += 1
                    fb = build_alternative_feedback(None, evidence, scope="integration")
                    for item in state.plan:  # 백트래킹: 유닛 pending 리셋 + 다른 접근 seed
                        alt_feedback[item.unit] = fb
                        attempts_of[item.unit] = 0
                        _set_plan_state(state, item.unit, PlanState.pending)
                    record_transition(STAGE_OR_ALTERNATIVE, None)
                    emit(f"OR 통합 대안 재계획 (대안 {integration_alt}/{or_alternatives}) — bar 불변")
                    persist()
                    continue  # 다른 접근으로 재라운드
                # 통합 대안 소진(또는 OR OFF) → escalate. OR>0이면 접근 첨부, 0이면 기존 동작.
                # WO#52: 오염된 마지막 대안을 남기지 않고 깨끗한 all-merged 체크포인트(C)로
                # 되돌려 escalate(검사 가능한 상태를 깔끔히). best-effort(없거나 실패면 그대로).
                if checkpoint_C is not None:
                    wm.reset_main_to(checkpoint_C)
                state.status = Status.escalated
                if or_alternatives > 0:
                    state.approaches.append(ApproachAttempt(
                        scope="integration", approach=f"통합 접근 {integration_alt}",
                        outcome="exhausted", evidence=summarize_gate_evidence(igr), index=integration_alt))
                    tried = [a.model_dump(mode="json") for a in state.approaches if a.scope == "integration"]
                    state.pending_escalations.append(
                        {"reason": f"통합 gate 실패 — cross-unit breakage (OR 대안 {or_alternatives} 소진)",
                         "verdict": igr.verdict.value, "approaches_tried": tried})
                else:  # OR OFF → 기존 escalate(후방호환)
                    state.pending_escalations.append(
                        {"reason": "통합 gate 실패 — cross-unit breakage",
                         "verdict": igr.verdict.value})
                break
            else:  # 라운드 완주했는데 all_done 아님 → 진전 불가(deadlock)
                state.status = Status.stopped_stuck
                break

        # 유닛-level terminal을 최종 status로 매핑(통합 분기는 위에서 이미 status 설정).
        if terminal == "escalated":
            state.status = Status.escalated
        elif terminal == "done":  # brain이 stop으로 done_when 충족 선언
            state.status = Status.done
        elif terminal == "stuck":
            state.status = Status.stopped_stuck
        elif terminal == "budget":  # WO#68 (B): 전역 예산 cap 초과 → clean stop(재개 가능)
            state.status = Status.stopped_budget

        materialize(integration_ev)
        emit(_final_label(state))
        try_save()
        return state
    except CodexUsageLimitError as e:
        # WO#68 (A): 병렬 경로의 codex 크레딧 소진(빌드 worker·gen_order replan·통합 judge 어디서든)
        #   → traceback 크래시 대신 graceful stop. 진행분 materialize→봉인(stopped_credit)→저장.
        #   worktree 정리는 아래 finally(cleanup_all)가 보장. best-effort(2차 크래시 금지).
        try:
            if state is not None and state.status == Status.running:
                state.status = Status.stopped_credit
                state.pending_escalations.append({
                    "reason": "codex 크레딧 소진 — 충전 후 `--continue-from`으로 재개(완료 유닛 보존)",
                    "detail": str(e),
                })
        except Exception:  # noqa: BLE001
            pass
        try:
            materialize()  # 진행분(per-unit 이벤트) 봉인 — 통합 ev는 크레딧 stop 시 없음
        except Exception:  # noqa: BLE001
            pass
        try:
            emit(_final_label(state))
        except Exception:  # noqa: BLE001
            pass
        try_save()
        return state
    except KeyboardInterrupt:
        # 웹 stop/SIGINT(예: OR 대안 replan 중 codex.complete에서 KeyboardInterrupt):
        #   진행분을 materialize→저장(persist)하고 클린 반환한다. worktree/산출물 정리는
        #   아래 finally(cleanup_all)가 보장한다(#21). best-effort(2차 크래시 금지).
        _finalize_interrupt(state, emit, persist)
        return state
    finally:
        # 타협 불가: done/escalate/예외/Ctrl-C 어떤 경로든 흔적 0.
        wm.cleanup_all()
