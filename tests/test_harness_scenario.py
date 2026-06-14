"""WO#98 — 하니스 시나리오 계약(scenario_steps) 테스트 (mock, codex/네트워크 없음).

#92가 드러낸 하니스 사슬의 마지막 프런티어: 필드(#78)는 다 emit됐는데 그 필드를 채우는
*시나리오 절차*가 부실해 거짓 음성 증거가 난 케이스.
  - ac3: DnD 시나리오가 *같은 카드*를 todo→doing→done 안 옮김(부분만).
  - ac5: persistence 시나리오가 reload *前* 카드를 삭제 → "안 남음" 거짓 음성.
run-judge는 정확히 fail(증거상 행동 없음 — anti-erosion 작동)했으나, 틀린 건 *앱*이 아니라
*하니스 시나리오*. 수정: #78 evidence_fields의 시나리오판으로 `scenario_steps`(하니스가 밟아야 할
완전한 흐름)를 합성기가 criteria서 *파생*해 명시 + verification-harness 스킬이 올바른 시나리오
구성을 유도. **빌더-측 유도만** — 적대 run-judge·게이트(#82-B)·바 불변(criteria 파생, 완화 아님).
"""

from pathlib import Path

from haetae.intake import extract_scenario_steps, harness_scenario_steps
from haetae.llm import MockClient
from haetae.loop import MockGate, run_loop
from haetae.models import CheckType, NextOrder, ProjectSpec, Verdict
from haetae.skills import inject_skills, load_skills, match_skills

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPT_DIR = REPO_ROOT / "prompts"
SYNTH_PROMPT = REPO_ROOT / "prompts" / "synthesizer.md"
SKILLS_DIR = REPO_ROOT / "skills"
JUDGE_PROMPT = REPO_ROOT / "prompts" / "judge.md"
RUN_JUDGE_PROMPT = REPO_ROOT / "prompts" / "run_judge.md"

# #92류 칸반 DnD 흐름의 올바른 STEP(같은 카드를 전 상태로).
_DND_STEPS = [
    "todo에 카드 생성",
    "*같은 카드*를 todo→doing 이동+위치확인",
    "doing→done 이동+위치확인",
    "각 전이 후 컬럼 멤버십 기록",
]
# #92류 persistence 흐름의 올바른 STEP(검사 前 보존).
_PERSIST_STEPS = [
    "항목 생성",
    "reload",
    "생성한 *그* 항목 존재 확인 — reload 前 변형/삭제 금지",
]


def _spec(acs: list[dict], decomp: list[dict]) -> ProjectSpec:
    return ProjectSpec.model_validate({
        "spec_id": "sc-001", "version": 1, "order_raw": "x", "goal": "g",
        "task_type": "feature_impl", "verifiability": "objective", "mode": "normal",
        "acceptance_criteria": acs, "non_goals": ["n"], "done_when": "전부 통과",
        "decomposition": decomp,
    })


# ════════════════════ 1. 추출 (criteria → 하니스 유닛 매핑) ════════════════════


def test_extract_scenario_steps_union_order_preserving():
    """run 기준들의 scenario_steps를 union(순서보존·중복제거) — 흐름 순서는 의미라 정렬하지 않는다."""
    spec = _spec(
        [
            {"id": "ac3", "desc": "DnD", "check": {"type": "run", "cmd": "trace"},
             "scenario_steps": _DND_STEPS},
            {"id": "ac5", "desc": "persistence", "check": {"type": "run", "cmd": "trace"},
             "scenario_steps": _PERSIST_STEPS + ["todo에 카드 생성"]},  # 마지막은 중복(ac3과)
        ],
        [{"unit": "u1", "desc": "엔진"}, {"unit": "u2", "desc": "헤드리스 trace 하니스"}],
    )
    # 선언 순서 보존 + 중복("todo에 카드 생성")은 1회만.
    assert extract_scenario_steps(spec) == _DND_STEPS + _PERSIST_STEPS


