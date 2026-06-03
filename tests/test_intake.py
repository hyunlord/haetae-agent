"""intake 러너 테스트 — mock LLM만 사용(네트워크/시크릿 없음)."""

from pathlib import Path

import pytest

from haetae.intake import SynthesisError, synthesize
from haetae.llm import MockClient
from haetae.models import Mode, ProjectSpec, TaskType, Verifiability

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPT_PATH = REPO_ROOT / "prompts" / "synthesizer.md"

# 합성기가 내놓는다고 가정하는 유효한 ProjectSpec YAML.
VALID_SPEC_YAML = """\
spec_id: gen-001
version: 1
order_raw: "로그인 폼 만들어"
goal: "이메일/비밀번호 로그인 폼을 추가한다"
task_type: feature_impl
verifiability: objective
mode: normal
constraints:
  - "기존 디자인 시스템 사용"
acceptance_criteria:
  - id: ac1
    desc: "유효한 자격증명으로 로그인 성공"
    check: { type: test, cmd: "pytest test_login" }
assumptions:
  - { id: as1, text: "소셜 로그인은 범위 외", confidence: 0.8, checkpoint: false }
non_goals:
  - "비밀번호 재설정"
  - "2FA"
done_when: "ac1 통과 AND 무회귀"
decomposition:
  - { unit: u1, desc: "폼 컴포넌트", deps: [] }
open_questions: []
"""


def _synth(response: str) -> ProjectSpec:
    return synthesize("로그인 폼 만들어", MockClient(response), prompt_path=PROMPT_PATH)


# ──────────────────────────── positive ────────────────────────────


def test_synthesize_returns_validated_projectspec():
    spec = _synth(VALID_SPEC_YAML)
    assert isinstance(spec, ProjectSpec)
    assert spec.spec_id == "gen-001"
    assert spec.task_type is TaskType.feature_impl
    assert spec.verifiability is Verifiability.objective
    assert spec.mode is Mode.normal
    assert spec.acceptance_criteria[0].check.type.value == "test"
    assert len(spec.non_goals) == 2


def test_synthesize_strips_yaml_code_fence():
    fenced = f"```yaml\n{VALID_SPEC_YAML}```"
    spec = _synth(fenced)
    assert spec.spec_id == "gen-001"


def test_synthesize_strips_bare_code_fence():
    fenced = f"```\n{VALID_SPEC_YAML}\n```"
    spec = _synth(fenced)
    assert spec.spec_id == "gen-001"


def test_synthesize_passes_order_and_context_to_client():
    client = MockClient(VALID_SPEC_YAML)
    synthesize(
        "로그인 폼 만들어",
        client,
        context="앱은 Flask 기반",
        prompt_path=PROMPT_PATH,
    )
    call = client.calls[0]
    # system은 synthesizer.md 내용
    assert "합성" in call["system"] or len(call["system"]) > 0
    assert "로그인 폼 만들어" in call["user"]
    assert "Flask" in call["user"]


# ──────────────────────────── negative ────────────────────────────


def test_synthesize_rejects_invalid_enum():
    bad = VALID_SPEC_YAML.replace("task_type: feature_impl", "task_type: made_up")
    with pytest.raises(SynthesisError) as ei:
        _synth(bad)
    # raw 응답이 예외에 동봉됐는지
    assert "made_up" in ei.value.raw_response


def test_synthesize_rejects_broken_yaml():
    broken = "spec_id: [unclosed\n  : : :"
    with pytest.raises(SynthesisError):
        _synth(broken)


def test_synthesize_rejects_non_mapping_response():
    with pytest.raises(SynthesisError):
        _synth("- just\n- a\n- list")
