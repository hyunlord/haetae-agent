"""WO#107 — 매칭 정밀도: word-boundary 토큰 매칭(스킬 트리거 + criteria→unit).

naive substring 과매칭 제거(왼쪽 word-boundary 앵커) + stem/prefix·멀티워드 구·한글 조사 보존.
결정적(정규식·LLM 아님). 스킬=빌더-측·criteria→unit=#97 리셋 범위 → 판정 무변경.
"""

from pathlib import Path

import yaml

from haetae.or_node import implicated_units
from haetae.models import ProjectSpec
from haetae.skills import Skill, boundary_match, load_skills, match_skills

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = REPO_ROOT / "skills"


# ════════════════════ 1. boundary_match 단위 (왼쪽 경계·오른쪽 열림) ════════════════════


def test_boundary_blocks_ascii_midword_overmatch():
    """ascii 중간-단어 substring 과매칭 차단(왼쪽 경계 실패)."""
    assert boundary_match("ui", "ui components") is True       # 토큰 시작
    assert boundary_match("ui", "build the fluid guid") is False  # build/fluid/guid 中 'ui' — 차단
    assert boundary_match("store", "store inventory") is True
    assert boundary_match("store", "bookstore restore") is False  # book**store**/re**store** 차단
    assert boundary_match("agent", "user agent") is True       # 진짜 토큰 경계(의미 disambig는 v2)
    assert boundary_match("agent", "reagent levels") is False  # re**agent** 차단


def test_boundary_preserves_stem_prefix():
    """stem/prefix 보존(오른쪽 열림): 'sim'→'simulation'."""
    assert boundary_match("sim", "simulation engine") is True
    assert boundary_match("sim", "sim") is True
    assert boundary_match("trace", "traceback harness") is True   # stem


def test_boundary_preserves_phrase_and_punct():
    """멀티워드 구·구두점 포함 트리거 보존."""
    assert boundary_match("behavior trace", "live behavior trace harness") is True
    assert boundary_match("sim:trace", "headless sim:trace entrypoint") is True  # 콜론 경계
    assert boundary_match("crowd simulator", "a 2d crowd simulator") is True


def test_boundary_preserves_korean_agglutination():
    """한글 조사/어미 부착 보존(왼쪽 경계는 ascii 영숫자만 차단 → 한글 인접 허용)."""
    assert boundary_match("드래그앤드롭", "카드를 드래그앤드롭으로 이동") is True   # 으로 부착
    assert boundary_match("드래그앤드롭", "드래그앤드롭") is True
    assert boundary_match("localstorage", "localstorage 어댑터") is True


def test_boundary_empty_needle_false():
    assert boundary_match("", "anything") is False


# ════════════════════ 2. 스킬 트리거: 과매칭 제거 + 진짜 매칭 보존 ════════════════════


def _skill(name, triggers):
    return Skill(name=name, triggers=tuple(triggers), body="pattern body")


def test_skill_overmatch_removed():
    """일반어 트리거가 무관 유닛(중간-단어 substring)에 매칭 안 함."""
    skills = [_skill("uiskill", ["ui"]), _skill("storeskill", ["store"])]
    # 'build a fluid layout' — 'ui'가 fluid/build 中에만 존재 → 매칭 0(과매칭 제거)
    assert match_skills(skills, "build a fluid layout require") == []
    # 'bookstore restore flow' — 'store'가 중간-단어에만 → 매칭 0
    assert match_skills(skills, "bookstore restore flow") == []


def test_skill_stem_and_phrase_preserved():
    """stem('sim'→'simulation')·멀티워드 구 매칭 유지."""
    skills = [_skill("simskill", ["sim"]), _skill("phrase", ["crowd simulator"])]
    assert [s.name for s in match_skills(skills, "build a simulation")] == ["simskill"]
    # 'crowd simulator' 구 매칭 + 'sim' stem('simulator') 둘 다 — phrase 매칭 보존 확인.
    names = {s.name for s in match_skills(skills, "a 2d crowd simulator app")}
    assert "phrase" in names


