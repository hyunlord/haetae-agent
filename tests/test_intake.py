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


# ──────────────── 에러-피드백 재시도 (WO#31) ────────────────

# 콜론을 품은 문자열 값을 따옴표 없이 옮겨 YAML 파싱이 깨지는 케이스
# (대시보드가 표면화한 실제 사례: order 원문을 옮긴 필드의 unquoted 콜론).
BROKEN_YAML = (
    "spec_id: gen-001\n"
    "order_raw: 빌드해라: 콜론이 따옴표 없이 들어간 값\n"  # mapping values not allowed here
    "version: 1\n"
)


class _RaisingClient:
    """complete()에서 예외를 던지는 mock — 클라이언트 실패(provider 다운 등) 흉내."""

    def __init__(self, exc: Exception):
        self._exc = exc
        self.calls: list[dict] = []

    def complete(self, system: str, user: str, **opts) -> str:
        self.calls.append({"system": system, "user": user, "opts": opts})
        raise self._exc


def test_synthesize_retries_on_yaml_parse_failure_and_recovers():
    """1차 깨진 YAML → 2차 유효 YAML → 재시도로 회복, 파싱된 spec 반환."""
    client = MockClient([BROKEN_YAML, VALID_SPEC_YAML])
    spec = synthesize("로그인 폼 만들어", client, prompt_path=PROMPT_PATH)
    assert isinstance(spec, ProjectSpec)
    assert spec.spec_id == "gen-001"
    assert len(client.calls) == 2  # 재시도로 회복


def test_synthesize_feeds_parser_error_back_into_retry_prompt():
    """재요청 프롬프트에 파서 오류(라인 등)와 인용 교정 가이드가 되먹여졌는지."""
    client = MockClient([BROKEN_YAML, VALID_SPEC_YAML])
    synthesize("x", client, prompt_path=PROMPT_PATH)
    second = client.calls[1]["user"]
    assert "파싱" in second  # "YAML 파싱에 실패"
    assert "mapping" in second or "line" in second  # PyYAML 오류 텍스트(라인 정보)
    assert "따옴표" in second  # 교정 가이드(문자열 값 인용)


def test_synthesize_retries_exhausted_then_raises():
    """계속 깨진 YAML → 재시도 소진 → SynthesisError. 기본 retries=2 → 정확히 3시도."""
    client = MockClient([BROKEN_YAML, BROKEN_YAML, BROKEN_YAML])
    with pytest.raises(SynthesisError) as ei:
        synthesize("x", client, prompt_path=PROMPT_PATH)
    assert "빌드해라" in ei.value.raw_response  # raw 보존
    assert len(client.calls) == 3  # 1 + 재시도 2


def test_synthesize_retries_count_is_configurable():
    """synth_retries로 시도 횟수 조절 — 0이면 재시도 없이 단발."""
    client = MockClient([BROKEN_YAML])
    with pytest.raises(SynthesisError):
        synthesize("x", client, prompt_path=PROMPT_PATH, synth_retries=0)
    assert len(client.calls) == 1  # 재시도 0


def test_synthesize_does_not_retry_on_validation_failure():
    """스키마 검증 실패는 '깨진 YAML'이 아니므로 재시도 안 함(기존 동작 보존)."""
    bad = VALID_SPEC_YAML.replace("task_type: feature_impl", "task_type: made_up")
    client = MockClient([bad])  # 리스트: 재시도하면 2번째 호출에서 IndexError
    with pytest.raises(SynthesisError):
        synthesize("x", client, prompt_path=PROMPT_PATH)
    assert len(client.calls) == 1  # 재시도 없음


def test_synthesize_does_not_retry_on_non_mapping():
    """비-매핑(유효 YAML이지만 dict 아님)도 재시도 대상 아님."""
    client = MockClient(["- a\n- b\n- c"])
    with pytest.raises(SynthesisError):
        synthesize("x", client, prompt_path=PROMPT_PATH)
    assert len(client.calls) == 1


def test_synthesize_propagates_client_exception_without_retry():
    """클라이언트 예외(provider 다운 등)는 기존처럼 전파 — 재시도 루프가 새 크래시 안 만듦."""
    from haetae.llm import CodexError

    client = _RaisingClient(CodexError("codex 다운"))
    with pytest.raises(CodexError):
        synthesize("x", client, prompt_path=PROMPT_PATH)
    assert len(client.calls) == 1  # 첫 호출에서 즉시 전파, 재시도 없음


def test_synthesizer_prompt_has_string_quoting_guidance():
    """Part C 예방: 합성기 프롬프트가 문자열 값 인용을 명시(unquoted 콜론 → 파싱 에러)."""
    text = PROMPT_PATH.read_text(encoding="utf-8")
    assert "따옴표" in text
    assert "콜론" in text or "mapping values are not allowed" in text
