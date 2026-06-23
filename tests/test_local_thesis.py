"""WO#171 완전-로컬 자급 모드(새 thesis: 강 모델 0) + shadow 비교 테스트.

새 thesis: 강 모델 0, 약 로컬 모델 하나(빌더·judge·critic 전부 로컬). judge≠builder *독립 모델*
분리가 불가하므로 적대성=(A)빌더≠judge 인스턴스 분리 +(B)기계적 게이트. 약-judge 무결성은 shadow
비교로 *측정*. **gate/run_judge 판정 로직·#113 바·hollow #98·ALLOWED_SANDBOXES 불변** 단언 포함.
"""

from __future__ import annotations

import argparse
import threading
from pathlib import Path

import pytest

import haetae.providers.local_agent as la
import haetae.run as run_mod
from haetae.executors import CodexExecutor, HumanRelayExecutor
from haetae.gate import CompositeGate, aggregate_verdict
from haetae.gate_signals import (
    LLM_CHECK_TYPES,
    MECHANICAL_CHECK_TYPES,
    classify_gate_signals,
)
from haetae.llm import CodexClient, LLMClient, LocalJudgeClient, MockClient
from haetae.loop import MockGate
from haetae.models import (
    AcceptanceCriterion,
    Check,
    CheckReport,
    CheckType,
    GateResult,
    JudgeProfile,
    ProjectSpec,
    ShadowComparison,
    State,
    Status,
    Verdict,
)
from haetae.providers.codex import ALLOWED_SANDBOXES
from haetae.providers.local_agent import _BUILDER_SYSTEM, LocalAgentExecutor
from haetae.run import (
    ExecutorWiring,
    _judge_profile_note,
    _make_role_client,
    main,
    resolve_executor_wiring,
    run,
)
from haetae.shadow import ShadowComparingGate, ShadowSink, compare_verdicts
from haetae.dashboard import state_to_view

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPT_DIR = REPO_ROOT / "prompts"


def _ns(**kw) -> argparse.Namespace:
    d = dict(
        fully_local=False, brain_executor=None, judge_executor=None,
        critic_executor=None, executor="human", shadow_judge=None,
    )
    d.update(kw)
    return argparse.Namespace(**d)


class _CapturePost:
    """post_chat 모킹 — payload를 캡처하고 스크립트된 content를 돌려준다(테스트 seam)."""

    def __init__(self, content: str = "verdicts: []"):
        self.content = content
        self.calls: list[dict] = []

    def __call__(self, endpoint, payload, timeout):
        self.calls.append(payload)
        return {"choices": [{"message": {"content": self.content}}]}


def _spec(criteria) -> ProjectSpec:
    return ProjectSpec(
        spec_id="s", version=1, order_raw="o", goal="g",
        task_type="feature_impl", verifiability="judge", mode="normal",
        acceptance_criteria=criteria, non_goals=[], done_when="x",
    )


# ════════════════════ 1. 완전-로컬 라우팅 (codex-free 기본·codex 경로 보존) ════════════════════


def test_default_wiring_all_codex_human_preserved():
    """기본(플래그 없음): brain/judge/critic=codex, builder=human, shadow=None — 기존 동작 보존."""
    w = resolve_executor_wiring(_ns())
    assert (w.brain, w.builder, w.judge, w.critic, w.shadow_judge) == (
        "codex", "human", "codex", "codex", None
    )
    assert w.uses_codex and not w.fully_local


def test_fully_local_routes_all_roles_local():
    """--fully-local: brain/builder/judge/critic 전부 local(한-플래그 프리셋)."""
    w = resolve_executor_wiring(_ns(fully_local=True))
    assert (w.brain, w.builder, w.judge, w.critic) == ("local", "local", "local", "local")
    assert w.fully_local


def test_fully_local_shadow_off_zero_codex_footprint():
    """shadow OFF + 완전-로컬 = codex 흔적 0(thesis 순수)."""
    w = resolve_executor_wiring(_ns(fully_local=True))
    assert not w.uses_codex


def test_shadow_on_implies_codex_footprint():
    """--shadow-judge codex는 codex를 쓴다(측정 때만 — 적용 0이지만 흔적은 있음, 정직)."""
    w = resolve_executor_wiring(_ns(fully_local=True, shadow_judge="codex"))
    assert w.shadow_judge == "codex" and w.uses_codex


