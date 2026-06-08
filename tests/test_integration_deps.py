"""WO#51 — 통합 유닛 deps 추론 넛지 테스트 (mock LLM만, 네트워크 없음).

머지 충돌 근본 차단: 통합 성격 유닛(대시보드·진입점·e2e·트레이스)이 *자기가 엮는 빌더
유닛들에 의존(deps)* 하도록 보수적 휴리스틱으로 탐지 → 과소 지정이면 #31 재합성 피드백
채널로 바운드 1회 교정 → **deps만** 채택(criteria/done_when 불변). 스케줄러는 무변경.
"""

from pathlib import Path

import pytest

from haetae.intake import (
    SynthesisError,
    _adopt_deps_only,
    _integration_dep_gaps,
    _is_integration_unit,
    integration_dep_feedback,
    nudge_integration_deps,
)
from haetae.llm import MockClient
from haetae.models import (
    AcceptanceCriterion,
    Check,
    DecompositionUnit,
    Mode,
    ProjectSpec,
    TaskType,
    Verifiability,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPT_PATH = REPO_ROOT / "prompts" / "synthesizer.md"


def _spec(
    decomp: list[tuple[str, str, list[str]]],
    *,
    criteria: list[AcceptanceCriterion] | None = None,
    done_when: str = "모든 ac 통과 AND 무회귀",
    goal: str = "통합 시스템을 만든다",
) -> ProjectSpec:
    """(unit, desc, deps) 튜플 리스트로 ProjectSpec 생성(테스트 헬퍼)."""
    return ProjectSpec(
        spec_id="t-001", version=1, order_raw="주문", goal=goal,
        task_type=TaskType.feature_impl, verifiability=Verifiability.objective, mode=Mode.normal,
        acceptance_criteria=criteria or [
            AcceptanceCriterion(id="ac1", desc="기준", check=Check(type="test", cmd="pytest")),
        ],
        non_goals=["ng1", "ng2"], done_when=done_when,
        decomposition=[DecompositionUnit(unit=u, desc=d, deps=list(dp)) for u, d, dp in decomp],
    )


# ──────────────────── 휴리스틱 탐지(보수적) ────────────────────


@pytest.mark.parametrize("desc", [
    "통합 대시보드: 모든 유닛을 화면에 wire", "real-time dashboard", "e2e 통합 테스트",
    "앱 진입점(entrypoint) 구성", "headless sim:trace 진입점", "실제 엔진을 import해 트레이스",
])
def test_is_integration_unit_true(desc):
    assert _is_integration_unit(desc) is True


@pytest.mark.parametrize("desc", [
    "폼 컴포넌트", "데미지 로직", "저장 모델과 JSON 입출력", "a", "물리 엔진 큐", "",
])
def test_is_integration_unit_false_for_plain_builders(desc):
    # 보수적: 통합 키워드 없는 순수 빌더는 통합으로 안 본다(과직렬화·오탐 회피).
    assert _is_integration_unit(desc) is False


def test_gaps_flags_underspecified_integration():
    """통합 유닛이 비통합 빌더 다수(≥2)를 deps에서 빠뜨리면 gap으로 잡힘."""
    spec = _spec([
        ("u1", "엔진 코어", []),
        ("u2", "데미지 로직", ["u1"]),
        ("u3", "재고 모델", []),
        ("u4", "큐 시스템", []),
        ("u5", "통합 대시보드 — 유닛들을 wire", ["u1"]),  # u2·u3·u4 빠뜨림
    ])
    gaps = _integration_dep_gaps(spec)
    assert set(gaps) == {"u5"}
    assert set(gaps["u5"]) == {"u2", "u3", "u4"}  # 빠뜨린 빌더들


def test_gaps_none_when_deps_adequate():
    """통합 유닛이 엮는 빌더들에 이미 의존하면 gap 없음(no-op 대상)."""
    spec = _spec([
        ("u1", "엔진 코어", []),
        ("u2", "데미지 로직", ["u1"]),
        ("u3", "재고 모델", []),
        ("u5", "통합 대시보드", ["u1", "u2", "u3"]),  # 모든 빌더에 의존
    ])
    assert _integration_dep_gaps(spec) == {}
    assert integration_dep_feedback(spec) is None


def test_gaps_none_when_no_integration_unit():
    """오탐 방지: 통합 성격 유닛이 없으면 deps가 성겨도 넛지 안 함(병렬성 보존)."""
    spec = _spec([
        ("u1", "엔진 코어", []),
        ("u2", "데미지 로직", []),
        ("u3", "재고 모델", []),
    ])
    assert _integration_dep_gaps(spec) == {}
    assert integration_dep_feedback(spec) is None


def test_gaps_none_when_single_missing_builder():
    """보수적 임계: 빠뜨린 빌더가 1개뿐이면 넛지 안 함(의도일 수 있음)."""
    spec = _spec([
        ("u1", "엔진 코어", []),
        ("u2", "데미지 로직", []),
        ("u5", "통합 대시보드", ["u1"]),  # u2 하나만 빠뜨림 → 넛지 안 함
    ])
    assert _integration_dep_gaps(spec) == {}


def test_does_not_overserialize_builders():
    """과직렬화 금지: 비통합 빌더끼리는 절대 gap(의존)을 만들지 않는다."""
    spec = _spec([
        ("u1", "엔진 코어", []),
        ("u2", "데미지 로직", []),
        ("u3", "재고 모델", []),
        ("u5", "통합 대시보드", ["u1", "u2", "u3"]),
    ])
    gaps = _integration_dep_gaps(spec)
    # 빌더(u1·u2·u3)는 gap 키에 절대 없음 — 통합 유닛만 대상.
    assert "u1" not in gaps and "u2" not in gaps and "u3" not in gaps


def test_feedback_text_names_unit_and_missing():
    spec = _spec([
        ("u1", "코어", []), ("u2", "로직", []), ("u3", "모델", []),
        ("u5", "통합 대시보드", []),
    ])
    fb = integration_dep_feedback(spec)
    assert fb is not None
    assert "u5" in fb and "deps" in fb
    assert "엮는" in fb  # 의존을 더하라는 넛지 문구


# ──────────────────── deps-only splice (criteria 불변 가드) ────────────────────


def test_adopt_deps_only_changes_deps_keeps_protected():
    """재합성 결과에서 deps만 채택 — criteria/done_when/goal은 원본 유지(governance)."""
    original = _spec(
        [("u1", "코어", []), ("u2", "로직", []), ("u5", "통합 대시보드", [])],
        done_when="원본 done_when", goal="원본 goal",
    )
    # 재합성 LLM이 deps도 고치고 *criteria/done_when/goal도 멋대로 바꿈*.
    restructured = _spec(
        [("u1", "코어", []), ("u2", "로직", []), ("u5", "통합 대시보드", ["u1", "u2"])],
        criteria=[AcceptanceCriterion(id="acX", desc="다른 기준", check=Check(type="lint"))],
        done_when="바뀐 done_when", goal="바뀐 goal",
    )
    merged = _adopt_deps_only(original, restructured)
    # deps는 재합성 채택
    deps = {u.unit: u.deps for u in merged.decomposition}
    assert deps["u5"] == ["u1", "u2"]
    # protected 필드는 전부 원본 그대로(불변 가드)
    assert merged.done_when == "원본 done_when"
    assert merged.goal == "원본 goal"
    assert [a.id for a in merged.acceptance_criteria] == ["ac1"]


def test_adopt_deps_only_rejects_structural_change():
    """unit 집합이 달라지면(분해 구조 변형) deps-only splice 불가 → 보수적으로 원본 그대로."""
    original = _spec([("u1", "코어", []), ("u5", "통합 대시보드", [])])
    restructured = _spec([("u1", "코어", []), ("u9", "새 유닛", []), ("u5", "통합", ["u1", "u9"])])
    merged = _adopt_deps_only(original, restructured)
    assert merged is original  # 구조 바뀜 → 원본 반환


# ──────────────────── nudge_integration_deps (bounded·best-effort) ────────────────────


def _spec_yaml(u5_deps: str, *, done_when: str = "원본 done_when") -> str:
    """재합성 LLM이 낸다고 가정하는 유효 ProjectSpec YAML(같은 unit 집합, u5 deps 교정)."""
    return f"""\
spec_id: t-001
version: 1
order_raw: "주문"
goal: "원본 goal"
task_type: feature_impl
verifiability: objective
mode: normal
constraints: []
acceptance_criteria:
  - id: ac1
    desc: "기준"
    check: {{ type: test, cmd: "pytest" }}
assumptions: []
non_goals: ["ng1", "ng2"]
done_when: "{done_when}"
decomposition:
  - {{ unit: u1, desc: "코어", deps: [] }}
  - {{ unit: u2, desc: "로직", deps: [] }}
  - {{ unit: u3, desc: "모델", deps: [] }}
  - {{ unit: u5, desc: "통합 대시보드", deps: {u5_deps} }}
open_questions: []
"""


def _underspec() -> ProjectSpec:
    return _spec(
        [("u1", "코어", []), ("u2", "로직", []), ("u3", "모델", []), ("u5", "통합 대시보드", [])],
        done_when="원본 done_when", goal="원본 goal",
    )


def test_nudge_noop_when_adequate_no_extra_call():
    """deps 충분 → 재합성 호출 0회(기존 동작·비용 불변)."""
    spec = _spec([
        ("u1", "코어", []), ("u2", "로직", []), ("u3", "모델", []),
        ("u5", "통합 대시보드", ["u1", "u2", "u3"]),
    ])
    client = MockClient("호출되면 안 됨")
    out = nudge_integration_deps("주문", spec, client, prompt_path=PROMPT_PATH)
    assert out is spec
    assert len(client.calls) == 0  # 재합성 안 함


def test_nudge_resynthesizes_once_and_adopts_deps():
    """과소 지정 → 정확히 1회 재합성, u5 deps가 엮는 빌더들로 교정됨(deps ⊇ 빌더)."""
    spec = _underspec()
    client = MockClient([_spec_yaml("[u1, u2, u3]")])  # 재합성 1개만 준비(소진 시 예외=초과호출 검출)
    out = nudge_integration_deps("주문", spec, client, prompt_path=PROMPT_PATH)
    assert len(client.calls) == 1  # bounded: 정확히 1회
    deps = {u.unit: u.deps for u in out.decomposition}
    assert set(deps["u5"]) >= {"u1", "u2", "u3"}  # 엮는 빌더에 의존
    # 재합성 피드백 텍스트가 user 메시지에 실렸는지(=#31 채널 사용)
    assert "엮는" in client.calls[0]["user"] and "u5" in client.calls[0]["user"]


def test_nudge_preserves_criteria_done_when_invariant():
    """재합성 LLM이 done_when을 바꿔도 채택 안 함 — deps만 채택, criteria/done_when 불변."""
    spec = _underspec()
    client = MockClient([_spec_yaml("[u1, u2, u3]", done_when="재합성이 바꾼 done_when")])
    out = nudge_integration_deps("주문", spec, client, prompt_path=PROMPT_PATH)
    assert out.done_when == "원본 done_when"  # 불변 가드
    assert [a.id for a in out.acceptance_criteria] == ["ac1"]


def test_nudge_bounded_resynth_failure_proceeds():
    """재합성이 깨진 YAML이면 흡수 → 원본 진행(데드락/raise 없음), 단일 시도(retries=0)."""
    spec = _underspec()
    client = MockClient(["completely : broken : yaml : ["])  # 파싱 실패 유발
    out = nudge_integration_deps("주문", spec, client, prompt_path=PROMPT_PATH, synth_retries=0)
    assert out is spec  # 원본 그대로 진행
    assert len(client.calls) == 1  # 소진(1회) 후 진행 — 무한 루프 없음


def test_nudge_structural_change_falls_back_to_original():
    """재합성이 unit 집합을 바꾸면 deps-only splice 불가 → 원본 진행(보수적)."""
    spec = _underspec()
    # 재합성이 u9를 새로 만들고 u3를 없앰(구조 변형) → _adopt_deps_only가 원본 반환.
    bad_struct = """\
spec_id: t-001
version: 1
order_raw: "주문"
goal: "원본 goal"
task_type: feature_impl
verifiability: objective
mode: normal
constraints: []
acceptance_criteria:
  - id: ac1
    desc: "기준"
    check: { type: test, cmd: "pytest" }
assumptions: []
non_goals: ["ng1", "ng2"]
done_when: "원본 done_when"
decomposition:
  - { unit: u1, desc: "코어", deps: [] }
  - { unit: u2, desc: "로직", deps: [] }
  - { unit: u9, desc: "새 유닛", deps: [] }
  - { unit: u5, desc: "통합 대시보드", deps: [u1, u2, u9] }
open_questions: []
"""
    client = MockClient([bad_struct])
    out = nudge_integration_deps("주문", spec, client, prompt_path=PROMPT_PATH)
    assert out is spec  # 구조 변형 → 원본 유지


# ──────────────────── 프롬프트(Part 1) ────────────────────


def test_synthesizer_prompt_has_integration_dep_guidance():
    """synthesizer.md에 통합 유닛 deps 지시(자기가 엮는 유닛들에 의존)가 있다."""
    src = PROMPT_PATH.read_text(encoding="utf-8")
    assert "엮는 유닛들에 의존" in src or "엮는 유닛" in src
    assert "과직렬화" in src                 # 병렬성 보존 명시
    assert "머지 충돌" in src                 # 근본 차단 의도
