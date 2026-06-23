"""분해 전 director-측 research 단계 테스트 (WO#166) — mock LLM만(네트워크/시크릿 없음).

복잡 의뢰면 첫 synthesize *전* 1회 bounded research → ResearchBrief(태스크분석·스택·패턴·후보
disjoint-scope 경계·facade 계약) → synth_context에 *제안*으로 주입. 단순 의뢰는 skip(추가 콜 0).
best-effort(실패→None)·오프라인(#32 레지스트리)·적대 분리(brief≠판정·executor 무관).
"""

from pathlib import Path

from haetae.llm import MockClient
from haetae.models import CandidateContract, CandidateUnit, ResearchBrief
from haetae.research import (
    DEFAULT_RESEARCH_PROMPT_PATH,
    is_complex_order,
    maybe_research,
    render_brief,
    research,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
RESEARCH_PROMPT = REPO_ROOT / "prompts" / "research.md"
SYN_PROMPT = REPO_ROOT / "prompts" / "synthesizer.md"

_BRIEF_YAML = """\
task_analysis: "snake: 이동·충돌·먹이·점수·game-over = 독립 서브시스템 다수."
stack: "TypeScript + Vite, src/ 모듈별 파일."
patterns:
  - "행동 게임은 로직을 렌더에서 분리해 node 헤드리스로 트레이스(서버리스)."
candidate_units:
  - { unit: u1, desc: "이동", scope: ["src/move.ts"], deps: [] }
  - { unit: u2, desc: "충돌", scope: ["src/collision.ts"], deps: [] }
  - { unit: u3, desc: "엔진 조립(wire)", scope: ["src/engine.ts"], deps: [u1, u2] }
candidate_contracts:
  - { producer: u3, module_path: "src/engine.ts", export_name: "GameEngine", consumers: [u4] }
note: "경계는 제안 — 합성기가 조정 가능."
"""

_COMPLEX_ORDER = "snake 게임을 만들어줘 — 이동·충돌·먹이·점수·game-over"
_SIMPLE_ORDER = "hello를 출력하는 함수 하나"


# ──────────────────────────── 복잡도 게이트 ────────────────────────────


def test_is_complex_order_flags_multi_subsystem_keywords():
    assert is_complex_order("snake 게임 엔진을 만들어줘") is True
    assert is_complex_order("crowd simulation with collision avoidance") is True
    assert is_complex_order("실시간 대시보드") is True


def test_is_complex_order_flags_long_order():
    long_order = "이 기능을 구현하라. " * 20  # ≥ 길이 임계
    assert len(long_order) >= 200
    assert is_complex_order(long_order) is True


def test_is_complex_order_false_for_simple():
    assert is_complex_order("hello를 출력") is False
    assert is_complex_order("두 수를 더하는 함수") is False
    assert is_complex_order("") is False


# ──────────────────────────── research 패스 (best-effort·bounded) ────────────────────────────


def test_research_produces_brief_with_all_sections():
    client = MockClient([_BRIEF_YAML])
    brief = research(_COMPLEX_ORDER, client, prompt_path=RESEARCH_PROMPT)
    assert isinstance(brief, ResearchBrief)
    assert "독립 서브시스템" in brief.task_analysis
    assert "Vite" in brief.stack
    assert brief.patterns and "헤드리스" in brief.patterns[0]
    # 후보 disjoint-scope 분해(#165): 유닛별 소유 파일
    scopes = {u.unit: u.scope for u in brief.candidate_units}
    assert scopes["u1"] == ["src/move.ts"] and scopes["u2"] == ["src/collision.ts"]
    # 형제 u1·u2 소유 ∩ = ∅(disjoint)
    assert set(scopes["u1"]).isdisjoint(scopes["u2"])
    # 후보 facade 계약(#160)
    assert brief.candidate_contracts[0].export_name == "GameEngine"
    assert brief.candidate_contracts[0].producer == "u3"


def test_research_is_bounded_one_call():
    client = MockClient([_BRIEF_YAML])
    research(_COMPLEX_ORDER, client, prompt_path=RESEARCH_PROMPT)
    assert len(client.calls) == 1  # 정확히 1회(선행·bounded)


def test_research_uses_research_prompt_and_order():
    client = MockClient([_BRIEF_YAML])
    research(_COMPLEX_ORDER, client, prompt_path=RESEARCH_PROMPT)
    assert "snake 게임" in client.calls[0]["user"]  # order가 user에 들어감
    assert "ResearchBrief" in client.calls[0]["system"]  # research 프롬프트 사용


def test_research_parse_failure_returns_none():
    """깨진 출력 → None(브리프 없이 진행, 절대 raise 안 함)."""
    assert research(_COMPLEX_ORDER, MockClient(["이건 YAML이 아니다 {{{"]), prompt_path=RESEARCH_PROMPT) is None


def test_research_client_exception_returns_none():
    """client.complete가 던져도 raise하지 않고 None으로 흡수."""

    class _Raise:
        def complete(self, system, user, **opts):
            raise RuntimeError("codex 다운")

    assert research(_COMPLEX_ORDER, _Raise(), prompt_path=RESEARCH_PROMPT) is None


# ──────────────────────────── 오프라인 (#32 레지스트리, 네트워크 0) ────────────────────────────


def _make_registry(tmp_path, name: str, triggers: list[str], body: str) -> Path:
    d = tmp_path / "skills" / name
    d.mkdir(parents=True)
    trig = "\n".join(f"  - {t}" for t in triggers)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ntriggers:\n{trig}\n---\n{body}\n", encoding="utf-8"
    )
    return tmp_path / "skills"