def test_explicit_role_flag_overrides_preset():
    """명시 역할 플래그가 --fully-local 프리셋을 이긴다(codex 경로 복구 가능)."""
    w = resolve_executor_wiring(_ns(fully_local=True, judge_executor="codex"))
    assert w.judge == "codex" and w.uses_codex
    w2 = resolve_executor_wiring(_ns(fully_local=True, executor="codex"))
    assert w2.builder == "codex"


def test_partial_judge_local_only():
    """--judge-executor local 단독: judge만 local, 나머지 기본(codex/human)."""
    w = resolve_executor_wiring(_ns(judge_executor="local"))
    assert (w.brain, w.builder, w.judge, w.critic) == ("codex", "human", "local", "codex")


def test_make_role_client_routes_by_kind():
    """codex→CodexClient(보존), local→LocalJudgeClient(약-judge). 생성에 네트워크 없음."""
    common = dict(
        model=None, local_endpoint="http://x/v1", local_model="m",
        idle_timeout=None, max_duration=None, stall_retries=0, local_timeout=1.0,
    )
    assert isinstance(_make_role_client("codex", role="judge", **common), CodexClient)
    assert isinstance(_make_role_client("local", role="judge", **common), LocalJudgeClient)


def test_main_fully_local_constructs_no_codex(monkeypatch):
    """완전-로컬 + shadow OFF면 main()이 codex 클라이언트/executor를 *하나도* 인스턴스화하지 않는다."""
    captured: dict = {}

    def fake_run(order, **kw):
        captured.update(kw)
        captured["order"] = order
        return State(spec_ref="x", spec_version=1, status=Status.done)

    monkeypatch.setattr(run_mod, "run", fake_run)

    class _Boom:
        def __init__(self, *a, **k):
            raise AssertionError("codex 인스턴스화됨 — 완전-로컬 thesis(codex 흔적 0) 위반")

    monkeypatch.setattr(run_mod, "CodexClient", _Boom)
    monkeypatch.setattr(run_mod, "CodexExecutor", _Boom)
    rc = main(["--order", "x", "--fully-local"])
    assert rc == 0
    assert isinstance(captured["executor"], LocalAgentExecutor)
    jp = captured["judge_profile"]
    assert jp.weak_judge and jp.judge_executor == "local" and jp.shadow_judge is None
    assert captured["shadow_sink"] is None  # shadow OFF


def test_main_default_preserves_human_and_strong_judge(monkeypatch):
    """기본(플래그 없음)은 human executor + judge_profile.weak_judge=False(강-judge)·shadow None."""
    captured: dict = {}

    def fake_run(order, **kw):
        captured.update(kw)
        return State(spec_ref="x", spec_version=1, status=Status.done)

    monkeypatch.setattr(run_mod, "run", fake_run)
    rc = main(["--order", "x"])
    assert rc == 0
    assert isinstance(captured["executor"], HumanRelayExecutor)
    assert captured["judge_profile"].weak_judge is False
    assert captured["shadow_sink"] is None


def test_main_shadow_judge_wires_sink(monkeypatch):
    """--shadow-judge codex면 shadow_sink가 주입되고 judge_profile.shadow_judge='codex'."""
    captured: dict = {}

    def fake_run(order, **kw):
        captured.update(kw)
        return State(spec_ref="x", spec_version=1, status=Status.done)

    monkeypatch.setattr(run_mod, "run", fake_run)
    rc = main(["--order", "x", "--fully-local", "--shadow-judge", "codex"])
    assert rc == 0
    assert isinstance(captured["shadow_sink"], ShadowSink)
    assert captured["judge_profile"].shadow_judge == "codex"


# ════════════════════ 2. A. 빌더 ≠ judge 인스턴스 분리 ════════════════════


def test_local_judge_is_llmclient_builder_is_not():
    """약-judge=complete-only LLMClient(judge 슬롯 가능), 빌더=run-only(complete 無 → judge 못 됨)."""
    j = LocalJudgeClient("http://x/v1", "m")
    b = LocalAgentExecutor("http://x/v1", "m")
    assert isinstance(j, LLMClient) and hasattr(j, "complete")
    assert not isinstance(b, LLMClient) and not hasattr(b, "complete")
    assert hasattr(b, "run") and not hasattr(j, "run")
    assert type(j) is not type(b)  # 인스턴스 분리: 다른 클래스


