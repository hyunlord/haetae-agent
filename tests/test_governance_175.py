"""WO#175 거버넌스 무결성 — rename-evasion 차단 + 순차 fixation governed-escalate.

#174 라이브: 약 brain이 replan서 (1) decomp-critic 거부를 *유닛 id 발명*으로 우회
(u2→u2h→u1×7) (2) 한 유닛 무진전 churn. 둘 다 brain 거버넌스 슬립 — gate 판정 아님.
이 테스트는 director-side 가드가 *거부의 불변성*(rename 무력화 봉쇄)과 *정직한 정지*
(조용한 churn 금지)를 강제하되 **과차단 0·바 불완화·gate 로직 불변**임을 확인한다.

mock LLM/executor/gate만 사용(네트워크/시크릿 없음). 순차 경로 전용
(병렬은 스케줄러가 unit id 권위 → rename 불가).
"""

from pathlib import Path

from haetae.decomp_critic import decomp_signature
from haetae.loop import MockExecutor, MockGate, run_loop
from haetae.llm import MockClient
from haetae.models import CheckReport, GateResult, NextOrder, Status, Verdict

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPT_DIR = REPO_ROOT / "prompts"

# u1/u2/u3 모두 spec에 둠(plan-state 안정). rename-evasion은 *내용 시그니처*로 잡는다.
SPEC_YAML = """\
spec_id: gov-175
version: 1
order_raw: "틱택토 — 룰+미니맥스+테스트"
goal: "틱택토를 헤드리스 순수 로직으로 구현"
task_type: feature_impl
verifiability: objective
mode: normal
constraints: []
acceptance_criteria:
  - id: ac1
    desc: "게임 로직"
    check: { type: test, cmd: "pytest tests/test_game.py" }
assumptions: []
non_goals: ["GUI"]
done_when: "ac1 통과"
decomposition:
  - { unit: u1, desc: "게임 로직", deps: [] }
  - { unit: u2, desc: "미니맥스", deps: [u1] }
  - { unit: u3, desc: "테스트", deps: [u1, u2] }
open_questions: []
"""

_CRIT_ADEQUATE = "verdict: adequate\ngaps: []\n"
_DC_WEAK = 'verdict: weak\nreason: "전체 done_when 재진술 — 무진전"\n'
_DC_PROGRESS = 'verdict: progress\nreason: "한 조각만 좁힘 — 진전"\n'

# 미니맥스 작업 — *동일 내용*(goal/scope/deliverable). unit id만 바꿔 rename-evasion 모사.
_MINIMAX_GOAL = "미니맥스 알고리즘으로 최적 수를 선택한다"
_MINIMAX_SCOPE = "src/ai.py"


def _order(unit: str, goal: str, scope: str | None = None, deliverable: str = "요약") -> str:
    scope_line = f'\n  scope: "{scope}"' if scope else ""
    return f"""\
verdict: pass
action: next_order
rationale: "{unit} 진행"
next_order:
  unit: {unit}
  goal: "{goal}"{scope_line}
  local_checks: [{{ type: test, cmd: "pytest tests/test_{unit}.py" }}]
  executor: codex
  deliverable: "{deliverable}"
"""


# ──────────────────────── decomp_signature (순수 단위) ────────────────────────


def test_decomp_signature_id_excluded_content_sensitive():
    """id 제외·내용 민감: 같은 내용 다른 id → 동일 sig; 다른 내용 → 다른 sig."""
    a = NextOrder(unit="u2", goal=_MINIMAX_GOAL, scope=_MINIMAX_SCOPE, deliverable="요약")
    a2 = NextOrder(unit="u2h", goal=_MINIMAX_GOAL, scope=_MINIMAX_SCOPE, deliverable="요약")
    b = NextOrder(unit="u2", goal="게임 보드 표현", scope="src/board.py", deliverable="요약")
    assert decomp_signature(a) == decomp_signature(a2)  # id만 다름 → 같은 작업
    assert decomp_signature(a) != decomp_signature(b)  # 내용 다름 → 다른 작업
    # 공백/대소문자 정규화(사소한 변형도 같은 작업으로)
    c = NextOrder(unit="x", goal="  Mini  Max ", scope=None)
    d = NextOrder(unit="y", goal="mini max", scope=None)
    assert decomp_signature(c) == decomp_signature(d)


# ──────────────────────── rename-evasion 차단 ────────────────────────


