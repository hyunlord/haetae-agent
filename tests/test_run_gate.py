"""run 체크 라우팅/판정 테스트 (WO#22) — codex 없이 MockClient + true/false 셸로 검증.

- CompositeGate가 run 타입을 harness로 실행하고 judge(있으면)/booted(없으면)로 판정.
- run-judge 경로(LLMJudge.judge_run_criteria): RunEvidence 직렬화 → verdict 파싱 / 깨짐→skipped.
- RunEvidence가 CheckReport/GateResult에 실리는지(감사).
- 기존 mechanical/judge/human 라우팅과 안 충돌.
"""

from pathlib import Path

from haetae.gate import CompositeGate
from haetae.judge import LLMJudge
from haetae.llm import MockClient
from haetae.models import CheckType, ProjectSpec, RunEvidence, Verdict

REPO_ROOT = Path(__file__).resolve().parents[1]
JUDGE_PROMPT = REPO_ROOT / "prompts" / "judge.md"
RUN_JUDGE_PROMPT = REPO_ROOT / "prompts" / "run_judge.md"


def _spec(acs: list[dict]) -> ProjectSpec:
    return ProjectSpec.model_validate(
        {
            "spec_id": "run-gate-001",
            "version": 1,
            "order_raw": "x",
            "goal": "g",
            "task_type": "feature_impl",
            "verifiability": "objective",
            "mode": "normal",
            "acceptance_criteria": acs,
            "non_goals": ["a", "b"],
            "done_when": "전부 통과",
        }
    )


def _gate(tmp_path, client=None) -> CompositeGate:
    return CompositeGate(
        workdir=tmp_path,
        judge_client=client,
        judge_prompt_path=JUDGE_PROMPT,
        run_judge_prompt_path=RUN_JUDGE_PROMPT,
        run_timeout=10,
    )


# ──────────────── graceful degrade (judge_client 없음 → pass=booted) ────────────────


def test_run_degrade_booted_passes(tmp_path):
    spec = _spec([{"id": "ac1", "desc": "부팅", "check": {"type": "run", "cmd": "true"}}])
    gr = _gate(tmp_path, client=None).judge("결과", spec)
    rep = gr.checks[0]
    assert rep.check_type is CheckType.run
    assert rep.status == "pass"  # booted(exit 0)
    assert rep.run_evidence is not None and rep.run_evidence.booted is True
    assert gr.verdict is Verdict.pass_


def test_run_degrade_crash_fails(tmp_path):
    spec = _spec([{"id": "ac1", "desc": "부팅", "check": {"type": "run", "cmd": "false"}}])
    gr = _gate(tmp_path, client=None).judge("결과", spec)
    rep = gr.checks[0]
    assert rep.status == "fail"  # 크래시(exit≠0) → 미부팅
    assert rep.run_evidence.booted is False
    assert rep.run_evidence.exit_code != 0
    assert gr.verdict is Verdict.fail_recoverable


def test_run_no_cmd_is_skipped(tmp_path):
    spec = _spec([{"id": "ac1", "desc": "x", "check": {"type": "run"}}])
    gr = _gate(tmp_path, client=None).judge("결과", spec)
    assert gr.checks[0].status == "skipped"
    assert gr.verdict is Verdict.ambiguous


# ──────────────── judge 경로 (동적 행동 판정이 booted를 덮는다) ────────────────


def test_run_judge_can_fail_even_when_booted(tmp_path):
    # cmd는 정상 부팅(true)이지만 judge가 '행동 틀림'으로 fail → fail이 채택된다.
    spec = _spec([{"id": "ac1", "desc": "에이전트가 분산 도달", "check": {"type": "run", "cmd": "true"}}])
    client = MockClient(
        "verdicts:\n  - ac_id: ac1\n    status: fail\n    reason: 콩나물로 뭉침\n"
    )
    gr = _gate(tmp_path, client=client).judge("결과", spec)
    rep = gr.checks[0]
    assert rep.status == "fail"
    assert "콩나물" in rep.detail
    assert rep.run_evidence is not None and rep.run_evidence.booted is True  # 증거는 부팅
    assert gr.verdict is Verdict.fail_recoverable
    # judge가 실제로 run-judge 프롬프트 + 트레이스 증거로 호출됐는지
    assert client.calls
    user = client.calls[0]["user"]
    assert "behavior trace" in user
    assert "booted" in user
    assert "적대적 실행 리뷰어" in client.calls[0]["system"]


def test_run_judge_pass(tmp_path):
    spec = _spec([{"id": "ac1", "desc": "행동 성립", "check": {"type": "run", "cmd": "true"}}])
    client = MockClient("verdicts:\n  - ac_id: ac1\n    status: pass\n    reason: 큐 형성 확인\n")
    gr = _gate(tmp_path, client=client).judge("결과", spec)
    assert gr.checks[0].status == "pass"
    assert gr.verdict is Verdict.pass_