def test_seeded_skills_intended_matches_preserved():
    """★back-compat★: seeded 스킬이 의도 유닛에 매칭 유지(frontend/simulation/verification)."""
    skills = load_skills(SKILLS_DIR)
    names = lambda wo: {s.name for s in match_skills(skills, wo)}
    assert "frontend-build" in names("React Vite TypeScript dashboard with canvas")
    assert "simulation-behavior" in names("crowd simulation with agents and collision avoidance")
    assert "verification-harness" in names("헤드리스 sim:trace 하니스 진입점 구현")
    assert "verification-harness" in names("playwright e2e harness for the app")


def test_seeded_skills_no_overmatch_on_unrelated():
    """seeded 스킬이 무관 유닛엔 과매칭 안 함(엔진/순수 로직 유닛)."""
    skills = load_skills(SKILLS_DIR)
    matched = {s.name for s in match_skills(skills, "계산대 FIFO 큐 결제 완료 흐름 구현")}
    # 'queue' 트리거(simulation-behavior)는 한글 텍스트엔 없음 → 과매칭 없어야(verification-harness 등)
    assert "verification-harness" not in matched
    assert "frontend-build" not in matched


# ════════════════════ 3. criteria→unit (or_node #97) word-boundary ════════════════════

_OVERMATCH_SPEC = """\
spec_id: wb-001
version: 1
order_raw: x
goal: g
task_type: feature_impl
verifiability: objective
mode: normal
constraints: []
acceptance_criteria:
  - { id: ac_x, desc: "prerendered 캐시 무효화 통합", check: { type: run, cmd: "true" }, unit: integration }
non_goals: ["n"]
done_when: "전부 통과"
decomposition:
  - { unit: u_seed, desc: "기반 상태 모델", deps: [], scope: ["src/state.ts"] }
  - { unit: u_render, desc: "render 파이프라인", deps: [u_seed], scope: ["src/render.ts"] }
open_questions: []
"""


def test_criteria_unit_overmatch_blocked_falls_back():
    """distinctive 토큰 'render'가 criterion 'prerendered' 中 substring일 뿐 → word-boundary 차단 →
    매핑 0 → None(전체 리셋 폴백, 안전)."""
    spec = ProjectSpec.model_validate(yaml.safe_load(_OVERMATCH_SPEC))
    # 'render'는 'prerendered'의 중간-단어 → boundary_match False → owner 0 → None.
    assert implicated_units(spec, {"ac_x"}) is None


def test_criteria_unit_real_token_still_maps():
    """진짜 토큰 경계 매칭은 유지 — criterion이 'render 파이프라인 회귀'면 u_render 매핑."""
    y = yaml.safe_load(_OVERMATCH_SPEC)
    y["acceptance_criteria"][0]["desc"] = "render 파이프라인 회귀"
    spec = ProjectSpec.model_validate(y)
    imp = implicated_units(spec, {"ac_x"})
    assert imp is not None and "u_render" in imp     # 경계 매칭 유지


def test_criteria_unit_dnd_persist_preserved():
    """back-compat: #92류 드래그앤드롭·localStorage 매핑 유지(기존 동작)."""
    y = {
        "spec_id": "wb-2", "version": 1, "order_raw": "x", "goal": "g",
        "task_type": "feature_impl", "verifiability": "objective", "mode": "normal",
        "acceptance_criteria": [
            {"id": "ac_dnd", "desc": "드래그앤드롭으로 카드 이동", "check": {"type": "run", "cmd": "true"}, "unit": "integration"},
        ],
        "non_goals": ["n"], "done_when": "x",
        "decomposition": [
            {"unit": "u_seed", "desc": "기반 상태", "deps": [], "scope": ["s.ts"]},
            {"unit": "u_dnd", "desc": "포인터 드래그앤드롭 UI", "deps": ["u_seed"], "scope": ["d.ts"]},
        ],
    }
    spec = ProjectSpec.model_validate(y)
    imp = implicated_units(spec, {"ac_dnd"})
    assert imp is not None and "u_dnd" in imp and "u_seed" not in imp   # 좁은 리셋
