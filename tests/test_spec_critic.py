"""adversarial spec critic 테스트 — mock LLM만(네트워크/시크릿 없음).

critique_spec(파싱/흡수) + synthesize_with_critique(하이브리드 (a)surface (b)바운드 재합성).
"""

from pathlib import Path

from haetae.llm import MockClient
from haetae.models import ProjectSpec, SpecCritique
from haetae.spec_critic import critique_spec, synthesize_with_critique

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPT_DIR = REPO_ROOT / "prompts"
SYN_PROMPT = PROMPT_DIR / "synthesizer.md"
CRITIC_PROMPT = PROMPT_DIR / "spec_critic.md"


# ──────────────────────────── spec 픽스처 ────────────────────────────

# 합성기 출력(약한 기준 버전 / 강화 버전) — order는 "연속공간 충돌 금지".
ORDER = "에이전트들이 연속공간에서 고밀도로 움직여도 절대 겹치지 않게 해라"

SPEC_WEAK = """\
spec_id: sim-001
version: 1
order_raw: "{order}"
goal: "에이전트 이동 시뮬레이션"
task_type: feature_impl
verifiability: objective
mode: normal
constraints: []
acceptance_criteria:
  - id: ac1
    desc: "같은 격자 칸을 둘이 점유하지 않음"
    check: {{ type: test, cmd: "pytest grid" }}
assumptions: []
non_goals: []
done_when: "ac1 통과"
decomposition:
  - {{ unit: u1, desc: "이동", deps: [] }}
open_questions: []
""".format(order=ORDER)

SPEC_STRONG = """\
spec_id: sim-001
version: 2
order_raw: "{order}"
goal: "에이전트 이동 시뮬레이션 (연속공간 충돌 금지)"
task_type: feature_impl
verifiability: objective
mode: normal
constraints: []
acceptance_criteria:
  - id: ac1
    desc: "연속 좌표 최소거리 겹침 없음 (고밀도 100+ 에이전트)"
    check: {{ type: test, cmd: "pytest collision_dense" }}
assumptions: []
non_goals: []
done_when: "ac1 통과 (연속공간 고밀도)"
decomposition:
  - {{ unit: u1, desc: "이동", deps: [] }}
open_questions: []
""".format(order=ORDER)


def _spec(yaml_text: str) -> ProjectSpec:
    import yaml as _y

    return ProjectSpec.model_validate(_y.safe_load(yaml_text))


class _RaisingClient:
    """complete()에서 예외를 던지는 mock — critic 클라이언트 실패(잘못된 모델·codex
    다운·인증·타임아웃 등)를 흉내낸다. calls로 호출 여부를 검증할 수 있다."""

    def __init__(self, exc: Exception):
        self._exc = exc
        self.calls: list[dict] = []

    def complete(self, system: str, user: str, **opts) -> str:
        self.calls.append({"system": system, "user": user, "opts": opts})
        raise self._exc


CRIT_SOFT = """\
verdict: soft
gaps:
  - area: "ac1 충돌 검사"
    cheap_path: "격자 칸 점유만 막으면 통과 — 연속공간 고밀도 겹침은 검사 안 됨."
    strengthening: "연속 좌표 최소거리 겹침을 고밀도 시나리오에서 검사하도록 ac 명시."
"""

CRIT_ADEQUATE = "verdict: adequate\ngaps: []\n"

CRIT_BROKEN = "이건 YAML 매핑이 아니라 그냥 산문이다 — 파싱 불가"


# ──────────────────────────── critique_spec ────────────────────────────


def test_critique_spec_parses_soft_with_concrete_gap():
    client = MockClient([CRIT_SOFT])
    crit = critique_spec(ORDER, _spec(SPEC_WEAK), client, prompt_path=CRITIC_PROMPT)
    assert crit.verdict == "soft"
    assert len(crit.gaps) == 1
    assert "격자" in crit.gaps[0].cheap_path
    assert crit.gaps[0].strengthening is not None


