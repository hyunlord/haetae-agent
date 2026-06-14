"""WO#94 — 군중/에이전트 충돌회피 스킬 (simulation-behavior 강화) 테스트.

crowd-sim 그리드락(순진한 위치-점유 거부)을 막는 빌더-측 패턴 주입을 검증한다:
충돌회피(RVO/ORCA 개념 또는 flow-field)·연속 스폰·큐 형성·로직-렌더 분리.
**빌더 전용**(judge/run-judge/gate 무주입 = 적대 분리), 바 불변, 알고리즘 패턴(구현 아님), IP 클론 금지.
실 SKILL.md(skills/simulation-behavior)를 로드해 검사 — mock/픽스처만(네트워크 없음).
"""

from pathlib import Path

from haetae.llm import MockClient
from haetae.loop import MockExecutor, MockGate, run_loop
from haetae.models import Status, Verdict
from haetae.skills import SKILL_SECTION_HEADER, load_skills, match_skills

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPT_DIR = REPO_ROOT / "prompts"
SEED_SKILLS_DIR = REPO_ROOT / "skills"


def _sim_skill():
    skills = load_skills(SEED_SKILLS_DIR)
    sim = [s for s in skills if s.name == "simulation-behavior"]
    assert sim, "simulation-behavior 스킬이 로드돼야 한다"
    return sim[0]


# ──────────────────────────── 패턴 내용 (충돌회피·스폰·큐·로직렌더) ────────────────────────────


def test_skill_has_collision_avoidance_algorithms():
    """속도기반 상호회피(RVO/ORCA 개념) + 대안 flow-field가 *원리 수준*으로 들어있다."""
    body = _sim_skill().body
    assert "RVO" in body and "ORCA" in body          # 속도기반 상호 회피 개념
    assert "flow-field" in body                        # 대안 벡터장 길찾기
    assert "상호" in body and "reciprocal" in body     # 회피 책임 절반씩(진동/교착 방지)
    assert "velocity obstacle" in body                 # 충돌 속도 집합 추정


def test_skill_targets_naive_position_blocking_gridlock():
    """그리드락 근본(순진한 위치-점유 거부)을 명시 지목한다 (#81/#87 진단)."""
    body = _sim_skill().body
    assert "위치-점유" in body                          # naive position-blocking
    assert "그리드락" in body or "gridlock" in body
    assert "교착" in body


def test_skill_has_continuous_spawn_and_queue():
    body = _sim_skill().body
    assert "연속 스폰" in body and "버스트" in body      # 연속 유입 / 버스트 금지
    assert "입구 압력" in body                           # backpressure
    assert "대기열" in body and "큐" in body             # 큐 형성(점유 슬롯+대기열+순차)
    assert "점유 슬롯" in body


# ──────────────────────────── v2: reciprocal collision-free (#94 v2, #112 대응) ────────────────────────────


def test_skill_v2_names_velocity_truncation_weakness():
    """v2가 #94 비상호 속도-절단의 약점(liveness hack·통과→overlap)을 #112 데이터로 명시 지목한다."""
    body = _sim_skill().body
    assert "liveness hack" in body
    assert "collision-free가 아니" in body              # liveness ≠ collision-free
    assert "통과" in body and "overlap" in body          # 멈추는 대신 서로 통과 → overlap
    assert "#112" in body                                # stress sweep 데이터 근거
    assert "비상호" in body or "non-reciprocal" in body  # 약점의 원인


def test_skill_v2_collision_free_priority_stop_not_pass():
    """충돌-free 후보 없으면 *정지*(통과 금지) — separation > progress, 교착은 비대칭으로 회피."""
    body = _sim_skill().body
    assert "통과 금지" in body and "정지" in body         # 정지 허용·통과 금지
    assert "separation > progress" in body or "separation > 전진" in body
    # 멈춤이 교착 안 되게 약한 비대칭(우선순위/지터/우측 양보)
    assert "비대칭" in body and ("지터" in body or "우선순위" in body)
    # half-plane(ORCA 연속해)로 상호성 강화
    assert "half-plane" in body


def test_skill_v2_density_handling_and_flow_field_coupling():
    """밀도 대응(이산 샘플 고갈→조밀화/연속 half-plane) + flow-field에 지역 상호분리 결합."""
    body = _sim_skill().body
    assert "조밀화" in body                               # 이산 샘플 해상도↑
    assert "clearance" in body                            # radius+clearance 접촉 전 분리
    # flow-field 전역 흐름 + 지역 reciprocal 분리 결합
    assert "flow-field" in body and "지역" in body and "분리" in body


