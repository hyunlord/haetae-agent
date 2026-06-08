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
from haetae.deps import Runner as DepsRunner, ensure_deps
from haetae.intake import SynthesisError, synthesize
from haetae.llm import LLMClient
from haetae.metering import (
    MeteredClient,
    accumulate,
    combine_costs,
    cost_from_usage,
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
from haetae.decomp_critic import (
    DEFAULT_DECOMP_CRITIC_PROMPT_PATH,
    build_decomp_feedback,
    critique_decomposition,
    is_weak,
)
from haetae.or_node import build_alternative_feedback, summarize_gate_evidence
from haetae.replan import ReplanError, replan
from haetae.scheduler import all_done, ready_units
from haetae.skills import Skill, inject_skills, load_skills, match_skills
from haetae.spec_change import apply_spec_change
from haetae.spec_critic import synthesize_with_critique
from haetae.worktree import WorktreeError, WorktreeManager

# 병렬 모드에서 worktree 경로를 받아 그 경로에 묶인 Executor/Gate를 만드는 팩토리.
# (Executor/Gate는 생성 시 workdir이 고정되므로 unit마다 worktree로 다시 묶어야 한다.)
ExecutorFactory = Callable[[Path], "Executor"]
GateFactory = Callable[[Path], "Gate"]


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
    pricing=None,
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
    result = executor.run(order)
    exec_cost = _executor_cost(executor, pricing)
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
    worktree_manager: WorktreeManager | None = None,
    scaffold_client: LLMClient | None = None,
    install_deps: bool = True,
    deps_runner: DepsRunner | None = None,
    skills_dir: str | Path | None = None,
    pricing: dict | None = None,
    clock: Callable[[], str] | None = None,
    activity_observer: Callable[[list["Activity"]], None] | None = None,
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

    # ── 계측 헬퍼(WO#33) — 전부 best-effort: 어떤 예외도 run을 죽이지 않는다 ──────
    def now() -> str | None:
        try:
            return clock() if clock is not None else _utcnow_iso()
        except Exception:  # noqa: BLE001
            return None

    def account(cost: Cost | None) -> None:
        """budget.spent에 누적(best-effort)."""
        try:
            accumulate(state.budget.spent, cost)
        except Exception:  # noqa: BLE001
            pass

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
    m_critic = (
        MeteredClient(critic_client, source="orchestration", pricing=pricing)
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
        emit("합성 중…")
        critique: SpecCritique | None = None
        try:
            if critic_client is not None:
                # opt-in 적대적 critic: 비평 surface + 구체 gap이면 바운드 1회 재합성.
                spec, critique = synthesize_with_critique(
                    order, m_client, m_critic,
                    syn_prompt_path=syn_prompt, critic_prompt_path=critic_prompt,
                )
            else:
                spec = synthesize(order, m_client, prompt_path=syn_prompt)
        except SynthesisError as e:
            state = _escalated_no_spec(
                "spec 합성 실패 (synthesize 출력 검증 불통과)", e.raw_response
            )
            record_transition(STAGE_SYNTHESIZE)
            # 합성 실패라도 거기까지 든 비용은 정직하게 누적(state 생성 후 — budget 존재).
            account(combine_costs(m_client.drain()))
            if m_critic is not None:
                account(combine_costs(m_critic.drain()))
            emit(_final_label(state))
            try_save()
            return state

        state = _init_state(spec)
        record_transition(STAGE_SYNTHESIZE)
        # 합성/critic(전역 단계) 비용을 budget에 누적(특정 유닛 event 아님).
        account(combine_costs(m_client.drain()))
        if m_critic is not None:
            account(combine_costs(m_critic.drain()))
        if critique is not None:
            state.spec_critique = critique  # 감사 기록(재합성 발생 여부 포함)
            emit(_critique_label(critique))
            try_save()

        # 선제 스캐폴드(WO#27): executor *dispatch 전에* host가 진짜 스택을 깐다.
        # scaffold_client 없으면 None → 모든 신규 경로 no-op(기존 동작 불변, critic 패턴과 동형).
        # generate_scaffold는 best-effort라 dep 스택 불필요/실패면 None을 돌려준다.
        scaffold: Scaffold | None = None
        if scaffold_client is not None:
            emit("scaffold 생성 중…")
            record_transition(STAGE_SCAFFOLD)
            scaffold = generate_scaffold(spec, m_scaffold, prompt_path=scaffold_prompt)
            account(combine_costs(m_scaffold.drain()))  # scaffold(전역 단계) 비용 누적
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
                rep_prompt=rep_prompt,
                emit=emit,
                try_save=try_save,
                scaffold=scaffold,
                install_deps=install_deps,
                deps_runner=deps_runner,
                apply_skills=apply_skills,
                pricing=pricing,
                now=now,
                account=account,
                record_transition=record_transition,
                activity_start=activity_start,
                activity_end=activity_end,
                decomp_retries=decomp_retries,
                run_decomp_critic=run_decomp_critic,
                account_decomp_cost=(
                    (lambda: account(combine_costs(m_critic.drain())))
                    if m_critic is not None
                    else (lambda: None)
                ),
                or_alternatives=or_alternatives,
            )

        # 순차(N=1): worktree 없음 → workdir에 직접 scaffold 쓰기 + host-install(커밋 불필요).
        # scaffold=None이면 no-op → 기존 순차 경로 불변.
        if scaffold is not None:
            wd = Path(workdir or ".")
            written = write_scaffold(scaffold, wd)
            emit(f"scaffold 적용: {len(written)}개 파일 (workdir, executor 전)")
            if scaffold.install and install_deps:
                res = ensure_deps(wd, runner=deps_runner)
                emit(f"scaffold host-install: {res.manager} ok={res.ok}")

        last_result = "(시작 — 아직 실행 없음)"
        # WO#25 Part A: 직전에 dispatch한 작업 유닛. brain이 다른 유닛으로 넘어가면(advance)
        # 이 유닛을 done으로 수용하고, escalate면 이 유닛을 failed로 표시한다.
        worked_unit: str | None = None

        iters = 0
        while iters < max_iters and state.status == Status.running:
            iters += 1

            # replan(비결정 LLM 출력 → 검증 실패 흡수) + 분해 critic(WO#40).
            # 바깥 루프 = 분해 critic 재계획(bounded decomp_retries), 안쪽 = replan 파싱 재시도.
            # weak 판정 → 피드백 주고 재replan; 재시도 소진 후에도 weak면 *진행*(데드락 금지)+기록.
            record_transition(STAGE_REPLAN)
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
            replan_cost = combine_costs(m_client.drain())
            # 분해 critic(verifier-side) 비용도 정직하게 누적(코스트 패널에 보임).
            if m_critic is not None:
                account(combine_costs(m_critic.drain()))
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
                result = executor.run(apply_skills(no))  # 스킬 주입은 executor에만(빌더 전용)
                exec_cost = _executor_cost(executor, pricing)  # 읽기만(best-effort)
                # WO#33: gate 진입=verify 단계로 갱신.
                activity_set_stage(no.unit, STAGE_VERIFY)
                record_transition(STAGE_VERIFY, no.unit)
                emit("gate 검사 중…")
                gr = gate.judge(result, spec)
                verdict = gr.verdict
                emit(_summarize_gate(gr))
                activity_end(no.unit)  # 완료 → 라이브 activity에서 제거
                # 이 유닛 처리 비용 = replan(orchestration) + executor + judge(gate) 귀속.
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
    emit: Callable[[str], None],
    try_save: Callable[[], None],
    scaffold: Scaffold | None = None,
    install_deps: bool = True,
    deps_runner: DepsRunner | None = None,
    apply_skills: Callable[[NextOrder], NextOrder] = lambda o: o,
    pricing: dict | None = None,
    now: Callable[[], str | None] = lambda: None,
    account: Callable[[Cost | None], None] = lambda c: None,
    record_transition: Callable[[str, str | None], None] = lambda s, u=None: None,
    activity_start: Callable[[str | None, str], None] = lambda u, s: None,
    activity_end: Callable[[str | None], None] = lambda u: None,
    decomp_retries: int = 1,
    run_decomp_critic: Callable[[NextOrder, str], "DecompCritique | None"] = lambda no, last: None,
    account_decomp_cost: Callable[[], None] = lambda: None,
    or_alternatives: int = 1,
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
        evs: list[Event] = []
        for i, e in enumerate(ordered, start=1):
            evs.append(Event(
                seq=i, unit=e["unit"], work_order_ref=e["goal"],
                result=e["result"], verdict=e["verdict"], checks=e["checks"],
                cost=e.get("cost"), ts=e.get("ts"), stage=STAGE_BUILD,
            ))
        if integration_ev is not None:
            integration_ev.seq = len(evs) + 1
            evs.append(integration_ev)
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
        orch_cost_of[unit] = combine_costs(client.drain())
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
        event_cost = combine_costs([orch_cost_of.pop(unit, None), exec_cost, gr.judge_cost])
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
            # 머지 충돌 → 직렬화 재dispatch(갱신된 main 위), 소진 시 escalate
            wm.discard(unit)
            if attempts_of[unit] < unit_retries:
                attempts_of[unit] += 1
                account(event_cost)  # 폐기된 시도도 비용은 정직하게 budget에 누적
                emit(f"머지 충돌 → 직렬화 재dispatch: {unit} (재시도 {attempts_of[unit]})")
                _set_plan_state(state, unit, PlanState.pending)
            else:
                account(event_cost)
                record(unit, order.goal, result, Verdict.fail_replan, gr.checks,
                       cost=event_cost, ts=now())
                _set_plan_state(state, unit, PlanState.failed)
                terminal = "escalated"
                state.pending_escalations.append(
                    {"reason": f"unit {unit} 머지 충돌 {unit_retries}회 후 미해소 — escalate",
                     "unit": unit})
                persist()
            return

        # gate 실패 → 바운드 *재시도*(같은 접근, 재dispatch). 재시도 소진 후엔 OR 대안.
        wm.discard(unit)  # 백트래킹: 실패 접근 worktree 정리(#21 보장 cleanup)
        if attempts_of[unit] < unit_retries:
            attempts_of[unit] += 1
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
                    if terminal:
                        return
                    for u in ready_units(state.plan, in_flight):
                        if terminal or len(futures) >= max_parallel:
                            break
                        order = gen_order(u)  # main 스레드 — 직렬·결정적
                        if order is None:  # escalate/stop/replan-소진 → terminal 세팅됨
                            break
                        wt = wm.create(u)
                        # 선제 스캐폴드: executor dispatch *전에* node_modules를 worktree에 준비.
                        # (gitignore라 git 상속 못 함 → main에서 symlink/copy, 없으면 host-install 폴백.)
                        if scaffold is not None and scaffold.install and install_deps:
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
                        # 스킬 주입은 executor로 가는 order에만. gate는 order.unit만 읽으므로
                        # 증강 order를 _exec_and_gate에 줘도 gate엔 스킬이 새지 않는다(분리 보존).
                        fut = pool.submit(
                            _exec_and_gate, executor_factory(wt), gate_factory(wt),
                            apply_skills(order), spec, pricing)
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
        while True:
            run_round()
            if terminal:  # 유닛-level done(brain stop)/escalate/stuck → 아래 매핑.
                break
            if all_done(state.plan):
                # 통합 gate: 머지된 main에서 spec 체크 1회 → cross-unit 깨짐 포착(판정 로직 불변).
                emit("통합 gate 검사 중…")
                record_transition(STAGE_VERIFY, None)
                igr = integration_gate.judge("(integration — 머지된 main 통합 검사)", spec)
                emit(_summarize_gate(igr))
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

        materialize(integration_ev)
        emit(_final_label(state))
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