def test_research_consults_offline_skill_registry(tmp_path):
    """research가 #32 레지스트리(오프라인)서 의뢰에 매칭된 패턴을 user 프롬프트에 넣는다."""
    reg = _make_registry(tmp_path, "game-trace", ["game", "게임"], "행동 게임은 헤드리스 트레이스로 검증")
    client = MockClient([_BRIEF_YAML])
    research(_COMPLEX_ORDER, client, skills_dir=reg, prompt_path=RESEARCH_PROMPT)
    user = client.calls[0]["user"]
    assert "헤드리스 트레이스로 검증" in user  # 레지스트리 패턴 본문 주입
    assert "game-trace" in user


def test_research_no_network_imports():
    """오프라인: research.py가 네트워크 모듈을 import하지 않는다(F.2 deferred)."""
    src = (REPO_ROOT / "src" / "haetae" / "research.py").read_text(encoding="utf-8")
    for forbidden in ("import requests", "urllib.request", "http.client", "import socket",
                      "capability_search", "make_searcher"):
        assert forbidden not in src, f"research가 네트워크 의존({forbidden})"
    # 소스 = #32 레지스트리(load_skills/match_skills)
    assert "load_skills" in src and "match_skills" in src


# ──────────────────────────── render_brief / maybe_research ────────────────────────────


def _brief() -> ResearchBrief:
    return ResearchBrief(
        task_analysis="이동·충돌 분리",
        stack="TS+Vite",
        patterns=["헤드리스 트레이스"],
        candidate_units=[
            CandidateUnit(unit="u1", desc="이동", scope=["src/move.ts"], deps=[]),
            CandidateUnit(unit="u2", desc="충돌", scope=["src/collision.ts"], deps=[]),
        ],
        candidate_contracts=[
            CandidateContract(producer="u3", module_path="src/engine.ts", export_name="GameEngine", consumers=["u4"]),
        ],
    )


def test_render_brief_formats_sections_and_marks_proposal():
    out = render_brief(_brief())
    assert "리서치 브리프" in out and "제안" in out and "mandate 아님" in out
    assert "src/move.ts" in out and "src/collision.ts" in out  # 후보 disjoint scope
    assert "GameEngine" in out and "#160" in out                # 후보 facade 계약
    assert "#165" in out                                        # disjoint-scope 출처


def test_maybe_research_skips_simple_order_zero_calls():
    """단순 의뢰 → research skip(추가 LLM 콜 0)·synth_context 불변(직접 synthesize)."""
    client = MockClient(["should not be called"])
    out = maybe_research(_SIMPLE_ORDER, client, "기존 컨텍스트", skills_dir=None)
    assert client.calls == []          # 호출 0
    assert out == "기존 컨텍스트"      # 불변