def test_skill_v2_preserves_gridlock_removal():
    """#94 교착-제거는 유지 — v2는 그 위에 collision-free를 얹을 뿐(회귀 방지)."""
    body = _sim_skill().body
    assert "그리드락" in body or "gridlock" in body       # 교착 제거 패턴 보존
    assert "위치-점유" in body                            # naive position-blocking 여전히 금지
    assert "정지는 허용" in body                          # 멈춤 허용(교착 아님) — liveness 유지


def test_skill_has_logic_render_separation():
    """로직-렌더 분리 → node 헤드리스 트레이스 가능(#84/#86 정합)."""
    body = _sim_skill().body
    assert "로직-렌더 분리" in body
    assert "헤드리스" in body
    assert "canvas/DOM" in body or "canvas" in body


def test_skill_is_pattern_not_copypaste_and_no_ip_clone():
    body = _sim_skill().body
    assert "그대로 베끼지 말고" in body                  # 패턴/원리지 완성 구현 아님
    assert "클론" in body and "원본" in body             # IP: 원본 작품, 클론 금지


def test_skill_forbids_self_scoring():
    """자가채점 금지 — 엔진/하니스가 스스로 판정 X, 원시 증거만(검증 독립성)."""
    body = _sim_skill().body
    assert "자가채점 금지" in body
    assert "독립 run-judge" in body


def test_skill_bar_invariant_no_weakening_language():
    """anti-erosion: 스킬은 빌더 가이드일 뿐 — 성공 기준(바)을 낮추라는 지시가 없어야 한다."""
    body = _sim_skill().body
    for weakening in ["기준을 낮", "기준 낮", "바를 낮", "완화하라", "통과시켜라", "쉽게 통과"]:
        assert weakening not in body, f"바 완화 문구 금지: {weakening!r}"


# ──────────────────────────── 매칭 (군중/시뮬/네비/충돌) ────────────────────────────


def _matches(text: str) -> bool:
    return any(s.name == "simulation-behavior" for s in match_skills(load_skills(SEED_SKILLS_DIR), text))


def test_matches_crowd_sim_collision_navigation_units():
    assert _matches("crowd simulation: agents navigate to exits")
    assert _matches("implement collision avoidance for moving agents")
    assert _matches("pedestrian navigation with flow-field pathfinding")
    assert _matches("RVO-based local steering for the crowd")
    assert _matches("flocking boids with separation")
    # 레거시 매칭 보존(기존 test_skills 무회귀)
    assert _matches("crowd simulation with queue and spawn logic")


def test_not_match_unrelated_units():
    """과매칭 방지: 군중/시뮬 무관 유닛엔 안 붙는다."""
    assert not _matches("마크다운 에디터: 실시간 프리뷰와 툴바, 문법 강조")
    assert not _matches("REST API 엔드포인트와 인증 미들웨어 구현")
    assert not _matches("kanban board with drag and drop and localStorage")


# ──────────────────────────── 빌더-측 전용 (judge/gate 무주입 = 적대 분리) ────────────────────────────

_SPEC = """\
spec_id: crowd-skill-001
version: 1
order_raw: "x"
goal: "g"
task_type: feature_impl
verifiability: objective
mode: normal
constraints: []
acceptance_criteria:
  - id: ac1
    desc: "d"
    check: { type: test, cmd: "true" }
assumptions: []
non_goals: ["n1", "n2"]
done_when: "ac1"
decomposition:
  - { unit: u1, desc: a, deps: [] }
open_questions: []
"""

_DEC = """\
verdict: pass
action: next_order
rationale: "build"
next_order:
  unit: u1
  goal: "crowd simulation engine with RVO collision avoidance"
  deliverable: "요약"
"""


def test_crowd_skill_builder_side_only_not_in_gate_or_events():
    """crowd 유닛에 simulation-behavior가 executor에만 주입되고 gate/감사 이벤트엔 안 샌다."""
    client = MockClient([_SPEC, _DEC])
    executor = MockExecutor(["순수 executor 결과"])
    gate = MockGate([Verdict.done])
    state = run_loop(
        "x", client, executor=executor, gate=gate,
        prompt_dir=PROMPT_DIR, skills_dir=SEED_SKILLS_DIR,
    )
    assert state.status is Status.done
    injected = executor.calls[0].scope or ""
    # 빌더(executor)에는 충돌회피 패턴이 주입됨
    assert SKILL_SECTION_HEADER in injected
    assert "RVO" in injected and "충돌 회피" in injected
    # gate가 본 입력(result)·감사 이벤트엔 스킬 흔적 없음 (적대 분리)
    for seen in gate.calls:
        assert SKILL_SECTION_HEADER not in seen
        assert "RVO" not in seen
    for ev in state.events:
        assert SKILL_SECTION_HEADER not in (ev.result or "")
        assert SKILL_SECTION_HEADER not in (ev.work_order_ref or "")
