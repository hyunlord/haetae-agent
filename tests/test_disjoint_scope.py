"""WO#59 — disjoint-scope 분해 유도 테스트 (mock LLM만, 네트워크 없음).

병렬 형제 유닛(서로 dep로 안 엮인 유닛)이 *같은 파일 scope*를 공유하면 worktree 머지 충돌을
일으킨다. #51의 형제로, 선제적 합성 경로(프롬프트+nudge)에서만 disjoint하게 유도한다:
보수적 탐지 → bounded 1회 재합성 → **decomposition만** 채택(bar 불변 가드). decomp critic 무변경.
"""

from pathlib import Path

import pytest

import yaml

from haetae.intake import (
    _adopt_scope_only,
    _are_parallel,
    _scope_overlaps,
    _transitive_deps,
    disjoint_scope_feedback,
    nudge_disjoint_scope,
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
    decomp: list[tuple[str, str, list[str], list[str]]],
    *,
    criteria: list[AcceptanceCriterion] | None = None,
    done_when: str = "모든 ac 통과 AND 무회귀",
    goal: str = "병렬 시스템을 만든다",
    constraints: list[str] | None = None,
    non_goals: list[str] | None = None,
) -> ProjectSpec:
    """(unit, desc, deps, scope) 튜플 리스트로 ProjectSpec 생성(테스트 헬퍼)."""
    return ProjectSpec(
        spec_id="t-059", version=1, order_raw="주문", goal=goal,
        task_type=TaskType.feature_impl, verifiability=Verifiability.objective, mode=Mode.normal,
        constraints=constraints or ["c1"],
        acceptance_criteria=criteria or [
            AcceptanceCriterion(id="ac1", desc="기준", check=Check(type="test", cmd="pytest")),
        ],
        non_goals=non_goals or ["ng1", "ng2"], done_when=done_when,
        decomposition=[
            DecompositionUnit(unit=u, desc=d, deps=list(dp), scope=list(sc))
            for u, d, dp, sc in decomp
        ],
    )


# ──────────────────────────── 비파괴 스키마 ────────────────────────────


def test_scope_field_optional_defaults_empty():
    """scope 없는 기존 spec 로드 OK → 빈 리스트(비파괴)."""
    u = DecompositionUnit(unit="u1", desc="d", deps=[])
    assert u.scope == []


def test_scope_roundtrip_to_from_yaml(tmp_path):
    """scope 있는 spec round-trip(to_yaml/from_yaml, #58 사이드카와 정합)."""
    spec = _spec([("u1", "엔진", [], ["src/a.ts"]), ("u2", "UI", [], ["src/b.ts"])])
    p = tmp_path / "spec.yaml"
    p.write_text(spec.to_yaml(), encoding="utf-8")
    back = ProjectSpec.from_yaml(p)
    assert back.decomposition[0].scope == ["src/a.ts"]
    assert back.decomposition[1].scope == ["src/b.ts"]


def test_legacy_spec_without_scope_loads():
    """scope 키 없는 옛 YAML도 검증 통과(빈 리스트)."""
    y = """\
spec_id: x
version: 1
order_raw: o
goal: g
task_type: feature_impl
verifiability: objective
mode: normal
acceptance_criteria: [{id: ac1, desc: d, check: {type: test, cmd: t}}]
non_goals: [a, b]
done_when: dw
decomposition:
  - {unit: u1, desc: d, deps: []}
"""
    spec = ProjectSpec.model_validate(yaml.safe_load(y))
    assert spec.decomposition[0].scope == []


# ──────────────────────────── 탐지 보수성 ────────────────────────────


def test_overlap_detected_only_when_parallel_both_declared_and_overlap():
    """병렬 형제 + 양쪽 scope 선언 + 겹침일 때만 feedback."""
    spec = _spec([
        ("u1", "엔진", [], ["src/shared.ts"]),
        ("u2", "UI", [], ["src/shared.ts"]),  # u1과 병렬(dep 없음) + 같은 파일
    ])
    fb = disjoint_scope_feedback(spec)
    assert fb is not None
    assert "u1" in fb and "u2" in fb and "src/shared.ts" in fb


def test_no_overlap_when_disjoint():
    spec = _spec([
        ("u1", "엔진", [], ["src/a.ts"]),
        ("u2", "UI", [], ["src/b.ts"]),  # 서로 다른 파일
    ])
    assert disjoint_scope_feedback(spec) is None


def test_dep_linked_overlap_is_not_flagged():
    """dep로 엮인 유닛(직렬)은 같은 파일 겹쳐도 feedback 아님(순차 머지)."""
    spec = _spec([
        ("u1", "모델", [], ["src/shared.ts"]),
        ("u2", "로직", ["u1"], ["src/shared.ts"]),  # u2가 u1에 의존 → 직렬
    ])
    assert disjoint_scope_feedback(spec) is None