def test_rename_evasion_blocked_then_governed_escalate():
    """거부된 작업을 *id만 바꿔* 재제출 → dispatch 차단(critic verdict 불변) → bound 후 escalate.

    핵심: iter2/iter3서 critic이 *progress*로 票를 바꿔도 차단된다(rename으로 무력화 봉쇄).
    """
    # brain: synth + u2(원) + u2a(재제출#1) + u2b(재제출#2) — 셋 다 동일 내용.
    brain = MockClient([
        SPEC_YAML,
        _order("u2", _MINIMAX_GOAL, _MINIMAX_SCOPE),
        _order("u2a", _MINIMAX_GOAL, _MINIMAX_SCOPE),
        _order("u2b", _MINIMAX_GOAL, _MINIMAX_SCOPE),
    ])
    # critic: spec adequate, 분해 u2 weak(거부→sig 등록), 이후 progress(그래도 차단돼야).
    critic = MockClient([_CRIT_ADEQUATE, _DC_WEAK, _DC_PROGRESS, _DC_PROGRESS])
    ex = MockExecutor("u2 build")
    state = run_loop(
        order="x", client=brain, executor=ex, gate=MockGate(Verdict.fail_recoverable),
        critic_client=critic, decomp_retries=0, rename_block_bound=1,
        max_iters=10, prompt_dir=PROMPT_DIR,
    )
    assert state.status is Status.escalated
    esc = [e for e in state.pending_escalations if "rename-evasion" in str(e.get("reason", ""))]
    assert esc, f"rename-evasion escalate 없음: {state.pending_escalations}"
    assert "자동 완화 없음" in esc[-1]["reason"]  # anti-erosion(바 불완화)
    # 원 작업(u2)만 dispatch됐고 id-스왑 재제출(u2a/u2b)은 dispatch 안 됨(차단).
    assert len(ex.calls) == 1
    # 거부 시그니처가 등록됐다(원 유닛 u2로).
    assert decomp_signature(
        NextOrder(unit="zz", goal=_MINIMAX_GOAL, scope=_MINIMAX_SCOPE, deliverable="요약")
    ) in state.rejected_decomp_signatures


def test_rename_evasion_same_id_resubmit_not_blocked():
    """같은 id 재제출(u1→u1)은 정상 반복 — 차단 0. critic이 progress로 올리면 채택·done."""
    brain = MockClient([SPEC_YAML, _order("u1", _MINIMAX_GOAL, _MINIMAX_SCOPE),
                        _order("u1", _MINIMAX_GOAL, _MINIMAX_SCOPE)])
    critic = MockClient([_CRIT_ADEQUATE, _DC_WEAK, _DC_PROGRESS])
    ex = MockExecutor("u1 build")
    state = run_loop(
        order="x", client=brain, executor=ex, gate=MockGate(Verdict.done),
        critic_client=critic, decomp_retries=1, rename_block_bound=1,
        max_iters=10, prompt_dir=PROMPT_DIR,
    )
    assert state.status is Status.done  # 같은 id는 rename-evasion 아님 → 과차단 0
    assert not any("rename-evasion" in str(e.get("reason", "")) for e in state.pending_escalations)
    assert len(ex.calls) == 1


def test_rename_evasion_different_content_not_blocked():
    """다른 내용(시그니처 상이)은 차단 안 됨 — 거부된 u1과 무관한 u2 작업은 정상 dispatch."""
    brain = MockClient([
        SPEC_YAML,
        _order("u1", "게임 보드 표현/표시", "src/board.py"),  # 거부될 작업
        _order("u2", _MINIMAX_GOAL, _MINIMAX_SCOPE),          # 다른 내용 — 통과해야
    ])
    critic = MockClient([_CRIT_ADEQUATE, _DC_WEAK, _DC_PROGRESS])
    ex = MockExecutor("build")
    state = run_loop(
        order="x", client=brain, executor=ex, gate=MockGate(Verdict.done),
        critic_client=critic, decomp_retries=1, rename_block_bound=1,
        max_iters=10, prompt_dir=PROMPT_DIR,
    )
    assert state.status is Status.done
    assert not any("rename-evasion" in str(e.get("reason", "")) for e in state.pending_escalations)
    assert len(ex.calls) == 1  # u2(다른 내용) dispatch


# ──────────────────────── 순차 fixation governed-escalate ────────────────────────


def _fail_check(ac_id: str = "ac1") -> CheckReport:
    return CheckReport(ac_id=ac_id, check_type="test", status="fail", exit_code=1)


