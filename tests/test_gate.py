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
    assert g.judge("noop", spec) is Verdict.pass_
    assert all(e["status"] == "pass" for e in g.last_report)


def test_one_fail_is_fail_recoverable(tmp_path):
    spec = _spec(
        [
            {"id": "ac1", "desc": "d", "check": {"type": "test", "cmd": "true"}},
            {"id": "ac2", "desc": "d", "check": {"type": "test", "cmd": "false"}},
        ]
    )
    g = CheckRunner(workdir=tmp_path)
    assert g.judge("noop", spec) is Verdict.fail_recoverable
    # per-check 보고에 exit code가 기록되는지
    failed = [e for e in g.last_report if e["status"] == "fail"][0]
    assert failed["ac_id"] == "ac2"
    assert failed["exit_code"] != 0


def test_unevaluatable_human_is_ambiguous(tmp_path):
    spec = _spec(
        [
            {"id": "ac1", "desc": "d", "check": {"type": "test", "cmd": "true"}},
            {"id": "ac2", "desc": "사람 확인", "check": {"type": "human"}},
        ]
    )
    g = CheckRunner(workdir=tmp_path)
    assert g.judge("noop", spec) is Verdict.ambiguous
    skipped = [e for e in g.last_report if e["status"] == "skipped"][0]
    assert skipped["ac_id"] == "ac2"


def test_judge_type_is_skipped(tmp_path):
    spec = _spec([{"id": "ac1", "desc": "d", "check": {"type": "judge"}}])
    g = CheckRunner(workdir=tmp_path)
    assert g.judge("noop", spec) is Verdict.ambiguous


def test_missing_cmd_is_skipped(tmp_path):
    # test 타입이지만 cmd가 없으면 자동 평가 불가 → skipped → ambiguous
    spec = _spec([{"id": "ac1", "desc": "d", "check": {"type": "test"}}])
    g = CheckRunner(workdir=tmp_path)
    assert g.judge("noop", spec) is Verdict.ambiguous


def test_fail_dominates_skipped(tmp_path):
    # 실패와 미평가가 둘 다 있으면 실패가 우선 → fail_recoverable
    spec = _spec(
        [
            {"id": "ac1", "desc": "d", "check": {"type": "test", "cmd": "false"}},
            {"id": "ac2", "desc": "d", "check": {"type": "human"}},
        ]
    )
    g = CheckRunner(workdir=tmp_path)
    assert g.judge("noop", spec) is Verdict.fail_recoverable


# ──────────────────────────── 비정상 처리 ────────────────────────────


def test_nonexistent_command_is_fail(tmp_path):
    spec = _spec(
        [{"id": "ac1", "desc": "d", "check": {"type": "test", "cmd": "haetae_no_such_cmd_xyz"}}]
    )
    g = CheckRunner(workdir=tmp_path)
    assert g.judge("noop", spec) is Verdict.fail_recoverable


def test_timeout_is_fail(tmp_path):
    spec = _spec(
        [{"id": "ac1", "desc": "d", "check": {"type": "test", "cmd": "sleep 5"}}]
    )
    g = CheckRunner(workdir=tmp_path, timeout=0.2)
    assert g.judge("noop", spec) is Verdict.fail_recoverable
    assert "timeout" in g.last_report[0]["detail"]


# ──────────────────────────── Protocol 적합성 ────────────────────────────


def test_checkrunner_satisfies_gate_protocol():
    assert isinstance(CheckRunner(), Gate)