def test_local_judge_distinct_seed_per_role():
    """judge·critic 역할별 고정 seed(서로·빌더와 decorrelate). 빌더는 seed 미전송(서버 기본)."""
    assert LocalJudgeClient("x", "m", role="judge").seed == 11171
    assert LocalJudgeClient("x", "m", role="critic").seed == 21171
    assert LocalJudgeClient("x", "m", role="judge").seed != LocalJudgeClient("x", "m", role="critic").seed


def test_local_judge_sends_adversarial_system_and_seed_not_builder(monkeypatch):
    """judge complete()는 caller의 *적대 프롬프트*를 system으로, seed를 payload에 — 빌더 system 아님."""
    cap = _CapturePost(content="verdicts: []")
    monkeypatch.setattr(la, "post_chat", cap)
    j = LocalJudgeClient("http://x/v1", "m", role="judge")
    out = j.complete("너는 적대적 평가자다. 미충족 이유를 찾아라.", "평가할 산출물 ...")
    p = cap.calls[0]
    sysmsg = p["messages"][0]
    assert sysmsg["role"] == "system"
    assert sysmsg["content"] == "너는 적대적 평가자다. 미충족 이유를 찾아라."
    assert sysmsg["content"] != _BUILDER_SYSTEM  # 빌더 프롬프트 아님(인스턴스 분리)
    assert p["seed"] == 11171  # 빌더(seed 미전송)와 다른 명시 시드
    assert out == "verdicts: []"


def test_local_judge_empty_on_error_degrades_no_fake_pass(monkeypatch):
    """엔드포인트 오류 → complete가 빈 문자열(가짜 pass 금지·degrade). judge.py 무변경."""
    def boom(endpoint, payload, timeout):
        raise la.LocalAgentError("endpoint down")

    monkeypatch.setattr(la, "post_chat", boom)
    j = LocalJudgeClient("http://x/v1", "m")
    assert j.complete("sys", "user") == ""  # 빈 출력 → 파싱 실패 → skipped → ambiguous
    assert j.last_error is not None and j.last_usage is None


def test_empty_judge_output_is_ambiguous_not_pass(tmp_path):
    """약-judge degrade(빈 출력)는 gate에서 ambiguous(escalate) — 가짜 pass 절대 아님(judge.py 불변)."""
    class _EmptyJudge:
        last_usage = None

        def complete(self, system, user, **o):
            return ""  # 오류/stall degrade와 동형(LocalJudgeClient의 빈 출력)

    spec = _spec([AcceptanceCriterion(id="a1", desc="품질", check=Check(type=CheckType.judge))])
    gate = CompositeGate(workdir=str(tmp_path), judge_client=_EmptyJudge())
    gr = gate.judge("result", spec)
    assert gr.verdict == Verdict.ambiguous  # skipped → ambiguous (가짜 pass 아님)


# ════════════════════ 3. B. 기계적 게이트 (결정적·바 불변·read-only 분류) ════════════════════


def test_classify_buckets_mechanical_llm_run():
    gr = GateResult(verdict=Verdict.fail_recoverable, checks=[
        CheckReport(ac_id="b", check_type=CheckType.build, status="fail", exit_code=1),
        CheckReport(ac_id="t", check_type=CheckType.test, status="pass"),
        CheckReport(ac_id="j", check_type=CheckType.judge, status="pass"),
        CheckReport(ac_id="r", check_type=CheckType.run, status="pass"),
    ])
    sp = classify_gate_signals(gr)
    assert sp.mechanical == ["b", "t"] and sp.llm == ["j"] and sp.run == ["r"]
    assert sp.mechanical_fail == ["b"]
    assert sp.fail_locus == "mechanical" and sp.mechanical_decisive


def test_classify_llm_only_fail_locus():
    gr = GateResult(verdict=Verdict.fail_recoverable, checks=[
        CheckReport(ac_id="j", check_type=CheckType.judge, status="fail"),
    ])
    sp = classify_gate_signals(gr)
    assert sp.fail_locus == "llm" and not sp.mechanical_decisive


def test_classify_is_readonly_does_not_recompute_verdict():
    gr = GateResult(verdict=Verdict.pass_, checks=[
        CheckReport(ac_id="b", check_type=CheckType.build, status="pass"),
    ])
    classify_gate_signals(gr)
    assert gr.verdict == Verdict.pass_  # 분류는 read-only — verdict 불변