def test_transitive_dep_link_not_flagged():
    """전이 의존(u3←u2←u1)도 직렬 → 겹쳐도 feedback 아님."""
    spec = _spec([
        ("u1", "a", [], ["src/x.ts"]),
        ("u2", "b", ["u1"], ["src/y.ts"]),
        ("u3", "c", ["u2"], ["src/x.ts"]),  # u3는 u1에 전이 의존
    ])
    assert disjoint_scope_feedback(spec) is None


def test_one_sided_scope_is_noop():
    """한쪽만 scope 선언 → None(no-op, 보수적)."""
    spec = _spec([
        ("u1", "엔진", [], ["src/shared.ts"]),
        ("u2", "UI", [], []),  # 미선언
    ])
    assert disjoint_scope_feedback(spec) is None


def test_no_scope_anywhere_is_noop():
    spec = _spec([("u1", "a", [], []), ("u2", "b", [], [])])
    assert disjoint_scope_feedback(spec) is None


def test_single_unit_is_noop():
    spec = _spec([("u1", "a", [], ["src/a.ts"])])
    assert disjoint_scope_feedback(spec) is None


def test_are_parallel_and_transitive_deps_helpers():
    spec = _spec([
        ("u1", "a", [], []),
        ("u2", "b", ["u1"], []),
        ("u3", "c", [], []),
    ])
    t = _transitive_deps(spec)
    assert t["u2"] == {"u1"}
    assert _are_parallel("u1", "u3", t) is True
    assert _are_parallel("u1", "u2", t) is False  # dep로 엮임


# ──────────────────────────── bounded · advisory · no-op ────────────────────────────


def _overlapping_spec() -> ProjectSpec:
    return _spec([
        ("u1", "엔진", [], ["src/shared.ts"]),
        ("u2", "UI", [], ["src/shared.ts"]),
    ])


def _restructured_yaml(*, disjoint=True, bar_change=None) -> str:
    """재합성 결과 YAML — disjoint면 scope 분리, bar_change면 해당 bar 필드 변경(거부 대상)."""
    goal = "병렬 시스템을 만든다" if bar_change != "goal" else "다른 goal"
    done_when = "모든 ac 통과 AND 무회귀" if bar_change != "done_when" else "약한 기준"
    constraints = "[c1]" if bar_change != "constraints" else "[c1, c2]"
    non_goals = "[ng1, ng2]" if bar_change != "non_goals" else "[ng1]"
    ac_desc = "기준" if bar_change != "criteria" else "완화된 기준"
    s2 = "src/b.ts" if disjoint else "src/shared.ts"
    return f"""\
spec_id: t-059
version: 1
order_raw: 주문
goal: "{goal}"
task_type: feature_impl
verifiability: objective
mode: normal
constraints: {constraints}
acceptance_criteria: [{{id: ac1, desc: "{ac_desc}", check: {{type: test, cmd: pytest}}}}]
non_goals: {non_goals}
done_when: "{done_when}"
decomposition:
  - {{unit: u1, desc: 엔진, deps: [], scope: ["src/a.ts"]}}
  - {{unit: u2, desc: UI, deps: [], scope: ["{s2}"]}}
"""


def test_nudge_bounded_one_shot_and_adopts_decomposition():
    """겹침 → 정확히 1회 재합성, disjoint 재구성 채택(scope 분리됨, bar 동일)."""
    client = MockClient([_restructured_yaml(disjoint=True)])
    out = nudge_disjoint_scope("주문", _overlapping_spec(), client, prompt_path=PROMPT_PATH)
    assert len(client.calls) == 1  # bounded: 정확히 1회
    scopes = {u.unit: u.scope for u in out.decomposition}
    assert scopes["u1"] == ["src/a.ts"] and scopes["u2"] == ["src/b.ts"]  # 채택됨(disjoint)


def test_nudge_noop_when_no_overlap_zero_calls():
    """겹침 없으면 no-op — 추가 LLM 호출 0(비용 불변)."""
    spec = _spec([("u1", "a", [], ["src/a.ts"]), ("u2", "b", [], ["src/b.ts"])])
    client = MockClient(["should not be called"])
    out = nudge_disjoint_scope("주문", spec, client, prompt_path=PROMPT_PATH)
    assert client.calls == []  # 호출 0
    assert out is spec


def test_nudge_absorbs_resynthesis_exception_returns_original():
    """재합성 예외(파싱 실패) 흡수 → 원본 진행(advisory)."""
    client = MockClient(["{{{ broken yaml"])  # 파싱 실패 → SynthesisError 내부 발생
    orig = _overlapping_spec()
    out = nudge_disjoint_scope("주문", orig, client, prompt_path=PROMPT_PATH)
    # 원본 scope(겹친 채) 그대로 — 넛지가 run을 막지 않음.
    assert {u.unit: u.scope for u in out.decomposition} == {
        "u1": ["src/shared.ts"], "u2": ["src/shared.ts"]
    }


# ──────────────────────────── anti-erosion (핵심) ────────────────────────────


