"""LLMJudge + CompositeGate 테스트 — codex/네트워크 없이 MockClient로 완전 검증."""

from pathlib import Path

from haetae.gate import CheckRunner, CompositeGate
from haetae.judge import LLMJudge
from haetae.llm import MockClient
from haetae.loop import Gate
from haetae.models import ProjectSpec, Verdict

REPO_ROOT = Path(__file__).resolve().parents[1]
JUDGE_PROMPT = REPO_ROOT / "prompts" / "judge.md"


def _spec(acs: list[dict]) -> ProjectSpec:
    return ProjectSpec.model_validate(
        {
            "spec_id": "judge-001",
            "version": 1,
            "order_raw": "x",
            "goal": "g",
            "task_type": "feature_impl",
            "verifiability": "judge",
            "mode": "normal",
            "acceptance_criteria": acs,
            "non_goals": ["a", "b"],
            "done_when": "전부 통과",
        }
    )


def _judge(client, workdir) -> LLMJudge:
    return LLMJudge(client, workdir=workdir, prompt_path=JUDGE_PROMPT)


# ──────────────────────────── LLMJudge ────────────────────────────


def test_judge_parses_pass_and_fail(tmp_path):
    (tmp_path / "out.txt").write_text("결과 산출물", encoding="utf-8")
    spec = _spec(
        [
            {"id": "ac1", "desc": "UI가 깔끔한가", "check": {"type": "judge"}},
            {"id": "ac2", "desc": "문서가 충분한가", "check": {"type": "judge"}},
        ]
    )
    resp = """\
verdicts:
  - ac_id: ac1
    status: fail
    reason: "여백이 불균일함"
  - ac_id: ac2
    status: pass
    reason: "README에 사용법이 명확함"
"""
    client = MockClient(resp)
    reports = _judge(client, tmp_path).judge_criteria(
        spec.acceptance_criteria, "결과 요약", spec
    )
    by_id = {r.ac_id: r for r in reports}
    assert by_id["ac1"].status == "fail"
    assert by_id["ac1"].check_type.value == "judge"
    assert by_id["ac1"].cmd is None
    assert by_id["ac1"].exit_code is None
    assert "여백" in by_id["ac1"].detail
    assert by_id["ac2"].status == "pass"


def test_judge_includes_file_contents_in_prompt(tmp_path):
    (tmp_path / "app.py").write_text("def health(): return 200", encoding="utf-8")
    spec = _spec([{"id": "ac1", "desc": "엔드포인트 구현", "check": {"type": "judge"}}])
    client = MockClient("verdicts:\n  - ac_id: ac1\n    status: pass\n    reason: ok\n")
    _judge(client, tmp_path).judge_criteria(spec.acceptance_criteria, "요약", spec)

    # judge가 실제 파일 내용을 프롬프트(user)에 실었는지 — self-report만 믿지 않는다는 증거.
    user = client.calls[0]["user"]
    assert "def health(): return 200" in user
    assert "app.py" in user
    assert "ac1" in user
    # 적대적 시스템 프롬프트가 실렸는지
    assert "리뷰어" in client.calls[0]["system"]


def test_judge_broken_output_is_skipped_no_crash(tmp_path):
    spec = _spec([{"id": "ac1", "desc": "d", "check": {"type": "judge"}}])
    client = MockClient("이건 YAML이 아니라 그냥 산문 응답입니다. {깨짐")
    reports = _judge(client, tmp_path).judge_criteria(
        spec.acceptance_criteria, "요약", spec
    )
    assert len(reports) == 1
    assert reports[0].status == "skipped"
    assert reports[0].detail == "judge 평가 불가"


def test_judge_missing_ac_in_output_is_skipped(tmp_path):
    # judge가 ac2 판정을 빠뜨림 → ac2만 skipped, ac1은 정상 평가.
    spec = _spec(
        [
            {"id": "ac1", "desc": "d", "check": {"type": "judge"}},
            {"id": "ac2", "desc": "d", "check": {"type": "judge"}},
        ]
    )
    client = MockClient("verdicts:\n  - ac_id: ac1\n    status: pass\n    reason: ok\n")
    reports = _judge(client, tmp_path).judge_criteria(
        spec.acceptance_criteria, "요약", spec
    )
    by_id = {r.ac_id: r for r in reports}
    assert by_id["ac1"].status == "pass"
    assert by_id["ac2"].status == "skipped"
    assert by_id["ac2"].detail == "judge 평가 불가"


def test_judge_empty_acs_no_call(tmp_path):
    client = MockClient("nope")
    reports = _judge(client, tmp_path).judge_criteria([], "요약", _spec([]))
    assert reports == []
    assert client.calls == []  # 호출 0회


# ──────────────────────────── CompositeGate 라우팅 ────────────────────────────