def test_mechanical_check_types_are_deterministic_set():
    """기계 타입 = 결정적(빌드/테스트/lint/bench/schema), LLM 타입 = judge(주관). run은 별도(혼합)."""
    assert MECHANICAL_CHECK_TYPES == frozenset(
        {CheckType.build, CheckType.test, CheckType.lint, CheckType.bench, CheckType.schema}
    )
    assert LLM_CHECK_TYPES == frozenset({CheckType.judge})
    assert CheckType.run not in MECHANICAL_CHECK_TYPES and CheckType.run not in LLM_CHECK_TYPES


def test_mechanical_fail_vetoes_regardless_of_llm():
    """기계 fail은 약 judge pass와 무관하게 verdict를 fail로 만든다(기계 주력 — aggregate_verdict 불변)."""
    reports = [
        CheckReport(ac_id="b", check_type=CheckType.build, status="fail"),
        CheckReport(ac_id="j", check_type=CheckType.judge, status="pass"),
    ]
    assert aggregate_verdict(reports) == Verdict.fail_recoverable


def test_aggregate_verdict_rules_unchanged():
    """#113 바·집계 규칙 불변(byte-identical): pass/fail/skipped/fail-dominates."""
    t = CheckType.test
    assert aggregate_verdict([CheckReport(ac_id="a", check_type=t, status="pass")]) == Verdict.pass_
    assert aggregate_verdict([CheckReport(ac_id="a", check_type=t, status="fail")]) == Verdict.fail_recoverable
    assert aggregate_verdict([CheckReport(ac_id="a", check_type=t, status="skipped")]) == Verdict.ambiguous
    assert aggregate_verdict([
        CheckReport(ac_id="a", check_type=t, status="fail"),
        CheckReport(ac_id="b", check_type=CheckType.judge, status="skipped"),
    ]) == Verdict.fail_recoverable
    assert aggregate_verdict([]) == Verdict.pass_  # 빈 = pass(executor-ok)


# ════════════════════ 4. C. 약-judge 플래그 (state/대시보드 정직 표기) ════════════════════


def test_judge_profile_weak_flag_roundtrip():
    import yaml

    jp = JudgeProfile(judge_executor="local", weak_judge=True, shadow_judge="codex", note="약-judge")
    s = State(spec_ref="x", spec_version=1, status=Status.running, judge_profile=jp)
    s2 = State.model_validate(yaml.safe_load(yaml.safe_dump(s.model_dump(by_alias=True, mode="json"))))
    assert s2.judge_profile.weak_judge and s2.judge_profile.shadow_judge == "codex"


def test_judge_profile_note_honesty():
    assert "약-judge" in _judge_profile_note(resolve_executor_wiring(_ns(fully_local=True)))
    assert "강-judge" in _judge_profile_note(resolve_executor_wiring(_ns()))
    note = _judge_profile_note(resolve_executor_wiring(_ns(fully_local=True, shadow_judge="codex")))
    assert "shadow" in note


def test_dashboard_surfaces_judge_profile_and_shadow():
    s = State(
        spec_ref="x", spec_version=1, status=Status.running,
        judge_profile=JudgeProfile(
            judge_executor="local", weak_judge=True, shadow_judge="codex", note="약-judge"
        ),
        shadow_comparisons=[
            ShadowComparison(unit="u5", primary_verdict="pass", shadow_verdict="fail_recoverable", inverted=True, locus="llm"),
            ShadowComparison(unit="u6", primary_verdict="pass", shadow_verdict="pass", inverted=False),
        ],
    )
    view = state_to_view(s)
    assert view["judge_profile"]["weak_judge"] is True
    assert view["judge_profile"]["judge_executor"] == "local"
    assert view["shadow"]["enabled"] is True
    assert view["shadow"]["comparisons"] == 2 and view["shadow"]["inversions"] == 1
    assert view["shadow"]["inversion_rate"] == 0.5
    assert view["shadow"]["items"][0]["locus"] == "llm"


def test_dashboard_no_profile_is_backcompat():
    """구버전/강-judge state: judge_profile=None·shadow 비활성(무크래시)."""
    view = state_to_view(State(spec_ref="x", spec_version=1, status=Status.running))
    assert view["judge_profile"] is None
    assert view["shadow"]["enabled"] is False
    assert view["shadow"]["comparisons"] == 0 and view["shadow"]["inversion_rate"] is None