def test_critique_spec_parses_adequate():
    client = MockClient([CRIT_ADEQUATE])
    crit = critique_spec(ORDER, _spec(SPEC_STRONG), client, prompt_path=CRITIC_PROMPT)
    assert crit.verdict == "adequate"
    assert crit.gaps == []


def test_critique_spec_absorbs_broken_output_as_adequate():
    """출력이 깨지면 crash 말고 adequate로 흡수하되 note에 기록(진행 막지 않음)."""
    client = MockClient([CRIT_BROKEN])
    crit = critique_spec(ORDER, _spec(SPEC_WEAK), client, prompt_path=CRITIC_PROMPT)
    assert crit.verdict == "adequate"
    assert crit.gaps == []
    assert crit.note is not None
    assert "평가 불가" in crit.note


def test_critique_spec_normalizes_verdict_variants():
    """'SOFT' 같은 변종도 canonical soft로, 미지 값은 adequate로 정규화."""
    client = MockClient(["verdict: SOFT\ngaps:\n  - area: x\n    cheap_path: y\n"])
    crit = critique_spec(ORDER, _spec(SPEC_WEAK), client, prompt_path=CRITIC_PROMPT)
    assert crit.verdict == "soft"

    client2 = MockClient(["verdict: maybe\ngaps: []\n"])
    crit2 = critique_spec(ORDER, _spec(SPEC_WEAK), client2, prompt_path=CRITIC_PROMPT)
    assert crit2.verdict == "adequate"


def test_critique_spec_absorbs_client_exception_as_adequate():
    """critic *클라이언트*가 던지는 예외(CodexError 등)도 crash 말고 adequate로 흡수.

    WO#19는 깨진 *출력*만 흡수했지만 클라이언트 *예외*는 전파돼 run을 죽였다(WO#20).
    이제 critique_spec은 어떤 실패든 SpecCritique를 반환하고 절대 raise하지 않는다.
    """
    from haetae.llm import CodexError

    client = _RaisingClient(CodexError("모델 'bogus'를 찾을 수 없음"))
    crit = critique_spec(ORDER, _spec(SPEC_WEAK), client, prompt_path=CRITIC_PROMPT)
    assert crit.verdict == "adequate"
    assert crit.gaps == []
    assert crit.note is not None
    assert "평가 불가" in crit.note  # surface가 "(평가 불가)"로 키잉됨
    assert "bogus" in crit.note  # 사유(원본 에러)가 기록됨
    assert len(client.calls) == 1  # 호출은 실제로 일어났음


def test_critique_spec_absorbs_generic_exception():
    """파싱/클라이언트 외 임의 예외(RuntimeError 등)도 동일하게 흡수(broad-except)."""
    client = _RaisingClient(RuntimeError("예기치 못한 내부 오류"))
    crit = critique_spec(ORDER, _spec(SPEC_WEAK), client, prompt_path=CRITIC_PROMPT)
    assert crit.verdict == "adequate"
    assert crit.gaps == []
    assert crit.note is not None
    assert "평가 불가" in crit.note


# ──────────────────── synthesize_with_critique (하이브리드) ────────────────────


def test_critic_off_when_critic_client_none():
    """critic_client=None → critic 안 돌고 (spec, None) 반환. synthesize 1회뿐."""
    client = MockClient([SPEC_WEAK])
    spec, crit = synthesize_with_critique(
        ORDER, client, None, syn_prompt_path=SYN_PROMPT, critic_prompt_path=CRITIC_PROMPT
    )
    assert crit is None
    assert spec.spec_id == "sim-001"
    assert len(client.calls) == 1  # synthesize만, critic 호출 0


