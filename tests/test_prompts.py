"""프롬프트 가이드 + run check.type 수용 테스트 (WO#24 Part A).

LLM *행동*은 캡스톤서 실물 검증한다. 여기선 (1) 프롬프트 파일이 run/trace 가이드를
담고 있는지(파일 내용 단언), (2) 합성 파이프라인이 run check.type을 그대로 받는지만 본다.
"""

from pathlib import Path

from haetae.intake import synthesize
from haetae.llm import MockClient
from haetae.models import CheckType, ProjectSpec

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPT_DIR = REPO_ROOT / "prompts"
SYNTH = PROMPT_DIR / "synthesizer.md"
CRITIC = PROMPT_DIR / "spec_critic.md"


# ──────────────────────── A1: synthesizer 가이드 ────────────────────────


def test_synthesizer_prompt_has_run_and_trace_guidance():
    text = SYNTH.read_text(encoding="utf-8")
    assert "run" in text
    assert "트레이스" in text  # 헤드리스 트레이스 진입점
    assert "decomposition" in text  # 진입점 만드는 unit 포함 지침
    # build/렌더 성공만 의존하지 말라는 경고가 있는지
    assert "build" in text or "렌더" in text


def test_synthesizer_prompt_has_run_check_example():
    text = SYNTH.read_text(encoding="utf-8")
    assert "type: run" in text  # 실제 check 예시


# ──────────────────────── A2: spec_critic 가이드 ────────────────────────


def test_spec_critic_prompt_recommends_run_for_dynamic_gaps():
    text = CRITIC.read_text(encoding="utf-8")
    assert "run" in text
    assert "트레이스" in text
    # 구조화 출력 형식(verdict/gaps)은 보존돼야 한다(행동 불변)
    assert "verdict" in text and "gaps" in text
    assert "strengthening" in text


# ──────────────────────── run check.type 수용 (합성 파이프라인) ────────────────────────

_SPEC_WITH_RUN = """\
spec_id: dyn-001
version: 1
order_raw: "에이전트가 자연스럽게 움직이는 시뮬"
goal: "100 에이전트가 목표로 분산 이동하는 연속공간 시뮬"
task_type: feature_impl
verifiability: judge
mode: normal
constraints: []
acceptance_criteria:
  - id: ac1
    desc: "트레이스상 에이전트가 목표로 분산 도달(콩나물 아님)"
    check: { type: run, cmd: "npm run sim:trace -- --ticks 300 --spawn high" }
assumptions: []
non_goals: ["3D 렌더", "멀티플레이어"]
done_when: "ac1 통과"
decomposition:
  - { unit: u1, desc: "sim:trace 헤드리스 진입점 구현", deps: [] }
open_questions: []
"""


def test_synthesize_accepts_run_check_type():
    """합성기 출력에 run check가 있어도 그대로 ProjectSpec으로 검증된다(CheckType.run)."""
    spec = synthesize("에이전트 시뮬", MockClient(_SPEC_WITH_RUN), prompt_path=SYNTH)
    assert isinstance(spec, ProjectSpec)
    assert spec.acceptance_criteria[0].check.type is CheckType.run
    assert "sim:trace" in spec.acceptance_criteria[0].check.cmd