def test_harness_scenario_steps_maps_to_harness_unit_only():
    """추출된 흐름은 *트레이스 하니스* 유닛(desc 키워드)에만 매핑 — 비하니스 유닛은 미포함."""
    spec = _spec(
        [{"id": "ac3", "desc": "DnD", "check": {"type": "run", "cmd": "trace"},
          "scenario_steps": _DND_STEPS}],
        [{"unit": "u1", "desc": "순수 store reducer"}, {"unit": "u2", "desc": "헤드리스 trace 하니스"}],
    )
    mapping = harness_scenario_steps(spec)
    assert mapping == {"u2": _DND_STEPS}  # 하니스 유닛만, 비하니스(u1) 없음


def test_harness_scenario_steps_graceful_when_no_steps():
    """scenario_steps 없는 기존 spec → 빈 매핑(무유도·기존 동작, back-compat)."""
    spec = _spec(
        [{"id": "ac1", "desc": "DnD", "check": {"type": "run", "cmd": "trace"}}],  # scenario_steps 없음
        [{"unit": "u1", "desc": "헤드리스 trace 하니스"}],
    )
    assert harness_scenario_steps(spec) == {}


def test_harness_scenario_steps_graceful_when_no_harness_unit():
    """run 기준에 흐름이 있어도 트레이스-하니스 유닛이 없으면 → 부착 대상 없음(빈 매핑)."""
    spec = _spec(
        [{"id": "ac3", "desc": "DnD", "check": {"type": "run", "cmd": "trace"},
          "scenario_steps": _DND_STEPS}],
        [{"unit": "u1", "desc": "물리 엔진"}, {"unit": "u2", "desc": "Canvas UI 렌더링"}],
    )
    assert harness_scenario_steps(spec) == {}


def test_harness_scenario_steps_only_from_run_criteria():
    """build/test 기준의 scenario_steps는 무시 — run 기준의 흐름만 추출(트레이스 증거 전용)."""
    spec = _spec(
        [{"id": "ac1", "desc": "빌드", "check": {"type": "build", "cmd": "npm run build"},
          "scenario_steps": ["빌드만"]}],
        [{"unit": "u1", "desc": "헤드리스 trace 하니스"}],
    )
    assert extract_scenario_steps(spec) == []
    assert harness_scenario_steps(spec) == {}


def test_harness_scenario_steps_pure_does_not_mutate_bar():
    """anti-erosion: 매핑은 criteria서 *파생*만 — spec(criteria/done_when/goal 등)을 안 바꾼다."""
    spec = _spec(
        [{"id": "ac3", "desc": "DnD", "check": {"type": "run", "cmd": "trace"},
          "scenario_steps": _DND_STEPS}],
        [{"unit": "u1", "desc": "trace 하니스"}],
    )
    before = spec.model_dump()
    harness_scenario_steps(spec)
    assert spec.model_dump() == before  # 순수 — 바 한 글자도 안 바뀜


# ════════════════════ 2. 합성기 프롬프트 유도 ════════════════════


def test_synthesizer_steers_scenario_steps():
    """합성기가 run/행동 기준에 scenario_steps(완전 흐름·step→field 연결)를 명시하도록 유도한다."""
    src = SYNTH_PROMPT.read_text(encoding="utf-8")
    assert "scenario_steps" in src
    # 완전 흐름·같은 엔티티·검사 前 보존·step→field
    assert "완전한 흐름" in src or "완전 흐름" in src
    assert "같은 엔티티" in src
    assert "step→field" in src or "STEP→field" in src
    # #92 류 예시(DnD 같은 카드 · persistence reload 前 보존)
    assert "같은 카드" in src
    assert "reload 前" in src or "reload 전" in src


def test_synthesizer_scenario_steps_is_builder_steer_not_bar():
    """무회귀+불변: scenario_steps는 *무엇을 구동할지* 유도지 판정 완화가 아니며, run-judge 채점은 그대로."""
    src = SYNTH_PROMPT.read_text(encoding="utf-8")
    assert "evidence_fields" in src                       # #82-A 보존
    # 판정은 여전히 독립 run-judge(완화 아님 — 바 불변)
    assert "run-judge가 한다" in src or "run-judge가 그런" in src


# ════════════════════ 3. verification-harness 스킬 — 흔한 시나리오 실수 ════════════════════