def test_soft_triggers_exactly_one_resynthesis():
    """soft(구체 gap) → 정확히 1회 재합성. synthesize 2회 호출, 최종은 강화 spec."""
    client = MockClient([SPEC_WEAK, SPEC_STRONG])  # 합성 → 재합성
    critic = MockClient([CRIT_SOFT])
    spec, crit = synthesize_with_critique(
        ORDER, client, critic, syn_prompt_path=SYN_PROMPT, critic_prompt_path=CRITIC_PROMPT
    )
    assert crit.resynthesized is True
    assert spec.version == 2  # 강화 spec 채택 (원본은 version 1)
    assert "연속" in spec.acceptance_criteria[0].desc
    assert len(client.calls) == 2  # 합성 + 재합성 1회
    assert len(critic.calls) == 1
    # 재합성 호출에 비평 피드백이 얹혔는지
    assert "싸구려 충족 경로" in client.calls[1]["user"]


def test_adequate_does_not_resynthesize():
    """adequate → 재합성 없음. synthesize 1회, 원본 spec 유지."""
    client = MockClient([SPEC_STRONG])
    critic = MockClient([CRIT_ADEQUATE])
    spec, crit = synthesize_with_critique(
        ORDER, client, critic, syn_prompt_path=SYN_PROMPT, critic_prompt_path=CRITIC_PROMPT
    )
    assert crit.verdict == "adequate"
    assert crit.resynthesized is False
    assert spec.version == 2
    assert len(client.calls) == 1  # 재합성 없음


def test_resynthesis_is_bounded_to_one_shot():
    """soft여도 재합성은 1회뿐 — critic을 두 번 부르지 않고 3회차 synthesize도 없다."""
    client = MockClient([SPEC_WEAK, SPEC_WEAK])  # 합성 + 재합성(여전히 약함)
    critic = MockClient([CRIT_SOFT])  # 1회만 주입 — 두 번째 호출이 있으면 IndexError
    spec, crit = synthesize_with_critique(
        ORDER, client, critic, syn_prompt_path=SYN_PROMPT, critic_prompt_path=CRITIC_PROMPT
    )
    assert crit.resynthesized is True
    assert len(client.calls) == 2  # 정확히 2회 (3회차 없음)
    assert len(critic.calls) == 1  # critic 재호출 없음(바운드)


def test_synthesize_with_critique_absorbs_critic_client_exception():
    """critic 클라이언트가 던져도 (원본 spec, adequate critique) 반환 — crash·재합성 없음.

    첫 synthesize는 정상(SPEC_WEAK)이고 critic만 실패하는 상황. spec은 그대로,
    critique는 평가 불가(adequate)로 흡수되고 synthesize는 1회뿐(재합성 없음)."""
    from haetae.llm import CodexError

    client = MockClient([SPEC_WEAK])  # 합성만, 재합성 응답 없음(있어선 안 됨)
    critic = _RaisingClient(CodexError("codex 인증 실패"))
    spec, crit = synthesize_with_critique(
        ORDER, client, critic, syn_prompt_path=SYN_PROMPT, critic_prompt_path=CRITIC_PROMPT
    )
    assert spec.version == 1  # 원본 spec 유지
    assert crit is not None
    assert crit.verdict == "adequate"
    assert crit.resynthesized is False
    assert "평가 불가" in crit.note
    assert len(client.calls) == 1  # 재합성 없음(synthesize 1회뿐)
    assert len(critic.calls) == 1


def test_resynthesis_failure_falls_back_to_original():
    """재합성 결과가 검증 실패 → 원본 spec 폴백(crash 금지). note에 사유 기록."""
    bad_resynth = "이건 spec이 아니라 그냥 문장 — 검증 실패"
    client = MockClient([SPEC_WEAK, bad_resynth])
    critic = MockClient([CRIT_SOFT])
    spec, crit = synthesize_with_critique(
        ORDER, client, critic, syn_prompt_path=SYN_PROMPT, critic_prompt_path=CRITIC_PROMPT
    )
    # 원본(version 1) 유지, crash 없음
    assert spec.version == 1
    assert crit.resynthesized is False
    assert crit.note is not None
    assert "폴백" in crit.note or "원본" in crit.note
    assert len(client.calls) == 2  # 재합성 시도는 했음