@pytest.mark.parametrize("field", ["goal", "done_when", "criteria", "constraints", "non_goals"])
def test_bar_change_ignored_scope_still_adopted(field):
    """WO#72 no-op 해소 + 구성적 anti-erosion: 모델이 scope를 바꾸며 bar도 건드려도 →
    scope 변경은 *적용*되고(형제 disjoint), bar는 *원본과 byte-동일*(모델 변경 무시). reject로 안 날아감."""
    client = MockClient([_restructured_yaml(disjoint=True, bar_change=field)])
    orig = _overlapping_spec()
    out = nudge_disjoint_scope("주문", orig, client, prompt_path=PROMPT_PATH)
    # scope 변경이 *실제로* 적용됨(no-op 아님) — 형제가 disjoint해짐.
    assert {u.unit: u.scope for u in out.decomposition} == {
        "u1": ["src/a.ts"], "u2": ["src/b.ts"]
    }
    # 바는 모델이 건드려도 무시 — 원본과 byte-동일(anti-erosion 구성적).
    od = orig.model_dump(by_alias=True, mode="json")
    nd = out.model_dump(by_alias=True, mode="json")
    for k in ("goal", "done_when", "acceptance_criteria", "constraints", "non_goals"):
        assert nd[k] == od[k], f"바 필드 {k}가 원본과 달라짐(anti-erosion 위반)"


def test_adopt_scope_only_keeps_bar_from_original():
    """`_adopt_scope_only`: scope/deps만 떼어 원본에 splice — bar는 모델이 바꿔도 원본 그대로."""
    orig = _spec(
        [("u1", "a", [], ["src/shared.ts"]), ("u2", "b", [], ["src/shared.ts"])],
        goal="원본 goal", done_when="원본 done_when",
    )
    restructured = _spec(
        [("u1", "a", [], ["src/a.ts"]), ("u2", "b", ["u1"], ["src/b.ts"])],
        goal="모델이 바꾼 goal", done_when="모델이 완화한 done_when",
    )
    out = _adopt_scope_only(orig, restructured)
    # scope/deps는 재구성에서 채택
    assert {u.unit: (u.deps, u.scope) for u in out.decomposition} == {
        "u1": ([], ["src/a.ts"]), "u2": (["u1"], ["src/b.ts"]),
    }
    # bar는 원본(모델 변경 무시)
    assert out.goal == "원본 goal" and out.done_when == "원본 done_when"


def test_adopt_scope_only_structural_change_returns_original():
    """재구성이 unit 집합을 바꾸면(개명/추가) scope-only splice 불가 → 보수적으로 원본(dangling 방지)."""
    orig = _spec([("u1", "a", [], ["src/shared.ts"]), ("u2", "b", [], ["src/shared.ts"])])
    restructured = _spec([("ux", "a", [], ["src/a.ts"]), ("u2", "b", [], ["src/b.ts"])])
    out = _adopt_scope_only(orig, restructured)
    assert out is orig  # 구조 변형 → 원본(데드락/dangling 방지)


# ──────────────────────────── decomp critic 불변(적대 분리) ────────────────────────────


def test_decomp_critic_replan_overlap_axis_decoupled_and_signal_only():
    """WO#165-v2(역할 분리): decomp critic은 *replan-time* 파일-소유권 겹침 축을 가지되 —
    (a) intake의 #59 메커니즘(`_scope_overlaps`/`disjoint_scope_feedback`)을 import·재사용하지 않고
    (intake 무변경·디커플, 보수 기준 로컬 재구현), (b) scope-overlap은 프롬프트 *신호*지 하드코딩
    verdict가 아니다(codex가 판정 — progress-only 정규화 유지). #59는 synthesis-time, 이 축은
    replan-time(같은 입력 scope·다른 시점 — 중복 아님).

    (#59 시절엔 critic에 scope-overlap *부재*가 불변식이었으나, #165-v2가 replan-time 갭을 메우며
    *신호*를 추가했다. 단 intake 메커니즘 미재사용 + verdict-비트리거는 유지된다.)
    """
    import inspect

    import haetae.decomp_critic as dc

    src = inspect.getsource(dc)
    # intake #59 메커니즘 미재사용(디커플 — intake 무변경). scheduler 술어도 미참조.
    assert "_scope_overlaps" not in src and "disjoint_scope_feedback" not in src
    assert "haetae.intake" not in src and "is_disjoint_from" not in src
    # 하지만 replan-time 소유권 *신호*는 존재한다(역할 분리 — 이름·시점이 다름).
    assert hasattr(dc, "scope_overlap_signal")
    # scope-overlap은 신호지 verdict 아님 — 미지 verdict는 progress(codex가 판정·안 막음, 유지).
    assert dc._norm_verdict("weak") == "weak"
    assert dc._norm_verdict("scope-overlap") == "progress"


def test_synthesizer_prompt_has_disjoint_scope_guidance():
    """synthesizer.md에 disjoint-scope 유도 + scope 예시가 있다."""
    md = PROMPT_PATH.read_text(encoding="utf-8")
    assert "disjoint" in md.lower() or "다른 파일" in md
    assert "scope" in md
    assert "scope: [" in md  # 예시에 scope 필드