def test_skill_teaches_complete_flow_same_entity():
    """스킬에 '완전 흐름 구동·같은 엔티티 전 상태' 패턴이 있다(#92 ac3)."""
    skills = {s.name: s for s in load_skills(SKILLS_DIR)}
    body = skills["verification-harness"].body
    assert "완전 흐름" in body or "완전한" in body
    assert "같은 엔티티" in body
    assert "부분" in body  # 부분만 구동 금지


def test_skill_teaches_preserve_before_persistence_check():
    """스킬에 'persistence/reload 검사 前 상태 보존' 패턴이 있다(#92 ac5)."""
    skills = {s.name: s for s in load_skills(SKILLS_DIR)}
    body = skills["verification-harness"].body
    assert "reload" in body
    assert "前" in body  # 검사 前 변형/삭제 금지
    assert "persistence" in body or "잔존" in body


def test_skill_teaches_realistic_order():
    """스킬에 '현실적 순서: 생성→조작→검증' 패턴이 있다."""
    skills = {s.name: s for s in load_skills(SKILLS_DIR)}
    body = skills["verification-harness"].body
    assert "생성→조작→검증" in body or ("생성" in body and "조작" in body and "검증" in body)


def test_skill_scenario_keeps_existing_patterns():
    """무회귀: 새 시나리오 섹션이 기존 패턴(로직-렌더 분리·stdout 위생·자가채점 금지)을 깨지 않는다."""
    skills = {s.name: s for s in load_skills(SKILLS_DIR)}
    body = skills["verification-harness"].body
    assert "로직-렌더 분리" in body or "분리" in body
    assert "stdout 위생" in body
    assert "자가채점" in body


# ════════════════════ 4. 빌더 주입 (run_loop 통합 — apply_builder 채널) ════════════════════


_INJ_SPEC = """\
spec_id: sc-inj-001
version: 1
order_raw: "x"
goal: "칸반 보드"
task_type: feature_impl
verifiability: objective
mode: normal
constraints: []
acceptance_criteria:
  - id: ac3
    desc: "DnD로 카드 컬럼 이동"
    check: { type: run, cmd: "trace" }
    scenario_steps:
      - "todo에 카드 생성"
      - "*같은 카드*를 todo→doing 이동+위치확인"
      - "doing→done 이동+위치확인"
non_goals: ["n"]
done_when: "ac3"
decomposition:
  - { unit: u1, desc: "헤드리스 trace 하니스" }
open_questions: []
"""

_NEXT_U1 = (
    "verdict: pass\naction: next_order\nrationale: build\n"
    "next_order:\n  unit: u1\n  goal: \"u1 구현\"\n  deliverable: \"요약\"\n"
)
_STOP = "verdict: done\naction: stop\nrationale: done\n"


class _CapturingExec:
    """executor가 받은 work order를 기록(주입 검증용)."""

    def __init__(self):
        self.orders: list[NextOrder] = []

    def run(self, order: NextOrder) -> str:
        self.orders.append(order)
        return f"{order.unit} done"


def test_builder_order_gets_scenario_injection():
    """run_loop 통합: 하니스 유닛 work order의 scope에 시나리오 흐름이 *주입*된다(빌더 전용)."""
    ex = _CapturingExec()
    state = run_loop(
        order="x", client=MockClient([_INJ_SPEC, _NEXT_U1, _STOP]),
        executor=ex, gate=MockGate([Verdict.pass_, Verdict.done]), prompt_dir=PROMPT_DIR,
    )
    assert state.status.value == "done"
    assert ex.orders, "executor가 work order를 받아야"
    o = ex.orders[0]
    assert o.unit == "u1"
    assert "검증 시나리오 흐름" in (o.scope or "")     # 주입 섹션
    assert "*같은 카드*를 todo→doing 이동+위치확인" in o.scope  # STEP 명시
    assert "doing→done 이동+위치확인" in o.scope