def test_maybe_research_complex_appends_brief_to_context():
    """복잡 의뢰 → brief 생성 → synth_context에 *제안*으로 append(기존 context 보존)."""
    client = MockClient([_BRIEF_YAML])
    out = maybe_research(_COMPLEX_ORDER, client, "기존 컨텍스트", skills_dir=None)
    assert len(client.calls) == 1
    assert "기존 컨텍스트" in out          # 기존 보존
    assert "리서치 브리프" in out          # 브리프 append
    assert "src/move.ts" in out            # 후보 경계 들어감


def test_maybe_research_none_context_returns_brief_only():
    out = maybe_research(_COMPLEX_ORDER, MockClient([_BRIEF_YAML]), None, skills_dir=None)
    assert out is not None and "리서치 브리프" in out


def test_maybe_research_research_failure_keeps_context():
    """복잡하지만 research 실패(파싱 불가) → synth_context 불변(브리프 없이 진행)."""
    out = maybe_research(_COMPLEX_ORDER, MockClient(["{{{ broken"]), "기존 컨텍스트", skills_dir=None)
    assert out == "기존 컨텍스트"


# ──────────────────────────── 모델 ────────────────────────────


def test_research_brief_model_optional_defaults():
    """ResearchBrief 전부 optional → 부분/빈 brief도 유효(graceful)."""
    b = ResearchBrief()
    assert b.task_analysis == "" and b.patterns == [] and b.candidate_units == []
    assert "owned_paths" not in CandidateUnit.model_fields  # #165: scope가 정식 필드명
    assert "scope" in CandidateUnit.model_fields


# ──────────────────────────── 합성기 소비 (제안이지 강제 아님) ────────────────────────────


def test_synthesizer_prompt_consumes_research_brief_as_proposal():
    """synthesizer.md가 리서치 브리프를 *제안*(override 가능)으로 소비하는 지침을 담는다."""
    syn = SYN_PROMPT.read_text(encoding="utf-8")
    assert "리서치 브리프" in syn and "#166" in syn
    assert "mandate 아님" in syn or "override" in syn  # 강제 아님


# ──────────────────────────── 적대 분리 (brief≠판정·executor 무관) ────────────────────────────


def test_research_module_is_director_side_not_judgment():
    """research.py는 판정 주체(gate/run_judge/judge/CompositeGate)·or_node/scheduler·
    ALLOWED_SANDBOXES·executor를 *import/사용*하지 않는다(director-side 계획 입력 — 적대 gate 독립).

    (산문에 'executor 아님' 같은 설명은 허용 — 실제 결합인 import-form만 검사한다, #165 교훈.)"""
    src = (REPO_ROOT / "src" / "haetae" / "research.py").read_text(encoding="utf-8")
    for forbidden in (
        "haetae.gate", "run_judge", "haetae.judge", "CompositeGate",
        "haetae.or_node", "haetae.scheduler", "ALLOWED_SANDBOXES",
        "haetae.executors", "from haetae.loop import",
    ):
        assert forbidden not in src, f"research가 {forbidden} 참조(분리 위반)"
    # research는 오케스트레이션 LLM(complete())을 쓰지 executor를 안 쓴다(director-side 입력).
    assert "complete" in src and "LLMClient" in src


def test_research_run_wiring_is_opt_in_and_loop_unchanged():
    """run.py: research는 opt-in(--research 기본 off)·순수 재개 skip·critic-model 우선이고,
    loop.py 코어는 무변경(brief는 기존 synth_context 배관으로 주입)."""
    run_src = (REPO_ROOT / "src" / "haetae" / "run.py").read_text(encoding="utf-8")
    assert '"--research"' in run_src and "default=False" in run_src  # opt-in
    assert "maybe_research" in run_src
    assert "resume_spec is None" in run_src        # 순수 재개면 skip
    assert "critic_client or client" in run_src    # 오케스트레이션 LLM(critic-model 우선)
    # loop.py 코어 무변경: research를 import·호출하지 않는다(brief는 기존 synth_context 배관으로만 흐름)
    loop_src = (REPO_ROOT / "src" / "haetae" / "loop.py").read_text(encoding="utf-8")
    assert "from haetae.research" not in loop_src and "maybe_research" not in loop_src
