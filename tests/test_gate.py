"""CheckRunner 테스트 — LLM/codex 없이 사소한 셸 명령(true/false)으로 검증."""

import pytest

from haetae.gate import CheckRunner
from haetae.loop import Gate
from haetae.models import ProjectSpec, Verdict


def _spec(acs: list[dict]) -> ProjectSpec:
    """주어진 acceptance_criteria로 최소 ProjectSpec을 만든다."""
    return ProjectSpec.model_validate(
        {
            "spec_id": "gate-001",
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


# ──────────────────────────── 집계 규칙 ────────────────────────────


def test_all_pass(tmp_path):
    spec = _spec(
        [
            {"id": "ac1", "desc": "d", "check": {"type": "test", "cmd": "true"}},
            {"id": "ac2", "desc": "d", "check": {"type": "build", "cmd": "true"}},
        ]
    )
    g = CheckRunner(workdir=tmp_path)
    gr = g.judge("noop", spec)
    assert gr.verdict is Verdict.pass_
    # 근거는 반환 계약(GateResult.checks)에 동봉된다.
    assert [c.ac_id for c in gr.checks] == ["ac1", "ac2"]
    assert all(c.status == "pass" for c in gr.checks)
    assert all(c.exit_code == 0 for c in gr.checks)
    assert gr.checks[0].cmd == "true"


def test_one_fail_is_fail_recoverable(tmp_path):
    spec = _spec(
        [
            {"id": "ac1", "desc": "d", "check": {"type": "test", "cmd": "true"}},
            {"id": "ac2", "desc": "d", "check": {"type": "test", "cmd": "false"}},
        ]
    )
    g = CheckRunner(workdir=tmp_path)
    gr = g.judge("noop", spec)
    assert gr.verdict is Verdict.fail_recoverable
    # per-check 보고에 exit code가 기록되는지
    failed = [c for c in gr.checks if c.status == "fail"][0]
    assert failed.ac_id == "ac2"
    assert failed.exit_code != 0


def test_unevaluatable_human_is_ambiguous(tmp_path):
    spec = _spec(
        [
            {"id": "ac1", "desc": "d", "check": {"type": "test", "cmd": "true"}},
            {"id": "ac2", "desc": "사람 확인", "check": {"type": "human"}},
        ]
    )
    g = CheckRunner(workdir=tmp_path)
    gr = g.judge("noop", spec)
    assert gr.verdict is Verdict.ambiguous
    skipped = [c for c in gr.checks if c.status == "skipped"][0]
    assert skipped.ac_id == "ac2"
    assert skipped.check_type.value == "human"


def test_judge_type_is_skipped(tmp_path):
    spec = _spec([{"id": "ac1", "desc": "d", "check": {"type": "judge"}}])
    g = CheckRunner(workdir=tmp_path)
    assert g.judge("noop", spec).verdict is Verdict.ambiguous


def test_missing_cmd_is_skipped(tmp_path):
    # test 타입이지만 cmd가 없으면 자동 평가 불가 → skipped → ambiguous
    spec = _spec([{"id": "ac1", "desc": "d", "check": {"type": "test"}}])
    g = CheckRunner(workdir=tmp_path)
    assert g.judge("noop", spec).verdict is Verdict.ambiguous


def test_fail_dominates_skipped(tmp_path):
    # 실패와 미평가가 둘 다 있으면 실패가 우선 → fail_recoverable
    spec = _spec(
        [
            {"id": "ac1", "desc": "d", "check": {"type": "test", "cmd": "false"}},
            {"id": "ac2", "desc": "d", "check": {"type": "human"}},
        ]
    )
    g = CheckRunner(workdir=tmp_path)
    assert g.judge("noop", spec).verdict is Verdict.fail_recoverable


# ──────────────────────────── 근거(GateResult.checks) 동봉 ────────────────────────────


def test_gateresult_carries_per_check_evidence(tmp_path):
    """true/false/human 섞은 spec → checks에 각 ac의 cmd/exit_code/status가 담기고
    verdict는 기존 집계 규칙대로(실패 우선 → fail_recoverable)인지."""
    spec = _spec(
        [
            {"id": "ac1", "desc": "통과", "check": {"type": "test", "cmd": "true"}},
            {"id": "ac2", "desc": "실패", "check": {"type": "test", "cmd": "false"}},
            {"id": "ac3", "desc": "사람 확인", "check": {"type": "human"}},
        ]
    )
    g = CheckRunner(workdir=tmp_path)
    gr = g.judge("noop", spec)

    # 행동 불변: 실패가 미평가를 누른다.
    assert gr.verdict is Verdict.fail_recoverable

    # 근거가 ac 순서대로 전부 담겼는지
    by_id = {c.ac_id: c for c in gr.checks}
    assert set(by_id) == {"ac1", "ac2", "ac3"}

    assert by_id["ac1"].status == "pass"
    assert by_id["ac1"].cmd == "true"
    assert by_id["ac1"].exit_code == 0

    assert by_id["ac2"].status == "fail"
    assert by_id["ac2"].cmd == "false"
    assert by_id["ac2"].exit_code != 0

    assert by_id["ac3"].status == "skipped"
    assert by_id["ac3"].check_type.value == "human"
    assert by_id["ac3"].cmd is None
    assert by_id["ac3"].exit_code is None


# ──────────────────────────── 비정상 처리 ────────────────────────────


def test_nonexistent_command_is_fail(tmp_path):
    spec = _spec(
        [{"id": "ac1", "desc": "d", "check": {"type": "test", "cmd": "haetae_no_such_cmd_xyz"}}]
    )
    g = CheckRunner(workdir=tmp_path)
    assert g.judge("noop", spec).verdict is Verdict.fail_recoverable


def test_timeout_is_fail(tmp_path):
    spec = _spec(
        [{"id": "ac1", "desc": "d", "check": {"type": "test", "cmd": "sleep 5"}}]
    )
    g = CheckRunner(workdir=tmp_path, timeout=0.2)
    gr = g.judge("noop", spec)
    assert gr.verdict is Verdict.fail_recoverable
    assert "timeout" in gr.checks[0].detail


# ──────────────────────────── Protocol 적합성 ────────────────────────────


def test_checkrunner_satisfies_gate_protocol():
    assert isinstance(CheckRunner(), Gate)