def test_run_judge_broken_output_is_skipped(tmp_path):
    spec = _spec([{"id": "ac1", "desc": "x", "check": {"type": "run", "cmd": "true"}}])
    client = MockClient("이건 YAML이 아니라 산문. {깨짐")
    gr = _gate(tmp_path, client=client).judge("결과", spec)
    rep = gr.checks[0]
    assert rep.status == "skipped"
    assert rep.detail == "run judge 평가 불가"
    assert rep.run_evidence is not None  # 증거는 여전히 기록
    assert gr.verdict is Verdict.ambiguous


# ──────────────── 라우팅 비충돌 (run + judge + mechanical + human 혼합) ────────────────


def test_run_does_not_conflict_with_other_routing(tmp_path):
    (tmp_path / "f.txt").write_text("ok", encoding="utf-8")
    spec = _spec(
        [
            {"id": "ac1", "desc": "기계", "check": {"type": "test", "cmd": "true"}},
            {"id": "ac2", "desc": "실행행동", "check": {"type": "run", "cmd": "true"}},
            {"id": "ac3", "desc": "품질", "check": {"type": "judge"}},
            {"id": "ac4", "desc": "사람", "check": {"type": "human"}},
        ]
    )
    # judge는 ac3(judge)만 평가, run은 ac2를 harness+judge로.
    client = MockClient(
        "verdicts:\n"
        "  - ac_id: ac2\n    status: pass\n    reason: 트레이스 양호\n"
        "  - ac_id: ac3\n    status: pass\n    reason: 문서 충분\n"
    )
    gr = _gate(tmp_path, client=client).judge("결과", spec)
    by_id = {c.ac_id: c for c in gr.checks}
    assert [c.ac_id for c in gr.checks] == ["ac1", "ac2", "ac3", "ac4"]  # spec 순서 보존
    assert by_id["ac1"].check_type is CheckType.test and by_id["ac1"].status == "pass"
    assert by_id["ac2"].check_type is CheckType.run and by_id["ac2"].status == "pass"
    assert by_id["ac2"].run_evidence is not None
    assert by_id["ac3"].check_type is CheckType.judge and by_id["ac3"].status == "pass"
    assert by_id["ac4"].check_type is CheckType.human and by_id["ac4"].status == "skipped"
    # human skipped 하나 → ambiguous(실패는 없음)
    assert gr.verdict is Verdict.ambiguous


def test_machine_only_spec_does_not_run_harness_judge(tmp_path):
    # run/judge 타입이 없으면 judge client는 호출 0회(비용/행동 불변).
    spec = _spec([{"id": "ac1", "desc": "d", "check": {"type": "test", "cmd": "true"}}])
    client = MockClient("절대_호출되면_안됨")
    gr = _gate(tmp_path, client=client).judge("결과", spec)
    assert client.calls == []
    assert gr.verdict is Verdict.pass_


# ──────────────── LLMJudge.judge_run_criteria 직접 ────────────────


def _ac(spec: ProjectSpec, ac_id: str):
    return next(a for a in spec.acceptance_criteria if a.id == ac_id)


def test_judge_run_criteria_parses_verdict(tmp_path):
    spec = _spec([{"id": "ac1", "desc": "행동", "check": {"type": "run", "cmd": "true"}}])
    ev = RunEvidence(booted=True, exit_code=0, trace='{"queue": [0,3,1,0]}', duration_s=0.1)
    client = MockClient("verdicts:\n  - ac_id: ac1\n    status: pass\n    reason: 큐 수렴\n")
    judge = LLMJudge(client, workdir=tmp_path, run_prompt_path=RUN_JUDGE_PROMPT)
    reports = judge.judge_run_criteria([(_ac(spec, "ac1"), ev)], "요약", spec)
    assert len(reports) == 1
    r = reports[0]
    assert r.status == "pass"
    assert r.check_type is CheckType.run
    assert r.run_evidence is ev
    # 트레이스가 프롬프트에 실렸는지(self-report 아닌 실행 증거 기반)
    assert '{"queue": [0,3,1,0]}' in client.calls[0]["user"]


def test_judge_run_criteria_broken_output_skipped(tmp_path):
    spec = _spec([{"id": "ac1", "desc": "행동", "check": {"type": "run", "cmd": "true"}}])
    ev = RunEvidence(booted=True, exit_code=0, trace="t", duration_s=0.1)
    client = MockClient("산문 {깨짐")
    judge = LLMJudge(client, workdir=tmp_path, run_prompt_path=RUN_JUDGE_PROMPT)
    reports = judge.judge_run_criteria([(_ac(spec, "ac1"), ev)], "요약", spec)
    assert reports[0].status == "skipped"
    assert reports[0].run_evidence is ev


def test_judge_run_criteria_empty_no_call(tmp_path):
    client = MockClient("nope")
    judge = LLMJudge(client, workdir=tmp_path, run_prompt_path=RUN_JUDGE_PROMPT)
    assert judge.judge_run_criteria([], "요약", _spec([])) == []
    assert client.calls == []
