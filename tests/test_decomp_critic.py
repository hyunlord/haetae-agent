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


def test_granularity_signal_flags_distinct_kind_responsibilities():
    """WO#152: ≥4 행동 임계 *미만*이라도 서로 다른 *종류*의 검증가능 책임(판정 + 상태전이)을
    한 유닛에 묶으면 분할 권고 — #151 u3(충돌 판정 + game-over 상태고정)가 escalate한 케이스.
    이 케이스는 clause<4라 기존 ≥4-count로는 안 걸리고 distinct-KIND 기준으로 걸려야 한다."""
    order = NextOrder(
        unit="u3",
        goal="collision.js: 벽/자기몸 충돌 판정 및 game over 이후 상태 고정",
        deliverable="collision.js + test",
    )
    # 기존 ≥4-count로는 안 잡히는 입력임을 명시(distinct-KIND가 트리거 — 회귀 격리)
    from haetae.decomp_critic import _behavior_clauses, _OVER_LARGE_THRESHOLD
    assert len(_behavior_clauses(order.goal)) < _OVER_LARGE_THRESHOLD
    sig = granularity_signal(order)
    assert sig is not None
    assert "단일-책임" in sig and "disjoint" in sig.lower()
    assert "종류" in sig or "distinct" in sig.lower()  # distinct-KIND 근거 노출


def test_granularity_signal_flags_distinct_kind_render_plus_input():
    """WO#152 일반성(충돌+상태 외): 렌더 + 입력처럼 다른 종류 책임 묶음도 분할 권고."""
    order = NextOrder(unit="u4",
                      goal="canvas 렌더링과 키보드 입력 처리를 함께 구현",
                      deliverable="ui.js")
    assert granularity_signal(order) is not None