# ════════════════════ 5. shadow 비교 (적용 0·검증역전 누적) ════════════════════


class _FixedGate:
    """스크립트된 verdict(+checks)를 돌려주는 Gate(shadow 단위 테스트용)."""

    def __init__(self, verdict, checks=None):
        self.verdict = verdict
        self.checks = checks or []

    def judge(self, result, spec, unit=None):
        return GateResult(verdict=self.verdict, checks=list(self.checks))


def test_shadow_applies_primary_records_shadow_inversion():
    """약(primary) verdict가 적용되고 강(shadow)은 기록만 — 둘이 달라도 적용은 primary(적용 0)."""
    sink = ShadowSink()
    primary = _FixedGate(Verdict.pass_)
    shadow = _FixedGate(Verdict.fail_recoverable, [CheckReport(ac_id="j", check_type=CheckType.judge, status="fail")])
    gate = ShadowComparingGate(primary, shadow, sink)
    gr = gate.judge("r", None, unit="u5")
    assert gr.verdict == Verdict.pass_  # 적용 = primary(약). shadow는 기록만.
    snap = sink.snapshot()
    assert len(snap) == 1
    assert snap[0].inverted and snap[0].locus == "llm" and snap[0].unit == "u5"
    assert snap[0].primary_verdict == "pass" and snap[0].shadow_verdict == "fail_recoverable"


def test_shadow_no_inversion_when_agree():
    sink = ShadowSink()
    ShadowComparingGate(_FixedGate(Verdict.pass_), _FixedGate(Verdict.pass_), sink).judge("r", None)
    assert not sink.snapshot()[0].inverted


def test_shadow_exception_does_not_change_applied_verdict():
    """shadow 판정 예외는 흡수 — 적용 verdict(약)는 절대 안 바뀐다(적용 0·관측이 run 안 죽임)."""
    class _Boom:
        def judge(self, result, spec, unit=None):
            raise RuntimeError("shadow judge down")

    sink = ShadowSink()
    gate = ShadowComparingGate(_FixedGate(Verdict.pass_), _Boom(), sink)
    assert gate.judge("r", None).verdict == Verdict.pass_
    assert sink.snapshot() == []  # 예외로 기록 0 — 그래도 적용 verdict 정상


def test_shadow_primary_exception_propagates():
    """primary(적용) 판정 자체의 예외는 그대로 전파(관측 래퍼가 정상 실패 경로를 안 가린다)."""
    class _Boom:
        def judge(self, result, spec, unit=None):
            raise RuntimeError("primary down")

    gate = ShadowComparingGate(_Boom(), _FixedGate(Verdict.pass_), ShadowSink())
    with pytest.raises(RuntimeError, match="primary down"):
        gate.judge("r", None)


def test_compare_verdicts_only_weak_pass_strong_fail_is_inversion():
    p = GateResult(verdict=Verdict.pass_)
    s_fail = GateResult(verdict=Verdict.fail_recoverable, checks=[
        CheckReport(ac_id="j", check_type=CheckType.judge, status="fail")
    ])
    assert compare_verdicts(p, s_fail, "u1").inverted          # 약pass·강fail = 역전
    assert not compare_verdicts(p, p, "u1").inverted           # 둘 다 pass = 역전 아님
    assert not compare_verdicts(s_fail, p, "u1").inverted      # 약fail·강pass = 역전 아님(약이 더 엄격)