def test_builder_scenario_injection_does_not_change_bar():
    """주입은 빌더 order(scope)에만 — spec의 바(criteria/done_when)는 불변(완화 0)."""
    ex = _CapturingExec()
    run_loop(
        order="x", client=MockClient([_INJ_SPEC, _NEXT_U1, _STOP]),
        executor=ex, gate=MockGate([Verdict.pass_, Verdict.done]), prompt_dir=PROMPT_DIR,
    )
    o = ex.orders[0]
    # 주입 섹션은 *판정 완화가 아니라 무엇을 구동할지* 유도임을 명시(바 불변)하고, run-judge 채점 보존.
    assert "판정 완화가 아니" in o.scope or "통과/실패 판정" in o.scope
    assert "run-judge" in o.scope


def test_non_harness_unit_order_has_no_scenario_injection():
    """비하니스 유닛 work order엔 시나리오 주입이 없다(무유도 → 기존 동작)."""
    spec_yaml = _INJ_SPEC.replace("헤드리스 trace 하니스", "순수 store reducer 구현")
    ex = _CapturingExec()
    run_loop(
        order="x", client=MockClient([spec_yaml, _NEXT_U1, _STOP]),
        executor=ex, gate=MockGate([Verdict.pass_, Verdict.done]), prompt_dir=PROMPT_DIR,
    )
    assert ex.orders
    assert "검증 시나리오 흐름" not in (ex.orders[0].scope or "")


def test_back_compat_no_scenario_steps_no_injection():
    """back-compat: scenario_steps 없는 기존 spec → 주입 없음(추가형·무영향)."""
    spec_yaml = _INJ_SPEC.replace(
        '    scenario_steps:\n'
        '      - "todo에 카드 생성"\n'
        '      - "*같은 카드*를 todo→doing 이동+위치확인"\n'
        '      - "doing→done 이동+위치확인"\n',
        "",
    )
    ex = _CapturingExec()
    run_loop(
        order="x", client=MockClient([spec_yaml, _NEXT_U1, _STOP]),
        executor=ex, gate=MockGate([Verdict.pass_, Verdict.done]), prompt_dir=PROMPT_DIR,
    )
    assert ex.orders
    assert "검증 시나리오 흐름" not in (ex.orders[0].scope or "")


# ════════════════════ 5. 분리 / 빌더-측 전용 (judge/run-judge 무주입) ════════════════════


def test_scenario_injection_is_builder_side_only_not_in_judge():
    """적대 게이트(run-judge)는 시나리오 주입을 안 받는다 — 주입은 executor 복사본에만(분리).

    게이트는 원본 spec.acceptance_criteria만 본다. scenario_steps는 criteria의 추가형 슬롯일 뿐
    *주입 섹션 텍스트*("검증 시나리오 흐름")는 빌더 order에만 붙으므로, 게이트로 가는 spec엔 없다.
    """
    from haetae.gate import CompositeGate

    spec = _spec(
        [{"id": "ac3", "desc": "DnD trace", "unit": "u1",
          "check": {"type": "run", "cmd": "echo '{}'", "pass": "ok"},
          "scenario_steps": _DND_STEPS}],
        [{"unit": "u1", "desc": "헤드리스 trace 하니스"}],
    )
    client = MockClient("verdicts:\n  - ac_id: ac3\n    status: pass\n    reason: ok\n")
    gate = CompositeGate(
        workdir=REPO_ROOT, judge_client=client,
        judge_prompt_path=JUDGE_PROMPT, run_judge_prompt_path=RUN_JUDGE_PROMPT,
        run_timeout=10, install_deps=False,
    )
    gate.judge("결과", spec, unit="u1")
    assert client.calls
    joined = client.calls[0]["system"] + client.calls[0]["user"]
    # 빌더 전용 주입 섹션 헤더가 적대 게이트 입력엔 없어야 한다(분리).
    assert "검증 시나리오 흐름" not in joined


def test_scenario_steps_is_additive_slot_like_evidence_fields():
    """scenario_steps는 evidence_fields와 동형의 추가형 슬롯 — 기존 검증 기준 검사에 무영향."""
    from haetae.models import AcceptanceCriterion, Check

    # 슬롯 미지정이면 기본 빈 리스트(추가형·비파괴).
    ac = AcceptanceCriterion(id="ac1", desc="d", check=Check(type=CheckType.run, cmd="x"))
    assert ac.scenario_steps == []
    assert ac.evidence_fields == []  # 동형 동작 확인
