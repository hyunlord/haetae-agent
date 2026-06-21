"""분해 critic(WO#40, Phase C) 단위 테스트 — mock LLM만(네트워크/시크릿 없음).

critique_decomposition: progress/weak 판정 + best-effort 흡수(파싱 실패·client 예외).
+ 정규화 변종 흡수, is_weak, build_decomp_feedback.
"""

from pathlib import Path

import yaml

from haetae.decomp_critic import (
    build_decomp_feedback,
    critique_decomposition,
    granularity_signal,
    is_weak,
)
from haetae.llm import MockClient
from haetae.models import (
    DecompCritique,
    NextOrder,
    PlanItem,
    PlanState,
    ProjectSpec,
    State,
    Status,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPT_DIR = REPO_ROOT / "prompts"
DECOMP_PROMPT = PROMPT_DIR / "decomp_critic.md"

_SPEC_DICT = {
    "spec_id": "dc-001",
    "version": 1,
    "order_raw": "리테일 흐름 시뮬레이션을 만들어라",
    "goal": "리테일 매장 고객 흐름 시뮬레이션",
    "task_type": "feature_impl",
    "verifiability": "objective",
    "mode": "normal",
    "constraints": [],
    "acceptance_criteria": [
        {"id": "ac1", "desc": "고객 에이전트 이동", "check": {"type": "test", "cmd": "pytest x"}}
    ],
    "assumptions": [],
    "non_goals": [],
    "done_when": "리테일 흐름이 실제로 시뮬레이션됨",
    "decomposition": [
        {"unit": "u1", "desc": "데이터 모델", "deps": []},
        {"unit": "u2", "desc": "렌더", "deps": ["u1"]},
    ],
}


def _spec() -> ProjectSpec:
    return ProjectSpec.model_validate(_SPEC_DICT)


def _state() -> State:
    return State(
        spec_ref="dc-001", spec_version=1, status=Status.running,
        plan=[
            PlanItem(unit="u1", state=PlanState.pending, deps=None),
            PlanItem(unit="u2", state=PlanState.pending, deps=["u1"]),
        ],
    )


def _order(unit: str = "u1", goal: str = "데이터 모델(스키마+CRUD)만 구현") -> NextOrder:
    return NextOrder(unit=unit, goal=goal, deliverable="요약")


_PROGRESS_YAML = "verdict: progress\nreason: \"전체 goal 중 데이터 모델 한 조각만 좁힘 — 진전.\"\n"
_WEAK_YAML = "verdict: weak\nreason: \"work order goal이 전체 done_when을 그대로 재진술 — 무진전.\"\n"


# ──────────────────────────── 판정 ────────────────────────────


def test_critique_progress():
    crit = critique_decomposition(
        _order(), _spec(), _state(), MockClient([_PROGRESS_YAML]),
        prompt_path=DECOMP_PROMPT,
    )
    assert crit.verdict == "progress"
    assert not is_weak(crit)
    assert crit.unit == "u1"


def test_critique_weak_on_restated_goal():
    # 전체 goal을 그대로 재진술한 work order → weak.
    crit = critique_decomposition(
        _order(goal="리테일 매장 고객 흐름 시뮬레이션을 만들어라"),
        _spec(), _state(), MockClient([_WEAK_YAML]), prompt_path=DECOMP_PROMPT,
    )
    assert crit.verdict == "weak"
    assert is_weak(crit)
    assert crit.reason


def test_critique_user_prompt_includes_order_and_progress():
    """critic user 프롬프트에 work order(goal) + spec goal/done_when + plan 진행이 들어간다."""
    client = MockClient([_PROGRESS_YAML])
    critique_decomposition(_order(), _spec(), _state(), client, prompt_path=DECOMP_PROMPT)
    user = client.calls[0]["user"]
    assert "데이터 모델(스키마+CRUD)만 구현" in user  # work order goal
    assert "리테일 흐름이 실제로 시뮬레이션됨" in user  # spec done_when
    assert "pending: u1, u2" in user  # plan 진행


# ──────────────────────────── best-effort 흡수 ────────────────────────────


def test_critique_parse_failure_degrades_to_progress():
    """깨진 출력 → progress로 흡수(진행 막지 않음), reason에 사유."""
    crit = critique_decomposition(
        _order(), _spec(), _state(), MockClient(["이건 YAML이 아니다 {{{"]),
        prompt_path=DECOMP_PROMPT,
    )
    assert crit.verdict == "progress"
    assert not is_weak(crit)
    assert "평가 불가" in (crit.reason or "")


def test_critique_client_exception_degrades_to_progress():
    """client.complete가 던져도 raise하지 않고 progress로 흡수."""

    class _Raise:
        def complete(self, system, user, **opts):
            raise RuntimeError("codex 다운")

    crit = critique_decomposition(
        _order(), _spec(), _state(), _Raise(), prompt_path=DECOMP_PROMPT
    )
    assert crit.verdict == "progress"
    assert "실행 실패" in (crit.reason or "")


def test_critique_unknown_verdict_is_progress():
    """미지 verdict는 보수적으로 progress(오버블록 금지)."""
    crit = critique_decomposition(
        _order(), _spec(), _state(), MockClient(["verdict: maybe\nreason: x\n"]),
        prompt_path=DECOMP_PROMPT,
    )
    assert crit.verdict == "progress"


def test_critique_normalizes_weak_aliases():
    """no-progress/restated 등 변종도 weak로 정규화."""
    for alias in ("no-progress", "restated", "stuck"):
        crit = critique_decomposition(
            _order(), _spec(), _state(),
            MockClient([f"verdict: {alias}\nrationale: 무진전\n"]),
            prompt_path=DECOMP_PROMPT,
        )
        assert crit.verdict == "weak", alias
        assert crit.reason == "무진전"  # rationale → reason 흡수


# ──────────────────────────── 헬퍼 ────────────────────────────


def test_build_decomp_feedback_includes_reason():
    fb = build_decomp_feedback(DecompCritique(verdict="weak", reason="전체 재진술", unit="u1"))
    assert "전체 재진술" in fb
    assert "더 작은" in fb  # 더 작은 진전 스텝으로 분해 지시


# ──────────────────── 입도/책임 수 축 (WO#148) ────────────────────
#
# #147: snake 엔진 전체(이동+성장+충돌+먹이+점수+game-over)를 한 유닛에 몰아 약한 로컬
# 빌더가 미수렴(gate=self-test 정렬·순수 용량). granularity_signal = director-side 결정적
# 탐지(LLM 아님·gate 아님) — 과대-다행동 유닛을 critic 프롬프트에 신호로 주입(codex가 판정).


def _multi_behavior_order() -> NextOrder:
    return NextOrder(
        unit="u2",
        goal="snake 엔진 상태, 이동 규칙, 방향 전환, 먹이 섭취, 점수, 벽/자기몸 충돌, game over 로직",
        deliverable="engine 모듈과 vitest",
    )


def test_granularity_signal_flags_over_large_multi_behavior():
    sig = granularity_signal(_multi_behavior_order())
    assert sig is not None
    assert "단일-책임" in sig and ("disjoint" in sig.lower())
    assert "쪼" in sig or "분할" in sig  # 분할 권고


def test_granularity_signal_none_for_right_sized():
    # 단일 책임(이동만) — 신호 없음(과분할 nagging 방지)
    assert granularity_signal(
        NextOrder(unit="u2", goal="engine/move.js에 이동 로직만 구현(방향에 따라 머리 한 칸 전진)",
                  deliverable="move.js + move.test.js")
    ) is None
    # 충돌 한 책임(벽·자기몸 = 충돌의 하위측면, 2 clause < 임계) — 신호 없음
    assert granularity_signal(NextOrder(unit="u3", goal="collision.js: 벽 충돌, 자기몸 충돌 판정")) is None


def test_granularity_signal_exempts_integration_unit():
    # 통합/조립 유닛은 모듈을 wire(조립 파일 소유) — 다수 모듈 언급해도 면제(disjoint-scope 설계)
    assert granularity_signal(
        NextOrder(unit="u9", goal="engine.js가 move, collision, food, gameover 모듈을 조립/wire",
                  deliverable="engine.js")
    ) is None


def test_granularity_signal_wired_into_critic_prompt():
    """과대-다행동 유닛이면 입도 신호가 critic(codex) user 프롬프트에 주입된다(codex가 판정)."""
    client = MockClient([_WEAK_YAML])
    critique_decomposition(_multi_behavior_order(), _spec(), _state(), client, prompt_path=DECOMP_PROMPT)
    user = client.calls[0]["user"]
    assert "입도 신호" in user and "단일-책임" in user


def test_granularity_signal_absent_for_right_sized_prompt():
    """적정-입도 유닛엔 입도 신호 섹션 없음(과분할 권고 안 함)."""
    client = MockClient([_PROGRESS_YAML])
    critique_decomposition(_order(), _spec(), _state(), client, prompt_path=DECOMP_PROMPT)
    assert "입도 신호" not in client.calls[0]["user"]


def test_build_decomp_feedback_guides_disjoint_scope():
    fb = build_decomp_feedback(DecompCritique(verdict="weak", reason="과대-다행동 유닛", unit="u2"))
    assert "단일-책임" in fb and "disjoint" in fb.lower()


def test_decomp_critic_is_director_side_not_gate():
    """적대 분리: decomp_critic은 gate/run-judge/judge를 import·참조하지 않는다(director-side 계획)."""
    src = REPO_ROOT.joinpath("src/haetae/decomp_critic.py").read_text(encoding="utf-8")
    for forbidden in ("haetae.gate", "run_judge", "haetae.judge", "GateResult", "CompositeGate"):
        assert forbidden not in src, f"decomp_critic이 {forbidden} 참조(분리 위반)"


def test_synthesizer_prompt_has_single_responsibility_disjoint_scope():
    """합성기 분해 프롬프트가 단일-책임 + disjoint-scope 분해 지침을 담는다(WO#148)."""
    syn = (PROMPT_DIR / "synthesizer.md").read_text(encoding="utf-8")
    assert "단일-책임" in syn
    assert "disjoint" in syn.lower()


def test_synthesizer_single_responsibility_guidance_is_concise():
    """WO#150(B): #148 단일-책임/disjoint-scope 지침은 *의도를 보존*하되 *간결*하다 —
    6-콜 합성을 유발한 장황한 #147 서사(이동+성장+충돌+먹이+점수 긴 나열)는 제거하고,
    핵심 의도(단일-책임·distinct 모듈·disjoint·통합 유닛·≥4 과대·하위측면 과분할 금지)는 유지."""
    syn = (PROMPT_DIR / "synthesizer.md").read_text(encoding="utf-8")
    # 의도 보존 (분해 SPLIT 동작 #149 확증 — 길이만 다이어트):
    assert "단일-책임" in syn and "disjoint" in syn.lower()
    assert "통합" in syn or "integration" in syn.lower()  # 조립/통합 유닛 wire
    assert "과대" in syn or "4" in syn  # ≥4 독립 행동 = 과대 → 쪼개라
    assert "하위측면" in syn or "과분할" in syn  # 한 책임의 하위측면은 과분할 금지(over-split guard)
    # 간결화: 장황한 #147 행동 나열 서사는 제거됨(토큰↓).
    assert "이동+성장+충돌+먹이+점수" not in syn


def test_decomp_critic_prompt_has_granularity_axis():
    """decomp-critic 프롬프트가 입도/책임 수 축(과대-다행동 유닛 weak)을 담는다(WO#148)."""
    dc = DECOMP_PROMPT.read_text(encoding="utf-8")
    assert "입도" in dc and "단일-책임" in dc
