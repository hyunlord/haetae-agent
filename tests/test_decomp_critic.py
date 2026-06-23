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
    scope_overlap_signal,
)
from haetae.llm import MockClient
from haetae.models import (
    DecompCritique,
    DecompositionUnit,
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
    없이), 합성 콜 회귀 0 의도 보존.
    WO#157: 검증-트레이스 비-split(end-to-end) + #27 트레이스-하니스 스캐폴드 지침(synthesizer §1e +
    decomp_critic 축)은 #156 u8 floor 직격이라 ceiling 소폭 상향(syn 365→378·dc 80→92) — *기준만*.
    WO#160: 통합 facade 계약 + 런타임-smoke(synthesizer §1f + decomp_critic 1줄)은 #158 통합 floor
    (build-pass≠runtime-works) 직격이라 ceiling 소폭 상향(syn 378→383) — *기준만*(서사 없이·콜 회귀 0 의도).
    WO#162: 트레이스-하니스 헤드리스 어댑터 재사용(synthesizer JSDOM→어댑터 redirect + 단일 트레이스
    consolidation; decomp_critic 1줄)은 #161 u7/u8 트레이스 floor(약빌더가 DOM 인프라를 못 짬) 직격이라
    ceiling 소폭 상향(syn 383→388·dc 92→93) — *기준만*(서사 없이·콜 회귀 0 의도).
    WO#165: 기존 scope 정식화(배타 소유·∅ 불변식·facade 계약 결합 #160) + decomp-critic replan-time
    파일-소유권 겹침 축(#59 synthesis-time와 역할 분리)은 통합 머지-충돌→직렬화→escalate 직격이라
    ceiling 소폭 상향(syn 388→392·dc 93→102) — *기준만*(새 필드 0·서사 없이·콜 회귀 0 의도).
    WO#166: 분해 전 research 단계(pipeline-strengthening B)가 synthesizer.md에 *리서치 브리프 소비*
    지침(제안이지 mandate 아님·override 가능)을 더해 ceiling 소폭 상향(syn 392→397) — *기준만*.
    WO#172: 합성 findability 정렬(테스트 cmd는 파일/디렉토리 대상 — 약 brain의 fragile `-k` 추측이
    0개 발견 exit 5로 완주를 막던 #171 직격)이 §1에 *기준 2줄*을 더해 ceiling 소폭 상향(syn 397→400) —
    *기준만*(서사 없이·합성 콜 회귀 0 의도; 코드 align_check_findability가 하드 가드, 프롬프트는 소프트)."""
    syn_lines = (PROMPT_DIR / "synthesizer.md").read_text(encoding="utf-8").count("\n")
    dc_lines = DECOMP_PROMPT.read_text(encoding="utf-8").count("\n")
    # #152 직전: synthesizer 343, decomp_critic 73. #157: 검증-트레이스 end-to-end + 스캐폴드 지침.
    assert syn_lines <= 400, f"synthesizer.md 길이 회귀({syn_lines}>400) — 간결 위반"
    assert dc_lines <= 102, f"decomp_critic.md 길이 회귀({dc_lines}>102)"


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


# ──────────────── 검증-트레이스 = end-to-end 유닛 (WO#157, #156 u8 fix) ────────────────
#
# #156 u8: 풀-사슬 트레이스(이동·먹이·성장·점수·충돌·game-over)를 한 유닛에 담자 granularity_signal이
# #148(행동수)/#152(종류수)로 split 권고 → critic이 "이동만"으로 좁힘 → #113 풀-사슬 바가 도로
# 확장 → 약빌더 미수렴·escalate. fix: 검증 트레이스-하니스 유닛은 end-to-end → split 면제(빌드
# 모듈은 #152 KIND-split 무회귀). 빌드 분해 ≠ 검증 분해.


def test_granularity_signal_exempts_verification_trace_full_chain():
    """WO#157: 풀-사슬 검증 트레이스 유닛(트레이스 마커 O·통합 마커 X)은 여러 행동/종류를 담아도
    split 권고 안 함(None) — end-to-end가 정상(#156 u8 직격: 이전엔 #148/#152로 잘못 flag됐다)."""
    order = NextOrder(
        unit="u8",
        goal=("결정적 플레이스루 트레이스로 이동, 먹이 섭취, 성장, 점수 증가, 벽 충돌, "
              "자기몸 충돌, game over 증거를 헤드리스 트레이스로 emit"),
        deliverable="scripts/trace/full-gameplay.mjs",
    )
    assert granularity_signal(order) is None


def test_granularity_signal_build_module_still_kind_split():
    """WO#157 무회귀: 빌드 모듈(트레이스 마커 없음)은 #152 distinct-KIND split 그대로 —
    충돌 판정 + game-over 상태전이를 한 유닛에 묶으면 여전히 flag(검증-트레이스 면제와 구분)."""
    order = NextOrder(
        unit="ux",
        goal="벽/자기몸 충돌 판정과 game over 상태 전이를 한 모듈에서 처리",
        deliverable="engine/rules.js",
    )
    sig = granularity_signal(order)
    assert sig is not None
    assert "종류" in sig  # #152 distinct-KIND 신호(검증-트레이스 면제로 가려지지 않음)


def test_granularity_signal_integration_plus_trace_still_structural():
    """WO#157 무회귀: 통합(wire) + 트레이스 *겸함*은 여전히 #155 구조적 재분해(트레이스-only 면제와 다름)."""
    order = NextOrder(
        unit="u9",
        goal="모듈을 wire해 앱으로 조립하고 헤드리스 트레이스 하니스로 전체 행동을 구동해 evidence emit",
        deliverable="app.js",
    )
    sig = granularity_signal(order)
    assert sig is not None
    assert "구조적 재분해" in sig


def test_decomp_critic_prompt_has_verification_trace_end_to_end():
    """WO#157: decomp-critic 프롬프트가 '검증-트레이스 = end-to-end(행동-split 금지)' 축을 담는다."""
    dc = DECOMP_PROMPT.read_text(encoding="utf-8")
    assert "#157" in dc
    assert "end-to-end" in dc
    assert "행동별로 쪼개지 마라" in dc or "행동 부분집합" in dc


def test_decomp_critic_module_is_director_side_no_judgment():
    """WO#157 적대 분리: decomp_critic 모듈은 판정 주체(gate/run_judge/CompositeGate)·
    ALLOWED_SANDBOXES를 참조하지 않는다(director-side 계획 — 적대 gate 독립)."""
    src = (REPO_ROOT / "src" / "haetae" / "decomp_critic.py").read_text(encoding="utf-8")
    for forbidden in ("ALLOWED_SANDBOXES", "run_judge", "CompositeGate", "import gate"):
        assert forbidden not in src, forbidden


# ──────────────── 파일-소유권 disjoint 축 (WO#165, replan-time) ────────────────
#
# #165: 분해된 병렬 형제 유닛이 같은 파일을 소유(scope 겹침)하면 머지 충돌→직렬화(#21)→통합 escalate.
# intake(#59)는 synthesis-time 겹침을 1회 재합성으로 잡고, 이 축은 *replan-time* 갭을 메운다 —
# scope_overlap_signal = director-side 결정적 탐지(신호; codex가 판정·#148/#152 동형). 새 필드 0
# (기존 scope 정식화). intake/scheduler 함수 미import(둘 다 무변경·디커플) — 보수 기준 로컬 재구현.


def _spec_with_scopes(decomp: list[tuple[str, list[str], list[str]]]) -> ProjectSpec:
    """(unit, deps, scope) 튜플로 _SPEC_DICT 위에 decomposition만 갈아끼운 spec(테스트 헬퍼)."""
    d = dict(_SPEC_DICT)
    d["decomposition"] = [
        {"unit": u, "desc": f"{u} 모듈", "deps": list(dp), "scope": list(sc)}
        for u, dp, sc in decomp
    ]
    return ProjectSpec.model_validate(d)


def test_scope_overlap_signal_flags_parallel_sibling_file_overlap():
    """병렬 형제 + 양쪽 scope 선언 + 같은 파일 → 소유권 겹침 신호(disjoint 위반)."""
    spec = _spec_with_scopes([
        ("u1", [], ["src/player.js"]),
        ("u2", [], ["src/player.js"]),  # u1과 병렬(dep 없음) + 같은 파일
    ])
    sig = scope_overlap_signal(NextOrder(unit="u1", goal="플레이어"), spec)
    assert sig is not None
    assert "src/player.js" in sig and "u2" in sig
    assert "∅" in sig or "disjoint" in sig.lower()
    assert "facade" in sig.lower() and "#160" in sig  # 파일 공유 아닌 계약 결합 권고


def test_scope_overlap_signal_none_when_disjoint():
    """서로 다른 파일을 소유 → 신호 없음(과개입 0)."""
    spec = _spec_with_scopes([
        ("u1", [], ["src/player.js"]),
        ("u2", [], ["src/collision.js"]),
    ])
    assert scope_overlap_signal(NextOrder(unit="u1", goal="x"), spec) is None


def test_scope_overlap_signal_none_when_dep_linked():
    """직렬(dep) 유닛은 같은 파일 겹쳐도 신호 없음(순차 머지 — #59 _scope_overlaps와 동형 보수성)."""
    spec = _spec_with_scopes([
        ("u1", [], ["src/shared.js"]),
        ("u2", ["u1"], ["src/shared.js"]),  # u2가 u1에 의존 → 직렬
    ])
    assert scope_overlap_signal(NextOrder(unit="u2", goal="x"), spec) is None


def test_scope_overlap_signal_transitive_dep_not_flagged():
    """전이 의존(u3←u2←u1)도 직렬 → 겹쳐도 신호 없음."""
    spec = _spec_with_scopes([
        ("u1", [], ["src/x.js"]),
        ("u2", ["u1"], ["src/y.js"]),
        ("u3", ["u2"], ["src/x.js"]),  # u3는 u1에 전이 의존
    ])
    assert scope_overlap_signal(NextOrder(unit="u3", goal="x"), spec) is None


def test_scope_overlap_signal_none_when_undeclared_or_unknown_unit():
    """한쪽이라도 scope 미선언 → no-op. order 유닛이 decomposition에 없어도 None(보수적)."""
    spec = _spec_with_scopes([("u1", [], ["src/a.js"]), ("u2", [], [])])
    assert scope_overlap_signal(NextOrder(unit="u1", goal="x"), spec) is None  # u2 미선언
    assert scope_overlap_signal(NextOrder(unit="uX", goal="x"), spec) is None  # order 유닛 미상


def test_scope_overlap_signal_wired_into_critic_prompt():
    """겹침이면 소유권 신호가 critic(codex) user 프롬프트에 *참고*로 주입된다(codex가 판정)."""
    spec = _spec_with_scopes([
        ("u1", [], ["src/player.js"]),
        ("u2", [], ["src/player.js"]),
    ])
    client = MockClient([_WEAK_YAML])
    critique_decomposition(
        NextOrder(unit="u1", goal="플레이어"), spec, _state(), client, prompt_path=DECOMP_PROMPT
    )
    user = client.calls[0]["user"]
    assert "소유권 신호" in user and ("∅" in user or "disjoint" in user.lower())


def test_scope_overlap_signal_absent_for_disjoint_prompt():
    """disjoint 분해엔 소유권 신호 섹션 없음(과개입 0 — 기존 동작)."""
    spec = _spec_with_scopes([("u1", [], ["src/a.js"]), ("u2", [], ["src/b.js"])])
    client = MockClient([_PROGRESS_YAML])
    critique_decomposition(
        NextOrder(unit="u1", goal="x"), spec, _state(), client, prompt_path=DECOMP_PROMPT
    )
    assert "소유권 신호" not in client.calls[0]["user"]


def test_build_decomp_feedback_guides_disjoint_ownership():
    """weak 피드백이 형제 파일-소유권 겹침 → 공유 파일 추출/경계 재조정 + facade 계약 결합을 유도(#165)."""
    fb = build_decomp_feedback(DecompCritique(verdict="weak", reason="형제 scope 겹침", unit="u1"))
    assert "facade" in fb.lower() or "계약" in fb
    assert "∅" in fb or "한 유닛만" in fb


def test_no_owned_paths_field_scope_is_canonical():
    """WO#165-v2: 새 owned_paths 필드 없음 — 기존 scope를 정식화(소유 매니페스트)한다."""
    assert "owned_paths" not in DecompositionUnit.model_fields
    assert "scope" in DecompositionUnit.model_fields


def test_scope_docstring_formalized_strict_ownership():
    """models.py scope 필드가 엄격 소유 매니페스트로 정식화(배타 소유·∅·facade 계약 #160)."""
    src = (REPO_ROOT / "src" / "haetae" / "models.py").read_text(encoding="utf-8")
    assert "배타" in src and "∅" in src
    assert "#160" in src  # 닿는 유닛은 facade 계약으로 결합(파일 공유 아님)


def test_decomp_critic_prompt_has_ownership_overlap_axis():
    """decomp-critic 프롬프트가 replan-time 파일-소유권 겹침 축(#165)을 담는다."""
    dc = DECOMP_PROMPT.read_text(encoding="utf-8")
    assert "#165" in dc
    assert "소유권" in dc and "∅" in dc
    assert "facade" in dc.lower() and "replan-time" in dc.lower()


def test_synthesizer_prompt_formalizes_exclusive_ownership():
    """합성기가 scope를 배타 소유 + ∅ 불변식 + facade 계약 결합으로 정식화(#165)."""
    syn = (PROMPT_DIR / "synthesizer.md").read_text(encoding="utf-8")
    assert "배타" in syn and "∅" in syn
    assert "facade" in syn.lower() and "#160" in syn


def test_scope_overlap_axis_decoupled_from_intake_and_scheduler():
    """적대 분리/디커플: decomp_critic이 intake·scheduler의 scope 함수를 import·참조하지 않는다
    (둘 다 무변경 — 보수 기준 로컬 재구현). intake #59 메커니즘 미재사용."""
    src = (REPO_ROOT / "src" / "haetae" / "decomp_critic.py").read_text(encoding="utf-8")
    assert "haetae.intake" not in src and "haetae.scheduler" not in src
    assert "disjoint_scope_feedback" not in src  # intake 메커니즘 미재사용(로컬)
    assert "is_disjoint_from" not in src  # scheduler 술어 미참조