def test_shadow_sink_thread_safe():
    sink = ShadowSink()

    def add_many():
        for _ in range(100):
            sink.add(ShadowComparison(primary_verdict="pass", shadow_verdict="pass"))

    ts = [threading.Thread(target=add_many) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(sink.snapshot()) == 800


# ════════════════════ run_loop 통합: judge_profile·shadow가 state로 흐른다 ════════════════════

SPEC_YAML = """\
spec_id: lt-001
version: 1
order_raw: "전투 추가"
goal: "전투 시스템 추가"
task_type: feature_impl
verifiability: objective
mode: normal
constraints: []
acceptance_criteria:
  - id: ac1
    desc: "컴포넌트 등록"
    check: { type: test, cmd: "true" }
assumptions: []
non_goals: ["x"]
done_when: "ac1 통과"
decomposition:
  - { unit: u1, desc: "스켈레톤", deps: [] }
  - { unit: u2, desc: "로직", deps: [u1] }
open_questions: []
"""


def _next_order(unit: str) -> str:
    return f"verdict: pass\naction: next_order\nrationale: \"{unit}\"\nnext_order:\n  unit: {unit}\n  goal: \"{unit} 구현\"\n  deliverable: \"요약\"\n"


def test_run_loop_threads_judge_profile_and_drains_shadow_sink():
    """run()→run_loop이 judge_profile을 state에 박고 shadow_sink를 state.shadow_comparisons로 드레인."""
    client = MockClient([SPEC_YAML, _next_order("u1"), _next_order("u2")])
    executor = HumanRelayExecutor(present=lambda t: None, collect=lambda: "결과")
    sink = ShadowSink()
    # 약(primary)=pass→done, 강(shadow)=항상 fail → u1서 검증역전(약pass·강fail) 발생.
    primary = MockGate([Verdict.pass_, Verdict.done])
    shadow = MockGate([Verdict.fail_recoverable])
    gate = ShadowComparingGate(primary, shadow, sink)
    jp = JudgeProfile(judge_executor="local", weak_judge=True, shadow_judge="codex", note="약-judge")

    state = run(
        "전투 추가", client=client, executor=executor, gate=gate, prompt_dir=PROMPT_DIR,
        judge_profile=jp, shadow_sink=sink,
    )

    assert state.status is Status.done
    assert state.judge_profile is not None and state.judge_profile.weak_judge  # C: state에 표기
    assert len(state.shadow_comparisons) >= 1                                  # shadow 누적
    assert any(c.inverted for c in state.shadow_comparisons)                   # 검증역전 기록(약pass·강fail)


def test_run_loop_no_shadow_no_profile_backcompat():
    """judge_profile/shadow_sink 미지정(기본)이면 state.judge_profile=None·shadow 빈(무회귀)."""
    client = MockClient([SPEC_YAML, _next_order("u1"), _next_order("u2")])
    executor = HumanRelayExecutor(present=lambda t: None, collect=lambda: "결과")
    state = run("전투 추가", client=client, executor=executor,
                gate=MockGate([Verdict.pass_, Verdict.done]), prompt_dir=PROMPT_DIR)
    assert state.status is Status.done
    assert state.judge_profile is None and state.shadow_comparisons == []


# ════════════════════ 적대 분리 재정의: 불변식 단언 ════════════════════


def test_allowed_sandboxes_unchanged():
    """ALLOWED_SANDBOXES 불변(로컬 executor도 sandbox 가드 무관 — 약 judge는 네트워크-only, sandbox 무관)."""
    assert ALLOWED_SANDBOXES == ("read-only", "workspace-write")


def test_local_judge_does_not_couple_to_judgment_engine():
    """LocalJudgeClient는 판정 엔진(gate/judge/run_judge) 코드를 import/참조하지 않는다(적대 분리)."""
    src = Path(la.__file__).read_text(encoding="utf-8")
    for forbidden in ("haetae.judge", "haetae.gate", "GateResult", "run_judge"):
        assert forbidden not in src, f"local_agent이 {forbidden} 참조(분리 위반)"


def test_local_judge_plugs_into_same_judge_slot_as_codex():
    """약-judge·codex judge 둘 다 LLMClient → 같은 judge_client 슬롯에 꽂힌다(gate 판정 로직 불변)."""
    assert isinstance(LocalJudgeClient("x", "m"), LLMClient)
    assert isinstance(CodexClient(), LLMClient)
    # CompositeGate가 어느 쪽이든 그대로 받는다(슬롯 동일).
    g_local = CompositeGate(workdir=".", judge_client=LocalJudgeClient("x", "m"))
    g_codex = CompositeGate(workdir=".", judge_client=CodexClient())
    assert g_local.judge_client is not None and g_codex.judge_client is not None


def test_codex_path_preserved_executor_unchanged():
    """codex 경로 보존: CodexExecutor·CodexClient 그대로 import·생성 가능(opt-out)."""
    assert CodexExecutor is not None and CodexClient is not None
    ex = CodexExecutor(model=None, workdir=".")
    assert ex.sandbox == "workspace-write"  # 가장 좁은 쓰기 모드(불변)