def test_composite_routes_machine_and_judge(tmp_path):
    # 기계(false→fail) + judge(MockClient→pass) 혼합 → 두 보고 다 담기고 verdict 집계.
    spec = _spec(
        [
            {"id": "ac1", "desc": "테스트", "check": {"type": "test", "cmd": "false"}},
            {"id": "ac2", "desc": "품질", "check": {"type": "judge"}},
        ]
    )
    client = MockClient("verdicts:\n  - ac_id: ac2\n    status: pass\n    reason: 좋음\n")
    gate = CompositeGate(
        workdir=tmp_path, judge_client=client, judge_prompt_path=JUDGE_PROMPT
    )
    gr = gate.judge("결과", spec)

    by_id = {c.ac_id: c for c in gr.checks}
    assert set(by_id) == {"ac1", "ac2"}
    assert by_id["ac1"].status == "fail"  # 기계 체크
    assert by_id["ac1"].exit_code != 0
    assert by_id["ac2"].status == "pass"  # judge
    assert by_id["ac2"].check_type.value == "judge"
    # 기계 fail이 judge pass를 누른다 → fail_recoverable
    assert gr.verdict is Verdict.fail_recoverable
    assert client.calls  # judge가 호출됨


def test_composite_all_pass(tmp_path):
    spec = _spec(
        [
            {"id": "ac1", "desc": "테스트", "check": {"type": "test", "cmd": "true"}},
            {"id": "ac2", "desc": "품질", "check": {"type": "judge"}},
        ]
    )
    client = MockClient("verdicts:\n  - ac_id: ac2\n    status: pass\n    reason: 좋음\n")
    gate = CompositeGate(
        workdir=tmp_path, judge_client=client, judge_prompt_path=JUDGE_PROMPT
    )
    gr = gate.judge("결과", spec)
    assert gr.verdict is Verdict.pass_


def test_composite_judge_fail_is_recoverable(tmp_path):
    spec = _spec(
        [
            {"id": "ac1", "desc": "테스트", "check": {"type": "test", "cmd": "true"}},
            {"id": "ac2", "desc": "품질", "check": {"type": "judge"}},
        ]
    )
    client = MockClient("verdicts:\n  - ac_id: ac2\n    status: fail\n    reason: 미흡\n")
    gate = CompositeGate(
        workdir=tmp_path, judge_client=client, judge_prompt_path=JUDGE_PROMPT
    )
    gr = gate.judge("결과", spec)
    assert gr.verdict is Verdict.fail_recoverable


def test_composite_preserves_spec_order(tmp_path):
    spec = _spec(
        [
            {"id": "ac1", "desc": "품질1", "check": {"type": "judge"}},
            {"id": "ac2", "desc": "테스트", "check": {"type": "test", "cmd": "true"}},
            {"id": "ac3", "desc": "품질2", "check": {"type": "judge"}},
        ]
    )
    client = MockClient(
        "verdicts:\n"
        "  - ac_id: ac1\n    status: pass\n    reason: a\n"
        "  - ac_id: ac3\n    status: pass\n    reason: c\n"
    )
    gate = CompositeGate(
        workdir=tmp_path, judge_client=client, judge_prompt_path=JUDGE_PROMPT
    )
    gr = gate.judge("결과", spec)
    assert [c.ac_id for c in gr.checks] == ["ac1", "ac2", "ac3"]


# ────────────── 기계 전용 spec → judge 안 불림 + CheckRunner 무회귀 ──────────────


def test_composite_machine_only_does_not_call_judge(tmp_path):
    # judge 타입 기준이 없으면 MockClient는 호출 0회여야 한다(비용/행동 불변).
    spec = _spec(
        [
            {"id": "ac1", "desc": "d", "check": {"type": "test", "cmd": "true"}},
            {"id": "ac2", "desc": "d", "check": {"type": "build", "cmd": "false"}},
        ]
    )
    client = MockClient("절대_호출되면_안됨")
    gate = CompositeGate(
        workdir=tmp_path, judge_client=client, judge_prompt_path=JUDGE_PROMPT
    )
    gr = gate.judge("결과", spec)

    assert client.calls == []  # judge 0회
    # CheckRunner와 동일 verdict(무회귀)
    runner_gr = CheckRunner(workdir=tmp_path).judge("결과", spec)
    assert gr.verdict is runner_gr.verdict is Verdict.fail_recoverable


def test_composite_no_judge_client_judge_type_skipped(tmp_path):
    # judge_client=None이면 judge 타입은 _run_check→skipped (기존 CheckRunner와 동일).
    spec = _spec(
        [
            {"id": "ac1", "desc": "d", "check": {"type": "test", "cmd": "true"}},
            {"id": "ac2", "desc": "품질", "check": {"type": "judge"}},
        ]
    )
    gate = CompositeGate(workdir=tmp_path, judge_client=None)
    gr = gate.judge("결과", spec)
    assert gr.verdict is Verdict.ambiguous  # judge→skipped → ambiguous
    runner_gr = CheckRunner(workdir=tmp_path).judge("결과", spec)
    assert gr.verdict is runner_gr.verdict


def test_composite_machine_only_matches_checkrunner_pass(tmp_path):
    spec = _spec([{"id": "ac1", "desc": "d", "check": {"type": "test", "cmd": "true"}}])
    client = MockClient("절대_호출되면_안됨")
    gate = CompositeGate(workdir=tmp_path, judge_client=client)
    gr = gate.judge("결과", spec)
    assert client.calls == []
    assert gr.verdict is Verdict.pass_


# ──────────────────────────── Protocol 적합성 ────────────────────────────


def test_composite_satisfies_gate_protocol():
    assert isinstance(CompositeGate("."), Gate)