def test_granularity_signal_same_kind_subaspects_not_oversplit():
    """WO#152 과분할 가드: 같은 종류 하위측면(벽 충돌 + 자기몸 충돌 + 겹침 = 셋 다 detection)은
    1 KIND → 분할 안 함(과분할 금지). distinct-KIND 기준이 같은-종류를 묶는지 확인."""
    assert granularity_signal(
        NextOrder(unit="u3", goal="collision.js: 벽 충돌, 자기몸 충돌, 머리-몸통 겹침 판정",
                  deliverable="collision.js")
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


def test_synthesizer_prompt_has_distinct_kind_split_criterion():
    """WO#152: 합성기가 *서로 다른 종류*의 책임(판정/상태전이/렌더/입력) 분할 기준을 담되,
    같은-종류 하위측면 과분할 금지 가드는 유지(게임오버 상태 ≠ 충돌 판정 = distinct → 분할)."""
    syn = (PROMPT_DIR / "synthesizer.md").read_text(encoding="utf-8")
    assert "종류" in syn  # distinct-KIND 분할 기준(행동 수만이 아니라 책임의 종류)
    assert "하위측면" in syn or "과분할" in syn  # 같은-종류 묶음은 과분할 금지(가드 유지)


def test_decomp_critic_prompt_has_distinct_kind_axis():
    """WO#152: decomp-critic 프롬프트가 distinct-종류 책임 분할 축을 담는다(판정 vs 상태전이 등)."""
    dc = DECOMP_PROMPT.read_text(encoding="utf-8")
    assert "종류" in dc


def test_decomposition_prompts_stay_concise():
    """WO#152: distinct-KIND 정밀화를 #150-B 간결 길이 안에서 — 프롬프트 길이 회귀 모니터.
    (서사 없이 기준만 추가; 합성 콜 수 회귀 0 목표의 프록시.)
    WO#155: end-to-end 검증의 구조적 분리(wire | 트레이스-하니스 §1e) + 풀-행동 사슬 scenario
    예시는 통합 floor의 핵심 신규 지침이라 ceiling을 소폭(350→365) 올린다 — 여전히 *기준만*(서사
    없이), 합성 콜 회귀 0 의도 보존."""
    syn_lines = (PROMPT_DIR / "synthesizer.md").read_text(encoding="utf-8").count("\n")
    dc_lines = DECOMP_PROMPT.read_text(encoding="utf-8").count("\n")
    # #152 직전: synthesizer 343, decomp_critic 73. #155: wire|트레이스-하니스 분리 + 풀-사슬 예시.
    assert syn_lines <= 365, f"synthesizer.md 길이 회귀({syn_lines}>365) — 간결 위반"
    assert dc_lines <= 80, f"decomp_critic.md 길이 회귀({dc_lines}>80)"


# ──────────────── 통합-급 구조적 재분해 (WO#155) ────────────────
#
# #153: 통합 유닛이 over-bundled(엔진-파사드 + 브라우저-어댑터 + 트레이스-재구성 = 3 distinct
# KIND) + 풀-행동 트레이스가 전체 사슬 미실증 → run-judge 정직 거부. director-side 보정: 통합
# 유닛이 조립에 *더해* 트레이스-하니스 KIND까지 겸하면 granularity_signal이 *구조적 재분해*
# (유닛 추가 — wire | 트레이스-하니스 분리) 신호를 낸다. 순수 조립은 면제 유지(과직렬화 회피).


def test_granularity_signal_flags_integration_bundling_trace_harness():
    """WO#155: 통합 유닛이 조립(wire)에 *더해* 트레이스-하니스(행동 사슬 구동+evidence)까지 겸하면
    구조적 재분해(유닛 추가) 권고 — in-place 축소 아님(#153 over-bundle 직격)."""
    order = NextOrder(
        unit="u9",
        goal="engine.js가 모듈을 wire하고 헤드리스 트레이스 하니스로 전체 행동 사슬을 구동해 evidence emit",
        deliverable="engine.js + trace.ts",
    )
    sig = granularity_signal(order)
    assert sig is not None
    assert "구조적 재분해" in sig          # in-place 축소 아닌 유닛 추가
    assert "트레이스-하니스" in sig and "wire" in sig.lower()
    assert "deps" in sig.lower()           # 트레이스-하니스가 wire에 의존


def test_granularity_signal_pure_wire_integration_still_exempt():
    """WO#155 가드: 순수 조립(트레이스-하니스 미겸)만 하는 통합 유닛은 *여전히 면제*(None) —
    구조적 재분해 신호가 순수 wire를 오탐하지 않는다(과직렬화 회피, #51/#123)."""
    # 트레이스/하니스 어휘 없는 순수 wire — 모듈명(collision/gameover)을 *언급*해도 면제.
    assert granularity_signal(
        NextOrder(unit="u9", goal="engine.js가 move, collision, food, gameover 모듈을 조립/wire",
                  deliverable="engine.js")
    ) is None


def test_granularity_signal_pure_trace_harness_unit_no_signal():
    """WO#155: 이미 *분리된* 전용 트레이스-하니스 유닛(wire 마커 없음)엔 신호 없음 — 자체 유닛이라
    더 쪼갤 것 없음(구조적 재분해는 wire+트레이스 *겸함*에만)."""
    assert granularity_signal(
        NextOrder(unit="u8", goal="헤드리스 trace 하니스로 풀-행동 사슬 트레이스 emit", deliverable="trace.ts")
    ) is None


def test_granularity_signal_structural_wired_into_critic_prompt():
    """WO#155: 통합+트레이스 over-bundle 신호가 critic(codex) user 프롬프트에 *참고*로 주입된다."""
    over = NextOrder(unit="u9",
                     goal="모듈을 wire하고 헤드리스 트레이스 하니스로 전체 행동을 구동해 evidence emit",
                     deliverable="engine.js")
    client = MockClient([_WEAK_YAML])
    critique_decomposition(over, _spec(), _state(), client, prompt_path=DECOMP_PROMPT)
    user = client.calls[0]["user"]
    assert "입도 신호" in user and "구조적 재분해" in user


def test_build_decomp_feedback_guides_integration_trace_harness_split():
    """WO#155: weak 피드백이 통합 트레이스-하니스 겸함 → wire | 전용 트레이스-하니스 분리(유닛 추가)를 유도."""
    fb = build_decomp_feedback(DecompCritique(verdict="weak", reason="통합이 조립+트레이스 겸함", unit="u9"))
    assert "트레이스-하니스" in fb
    assert "전용" in fb or "유닛을 추가" in fb


def test_synthesizer_prompt_has_wire_trace_harness_split():
    """WO#155: 합성기가 end-to-end 검증을 wire/파사드 유닛 | 전용 트레이스-하니스 유닛(wire에 deps)으로
    분리하도록 유도한다(distinct KIND·DAG)."""
    syn = (PROMPT_DIR / "synthesizer.md").read_text(encoding="utf-8")
    assert "트레이스-하니스" in syn
    assert "wire" in syn.lower() and "파사드" in syn
    assert "deps" in syn.lower()  # 트레이스-하니스가 wire에 의존(DAG)


def test_decomp_critic_prompt_has_integration_structural_axis():
    """WO#155: decomp-critic 프롬프트가 통합-급 구조적 재분해 축(조립+트레이스-하니스 겸함 → 유닛 추가)을 담는다."""
    dc = DECOMP_PROMPT.read_text(encoding="utf-8")
    assert "구조적 재분해" in dc
    assert "트레이스-하니스" in dc