def test_serial_fixation_governed_escalate():
    """같은 유닛이 *같은 gate fail 지문*으로 N회 무진전 → governed escalate(정직 정지)."""
    brain = MockClient([SPEC_YAML] + [_order("u1", "u1 구현") for _ in range(6)])
    # 매 호출 동일 fail 지문(ac1/test) → 무진전.
    gate = MockGate(Verdict.fail_recoverable, checks=[_fail_check("ac1")])
    ex = MockExecutor("u1 build")
    state = run_loop(
        order="x", client=brain, executor=ex, gate=gate,
        decomp_retries=0, fixation_escalate=3, max_iters=20, prompt_dir=PROMPT_DIR,
    )
    assert state.status is Status.escalated
    esc = [e for e in state.pending_escalations if "fixation" in str(e.get("reason", ""))]
    assert esc, f"fixation escalate 없음: {state.pending_escalations}"
    assert "자동 완화 없음" in esc[-1]["reason"]
    # 3회 무진전 후 정지 — 무한 churn 아님(시도 횟수가 bound 근처).
    assert len(ex.calls) == 3


class _VaryingFailGate:
    """매 호출 *다른* ac_id로 fail → fixation 지문이 계속 바뀜(진전 신호 모사)."""

    def __init__(self) -> None:
        self.n = 0
        self.calls: list[str] = []

    def judge(self, result, spec, unit=None) -> GateResult:
        self.calls.append(result)
        self.n += 1
        return GateResult(
            verdict=Verdict.fail_recoverable,
            checks=[CheckReport(ac_id=f"ac{self.n}", check_type="test", status="fail")],
        )


def test_serial_fixation_resets_on_progress():
    """진전(다른 fail 지문)이면 fixation 카운트 깨짐 → 안 죽임(느리지만 나아가는 유닛 보호)."""
    brain = MockClient([SPEC_YAML] + [_order("u1", "u1 구현") for _ in range(6)])
    gate = _VaryingFailGate()  # 지문이 매번 바뀜 → 절대 N회 연속 동일 아님
    ex = MockExecutor("u1 build")
    state = run_loop(
        order="x", client=brain, executor=ex, gate=gate,
        decomp_retries=0, fixation_escalate=3, max_iters=5, prompt_dir=PROMPT_DIR,
    )
    # 지문이 매번 달라 fixation escalate가 발화하면 안 된다(진전 = 리셋).
    assert not any("fixation" in str(e.get("reason", "")) for e in state.pending_escalations)
    assert gate.n >= 4  # 여러 번 시도했는데도 fixation으로 안 죽었다


# ──────────────────────── 무결성/적대 분리 ────────────────────────


def test_governance_guards_are_director_side_not_gate():
    """가드는 brain 거버넌스(replan/decomp_critic/loop)지 gate 판정 아님 — 소스 스캔.

    decomp_signature가 decomp_critic.py에 있고, 이 모듈은 gate/run_judge를 참조하지 않는다
    (기존 test_decomp_critic 분리 가드와 동형). loop의 rename/fixation 가드는 *기존* 신호
    함수(fixation_fail_digest)만 쓴다 — gate 판정 로직 미변경.
    """
    dc_src = REPO_ROOT.joinpath("src/haetae/decomp_critic.py").read_text(encoding="utf-8")
    for forbidden in ("haetae.gate", "run_judge", "haetae.judge", "GateResult", "CompositeGate"):
        assert forbidden not in dc_src, f"decomp_critic이 {forbidden} 참조(분리 위반)"


def test_rename_and_fixation_escalate_do_not_lower_bar():
    """두 escalate 모두 *정직 정지*지 바 완화 아님 — spec_changes 0(기준 자동 변경 없음)."""
    # rename-evasion 경로
    brain = MockClient([
        SPEC_YAML,
        _order("u2", _MINIMAX_GOAL, _MINIMAX_SCOPE),
        _order("u2a", _MINIMAX_GOAL, _MINIMAX_SCOPE),
        _order("u2b", _MINIMAX_GOAL, _MINIMAX_SCOPE),
    ])
    critic = MockClient([_CRIT_ADEQUATE, _DC_WEAK, _DC_PROGRESS, _DC_PROGRESS])
    state = run_loop(
        order="x", client=brain, executor=MockExecutor("a"), gate=MockGate(Verdict.fail_recoverable),
        critic_client=critic, decomp_retries=0, rename_block_bound=1,
        max_iters=10, prompt_dir=PROMPT_DIR,
    )
    assert state.status is Status.escalated
    assert state.spec_changes == []  # 바(criteria/done_when) 자동 변경 없음(anti-erosion)


def test_completion_still_works_with_guards_on():
    """가드 ON이어도 정상 완료는 그대로 — 완료=gate-pass 사실(#173), 가드는 churn만 잡는다."""
    brain = MockClient([SPEC_YAML, _order("u1", "u1 구현")])
    state = run_loop(
        order="x", client=brain, executor=MockExecutor("done"), gate=MockGate(Verdict.done),
        decomp_retries=0, rename_block_bound=1, fixation_escalate=3,
        max_iters=10, prompt_dir=PROMPT_DIR,
    )
    assert state.status is Status.done  # 가드가 정상 경로를 막지 않는다
