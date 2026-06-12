"""WO#79 — proactive anti-fixation (CRDAL co-regulation) 테스트 (mock, codex/네트워크 없음).

고착(같은 사유 반복 실패)을 *분리된 신호*(독립 gate 산출 fail 사유 + 시도 이력)로 **조기** 감지해
#41 OR / #68C ceiling 진입 *前*에 "구조적으로 다른 접근" 빌더-전용 nudge를 주입한다.
핵심: co-regulation(빌더 자기평가 아님) · 빌더 전용(judge 무주입) · 바 불변 · advisory · #41/#68C 백업.
"""

from pathlib import Path

from haetae.llm import MockClient
from haetae.loop import MockExecutor, run_loop
from haetae.models import (
    CheckReport,
    CheckType,
    GateResult,
    Status,
    Verdict,
)
from haetae.or_node import (
    build_anti_fixation_feedback,
    fixation_fail_digest,
    is_fixated,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPT_DIR = REPO_ROOT / "prompts"

_NUDGE_MARK = "고착 조기 감지"            # build_anti_fixation_feedback 첫 줄 마커
_STRUCT_MARK = "구조적으로 다른 접근"     # directive 핵심 문구


def _gr_fail(ac_id: str = "ac1", detail: str = "same problem") -> GateResult:
    return GateResult(
        verdict=Verdict.fail_recoverable,
        checks=[CheckReport(ac_id=ac_id, check_type=CheckType.run, status="fail", detail=detail)],
    )


# ════════════════════ 1. 고착 감지 (분리 신호·결정적) — 순수 함수 ════════════════════


def test_digest_from_failing_checks_is_deterministic():
    d1 = fixation_fail_digest(_gr_fail("ac6"))
    d2 = fixation_fail_digest(_gr_fail("ac6", detail="다른 detail이라도"))
    d3 = fixation_fail_digest(_gr_fail("ac7"))
    assert d1 == d2          # 같은 실패 ac → 같은 지문(detail 변동 무관 = 분리 카테고리)
    assert d1 != d3          # 다른 실패 ac → 다른 지문(진전 신호)


def test_digest_none_when_no_failing_checks():
    """실패 사유 불명(체크 0)이면 None — 고착 판정 보류(advisory)."""
    assert fixation_fail_digest(GateResult(verdict=Verdict.fail_recoverable)) is None
    # pass 체크만 있어도(실패 0) None
    ok = GateResult(verdict=Verdict.pass_,
                    checks=[CheckReport(ac_id="ac1", check_type=CheckType.run, status="pass")])
    assert fixation_fail_digest(ok) is None


def test_is_fixated_same_reason_consecutive():
    d = fixation_fail_digest(_gr_fail("ac6"))
    assert is_fixated([d, d], 2) is True            # 2회 연속 동일 → 고착
    assert is_fixated([d], 2) is False              # 1회 → 아직 아님
    assert is_fixated([], 2) is False


def test_is_fixated_different_reason_is_progress():
    a = fixation_fail_digest(_gr_fail("ac6"))
    b = fixation_fail_digest(_gr_fail("ac7"))
    assert is_fixated([a, b], 2) is False           # 다른 사유 = 진전 → 고착 아님
    assert is_fixated([a, a, b], 2) is False         # 마지막 창이 다름


def test_is_fixated_none_breaks_and_threshold_off():
    d = fixation_fail_digest(_gr_fail("ac6"))
    assert is_fixated([None, None], 2) is False      # 불확실(None) → 고착 아님(advisory)
    assert is_fixated([d, None], 2) is False
    assert is_fixated([d, d], 1) is False            # threshold<2 → 비활성(off)
    assert is_fixated([d, d, d], 3) is True          # 임계 configurable


# ════════════════════ 2. nudge 텍스트 — 바 불변(anti-erosion) ════════════════════


def test_nudge_demands_structural_alternative():
    fb = build_anti_fixation_feedback("flow-field로 회피", "ac6: gridlock", recurrences=2)
    assert _NUDGE_MARK in fb
    assert _STRUCT_MARK in fb
    assert "2회 연속" in fb
    assert "flow-field" in fb                         # 폐기할 직전 접근 명시


def test_nudge_does_not_lower_bar():
    """nudge는 *접근*만 바꾸라 함 — acceptance_criteria/done_when 변경 금지를 명시(anti-erosion)."""
    fb = build_anti_fixation_feedback("x", "y")
    assert "acceptance_criteria / done_when은 절대 바꾸지 마라" in fb
    assert "기준 약화 금지" in fb


# ════════════════════ 3. 배선 (run_loop 병렬 — 분리 신호로 OR 前 nudge) ════════════════════


_SPEC = """\
spec_id: af-001
version: 1
order_raw: "x"
goal: "g"
task_type: feature_impl
verifiability: objective
mode: normal
constraints: []
acceptance_criteria:
  - id: ac1
    desc: "동작"
    check: { type: test, cmd: "true" }
non_goals: ["n"]
done_when: "ac1"
decomposition:
  - { unit: u1, desc: "물리 엔진 구현" }
open_questions: []
"""

_DEC = (
    "verdict: pass\naction: next_order\nrationale: build\n"
    "next_order:\n  unit: u1\n  goal: \"u1 구현\"\n  deliverable: \"요약\"\n"
)


class _RecordingBrain:
    """call#1=synthesize / 이후=replan(DEC). 모든 (system,user) 호출을 기록(주입/분리 검증)."""

    def __init__(self, spec_yaml: str = _SPEC):
        self.spec = spec_yaml
        self.n = 0
        self.calls: list[dict] = []

    def complete(self, system: str, user: str, **opts) -> str:
        self.calls.append({"system": system, "user": user})
        self.n += 1
        return self.spec if self.n == 1 else _DEC


class _SameReasonFailGate:
    """항상 *같은* ac를 fail — 같은 fail 지문 재발(고착 유발)."""

    def judge(self, result, spec, unit=None):
        return GateResult(
            verdict=Verdict.fail_recoverable,
            checks=[CheckReport(ac_id="ac1", check_type=CheckType.run, status="fail",
                                detail="same gridlock")],
        )


class _RotatingFailGate:
    """매번 *다른* ac를 fail — 진전 신호(고착 아님, nudge 미발동)."""

    def __init__(self):
        self.i = 0

    def judge(self, result, spec, unit=None):
        self.i += 1
        return GateResult(
            verdict=Verdict.fail_recoverable,
            checks=[CheckReport(ac_id=f"ac{self.i}", check_type=CheckType.run, status="fail")],
        )


class _PassExec:
    def run(self, order):
        return f"{order.unit} done"


def test_fixation_injects_structural_nudge_before_retries_exhausted(tmp_path):
    """같은 사유 2회 연속 → 3번째 재빌드 작업지시서(replan feedback)에 anti-fixation nudge 주입."""
    brain = _RecordingBrain()
    state = run_loop(
        "x", brain, executor=None, gate=_SameReasonFailGate(),
        executor_factory=lambda wt: _PassExec(), gate_factory=lambda wt: _SameReasonFailGate(),
        max_parallel=2, workdir=tmp_path, prompt_dir=PROMPT_DIR,
        unit_retries=3, or_alternatives=0,  # 재시도 충분, OR off → 재시도 구간서 nudge 검증
        fixation_threshold=2,
    )
    # 빌더(replan) 프롬프트 어딘가에 구조적 대안 nudge가 들어갔다.
    nudged = [c for c in brain.calls if _NUDGE_MARK in c["user"]]
    assert nudged, "고착 감지 후 replan feedback에 anti-fixation nudge가 주입돼야"
    assert any(_STRUCT_MARK in c["user"] for c in nudged)
    # 첫 합성/첫 replan(고착 전)엔 nudge가 없다(재시도 소진 前·진전 중엔 미발동).
    assert _NUDGE_MARK not in brain.calls[0]["user"]   # synthesize
    assert _NUDGE_MARK not in brain.calls[1]["user"]   # 첫 replan(아직 실패 0회)


def test_no_nudge_when_reasons_differ_progress(tmp_path):
    """*다른* 사유로 실패(진전 중) → 고착 아님 → nudge 미발동(advisory)."""
    brain = _RecordingBrain()
    rot = _RotatingFailGate()  # 공유 인스턴스 — 매 판정 다른 ac(진전 신호)
    run_loop(
        "x", brain, executor=None, gate=rot,
        executor_factory=lambda wt: _PassExec(), gate_factory=lambda wt: rot,
        max_parallel=2, workdir=tmp_path, prompt_dir=PROMPT_DIR,
        unit_retries=3, or_alternatives=0, fixation_threshold=2,
    )
    assert not any(_NUDGE_MARK in c["user"] for c in brain.calls)


def test_nudge_is_builder_only_not_in_synthesize(tmp_path):
    """분리: nudge는 *replan(빌더)* 프롬프트에만 — synthesize 호출엔 없다(judge 무주입과 동형)."""
    brain = _RecordingBrain()
    run_loop(
        "x", brain, executor=None, gate=_SameReasonFailGate(),
        executor_factory=lambda wt: _PassExec(), gate_factory=lambda wt: _SameReasonFailGate(),
        max_parallel=2, workdir=tmp_path, prompt_dir=PROMPT_DIR,
        unit_retries=3, or_alternatives=0, fixation_threshold=2,
    )
    # 단일 유닛 → 재합성 넛지 없음 → synthesize는 정확히 첫 호출(calls[0]). 거기엔 nudge 무주입.
    assert _NUDGE_MARK not in brain.calls[0]["user"]
    # nudge는 replan(빌더) 호출에만 존재 — judge/run-judge는 별도 client(여기선 gate=비-LLM)라 무수신.
    assert any(_NUDGE_MARK in c["user"] for c in brain.calls[1:])


def test_or_backstop_still_fires_when_fixation_unbroken(tmp_path):
    """공존: anti-fixation이 고착을 못 깨도 #41 OR 대안이 백업으로 정상 진입(approaches 기록)."""
    brain = _RecordingBrain()
    state = run_loop(
        "x", brain, executor=None, gate=_SameReasonFailGate(),
        executor_factory=lambda wt: _PassExec(), gate_factory=lambda wt: _SameReasonFailGate(),
        max_parallel=2, workdir=tmp_path, prompt_dir=PROMPT_DIR,
        unit_retries=2, or_alternatives=2, fixation_threshold=2,
    )
    # nudge도 발동했고(고착), 그래도 안 풀려 #41 OR 대안 백트래킹이 기록됐다(백업 보존).
    assert any(_NUDGE_MARK in c["user"] for c in brain.calls)
    u1_approaches = [a for a in state.approaches if a.scope == "unit:u1"]
    assert u1_approaches, "#41 OR 대안이 백업으로 진입해 approach가 기록돼야"
    assert state.status is Status.escalated  # 끝내 못 풀면 escalate(백업 경로)


def test_back_compat_pass_path_no_nudge(tmp_path):
    """무회귀: 정상 통과 경로엔 고착 감지·nudge 없음(이력 미사용)."""
    brain = _RecordingBrain()

    class _PassGate:
        def judge(self, result, spec, unit=None):
            return GateResult(verdict=Verdict.pass_)

    # 통과 → 머지 → done. (PassExec가 빈 변경이라도 gate pass면 머지 시도; 단일 유닛은 충돌 없음.)
    state = run_loop(
        "x", brain, executor=None, gate=_PassGate(),
        executor_factory=lambda wt: _PassExec(), gate_factory=lambda wt: _PassGate(),
        max_parallel=2, workdir=tmp_path, prompt_dir=PROMPT_DIR,
        unit_retries=2, or_alternatives=1, fixation_threshold=2,
    )
    assert not any(_NUDGE_MARK in c["user"] for c in brain.calls)


def test_threshold_off_disables_detection(tmp_path):
    """fixation_threshold<2 → 감지 비활성(off): 같은 사유 반복해도 nudge 미발동(기존 동작)."""
    brain = _RecordingBrain()
    run_loop(
        "x", brain, executor=None, gate=_SameReasonFailGate(),
        executor_factory=lambda wt: _PassExec(), gate_factory=lambda wt: _SameReasonFailGate(),
        max_parallel=2, workdir=tmp_path, prompt_dir=PROMPT_DIR,
        unit_retries=3, or_alternatives=0, fixation_threshold=1,  # off
    )
    assert not any(_NUDGE_MARK in c["user"] for c in brain.calls)


def test_sequential_path_unaffected_back_compat():
    """무회귀: 순차(max_parallel=1) 경로는 anti-fixation 무관 — 정상 done."""
    client = MockClient([_SPEC, _DEC, "verdict: done\naction: stop\nrationale: done\n"])
    state = run_loop(
        order="x", client=client, executor=MockExecutor("ok"),
        gate=_PassGateSeq(), prompt_dir=PROMPT_DIR,
    )
    assert state.status is Status.done


class _PassGateSeq:
    def judge(self, result, spec, unit=None):
        return GateResult(verdict=Verdict.pass_) if unit is None else GateResult(verdict=Verdict.pass_)
