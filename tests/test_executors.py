"""Executor 테스트 — 실제 codex/stdin 없이 주입/monkeypatch."""

from types import SimpleNamespace

import pytest

import haetae.providers.codex as codex_mod
from haetae.executors import (
    SENTINEL,
    CodexExecutor,
    CodexExecutorError,
    HumanRelayExecutor,
    format_work_order,
    _stdin_collect,
)
from haetae.loop import Executor
from haetae.models import Check, CheckType, NextOrder


def _order() -> NextOrder:
    return NextOrder(
        unit="u1",
        goal="로그인 폼 구현",
        scope="폼만. 소셜 로그인 제외",
        context_refs=["spec.ac1", "state.u0"],
        local_checks=[Check(type=CheckType.test, cmd="pytest test_login")],
        executor="codex",
        deliverable="변경 파일 목록 + 요약",
    )


def test_run_presents_order_and_returns_collected():
    captured: list[str] = []
    ex = HumanRelayExecutor(present=captured.append, collect=lambda: "사람이 돌린 결과")
    result = ex.run(_order())
    assert result == "사람이 돌린 결과"
    # 제시된 텍스트에 핵심 필드가 포함됐는지
    assert len(captured) == 1
    text = captured[0]
    assert "로그인 폼 구현" in text
    assert "pytest test_login" in text
    assert "폼만. 소셜 로그인 제외" in text
    assert SENTINEL in text


def test_format_work_order_handles_minimal_order():
    minimal = NextOrder(unit="u9", goal="최소 주문")
    text = format_work_order(minimal)
    assert "u9" in text
    assert "최소 주문" in text


def test_humanrelay_satisfies_executor_protocol():
    assert isinstance(HumanRelayExecutor(), Executor)


def test_stdin_collect_reads_until_sentinel(monkeypatch):
    import io

    monkeypatch.setattr(
        "sys.stdin", io.StringIO(f"line1\nline2\n{SENTINEL}\nline3\n")
    )
    assert _stdin_collect() == "line1\nline2"


def test_stdin_collect_reads_until_eof(monkeypatch):
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO("only\nlines\n"))
    assert _stdin_collect() == "only\nlines"


# ──────────────────────────── CodexExecutor (자율 쓰기) ────────────────────────────


def test_codexexecutor_satisfies_executor_protocol():
    assert isinstance(CodexExecutor(), Executor)


def test_codexexecutor_run_passes_work_order_as_prompt(monkeypatch):
    """run(order)가 work order 텍스트를 프롬프트로 _run에 넘기고, 캡처 출력을 반환."""
    ex = CodexExecutor(workdir="/tmp/scratch")
    seen = {}

    def fake_run(prompt):
        seen["p"] = prompt
        return "구현 완료 보고"

    monkeypatch.setattr(ex, "_run", fake_run)

    result = ex.run(_order())

    assert result == "구현 완료 보고"
    p = seen["p"]
    # work order의 핵심 필드가 프롬프트에 포함됐는지
    assert "로그인 폼 구현" in p
    assert "pytest test_login" in p
    # 자율 실행 지시가 덧붙었는지
    assert "이 작업 디렉토리에서" in p


def test_codexexecutor_uses_write_sandbox_and_workdir_cwd(monkeypatch):
    """_run이 만든 codex 명령에 workspace-write sandbox + cwd=workdir(-C)가 있는지,
    그리고 위험 플래그(full-access/danger)는 절대 없는지 단언."""
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        out_path = cmd[cmd.index("-o") + 1]
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("ok")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(codex_mod.subprocess, "run", fake_run)

    CodexExecutor(workdir="/tmp/scratch-xyz")._run("프롬프트")
    cmd = seen["cmd"]

    # 가장 좁은 쓰기 sandbox
    assert cmd[cmd.index("-s") + 1] == "workspace-write"
    # 실행 범위 = workdir
    assert cmd[cmd.index("-C") + 1] == "/tmp/scratch-xyz"
    # 위험 모드는 절대 사용 안 함
    assert "danger-full-access" not in cmd
    assert "--dangerously-bypass-approvals-and-sandbox" not in cmd
    assert "--full-auto" not in cmd


def test_codexexecutor_omits_model_when_unset(monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        out_path = cmd[cmd.index("-o") + 1]
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("ok")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(codex_mod.subprocess, "run", fake_run)
    CodexExecutor()._run("p")
    assert "-m" not in seen["cmd"]


def test_codexexecutor_captures_usage_into_last_usage(monkeypatch):
    """WO#33: codex executor도 --json stdout usage를 읽어 last_usage에 싣는다(읽기만)."""
    import json

    usage_jsonl = json.dumps(
        {"type": "turn.completed", "usage": {"input_tokens": 5000, "output_tokens": 700}}
    )

    def fake_run(cmd, **kwargs):
        out_path = cmd[cmd.index("-o") + 1]
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("구현 완료")
        return SimpleNamespace(returncode=0, stdout=usage_jsonl, stderr="")

    monkeypatch.setattr(codex_mod.subprocess, "run", fake_run)
    ex = CodexExecutor(model="gpt-test", workdir="/tmp/x")
    assert ex._run("p") == "구현 완료"
    assert ex.last_usage is not None
    assert ex.last_usage.input_tokens == 5000
    assert ex.last_usage.output_tokens == 700
    assert ex.last_usage.model == "gpt-test"


def test_codexexecutor_no_usage_sets_last_usage_none(monkeypatch):
    """usage 미노출이면 last_usage=None — 날조하지 않는다."""

    def fake_run(cmd, **kwargs):
        out_path = cmd[cmd.index("-o") + 1]
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("ok")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(codex_mod.subprocess, "run", fake_run)
    ex = CodexExecutor(workdir="/tmp/x")
    ex._run("p")
    assert ex.last_usage is None


def test_codexexecutor_raises_on_nonzero_exit(monkeypatch):
    def fake_run(cmd, **kwargs):
        return SimpleNamespace(returncode=2, stdout="", stderr="boom")

    monkeypatch.setattr(codex_mod.subprocess, "run", fake_run)
    with pytest.raises(CodexExecutorError):
        CodexExecutor(workdir="/tmp/x")._run("p")


def test_codexexecutor_raises_on_empty_output(monkeypatch):
    def fake_run(cmd, **kwargs):
        # -o 파일을 안 써서 빈 출력
        return SimpleNamespace(returncode=0, stdout="done", stderr="")

    monkeypatch.setattr(codex_mod.subprocess, "run", fake_run)
    with pytest.raises(CodexExecutorError):
        CodexExecutor(workdir="/tmp/x")._run("p")


def test_codexexecutor_adds_reasoning_effort_flag_when_set(monkeypatch):
    """WO#38: reasoning_effort 설정 시 `-c model_reasoning_effort=<effort>`가 cmd에 포함."""
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        out_path = cmd[cmd.index("-o") + 1]
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("ok")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(codex_mod.subprocess, "run", fake_run)
    CodexExecutor(workdir="/tmp/x", reasoning_effort="xhigh")._run("p")
    cmd = seen["cmd"]
    # 확인된 형식: codex exec엔 -e 전용 플래그가 없어 -c config override로 넘긴다.
    assert "-c" in cmd
    assert cmd[cmd.index("-c") + 1] == "model_reasoning_effort=xhigh"
    # 추론강도는 sandbox 권한과 무관 — 쓰기 sandbox 그대로(가드 불변).
    assert cmd[cmd.index("-s") + 1] == "workspace-write"
    assert "danger-full-access" not in cmd


def test_codexexecutor_omits_reasoning_effort_when_unset(monkeypatch):
    """미설정(기본)이면 플래그 미부착 → codex 기본(medium) 그대로(기존 동작 불변)."""
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        out_path = cmd[cmd.index("-o") + 1]
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("ok")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(codex_mod.subprocess, "run", fake_run)
    CodexExecutor(workdir="/tmp/x")._run("p")
    cmd = seen["cmd"]
    assert "-c" not in cmd
    assert "model_reasoning_effort" not in " ".join(cmd)


def test_codexexecutor_rejects_bad_reasoning_effort(monkeypatch):
    """화이트리스트 밖 추론강도는 ValueError로 거부(subprocess 미실행)."""
    def fake_run(cmd, **kwargs):  # 호출되면 안 됨
        raise AssertionError("subprocess should not run for rejected reasoning_effort")

    monkeypatch.setattr(codex_mod.subprocess, "run", fake_run)
    with pytest.raises(ValueError):
        CodexExecutor(workdir="/tmp/x", reasoning_effort="ultra")._run("p")


def test_codexexecutor_rejects_danger_sandbox(monkeypatch):
    """방어선: danger-full-access를 sandbox로 주면 plumbing이 ValueError로 거부."""
    def fake_run(cmd, **kwargs):  # 호출되면 안 됨
        raise AssertionError("subprocess should not run for rejected sandbox")

    monkeypatch.setattr(codex_mod.subprocess, "run", fake_run)
    ex = CodexExecutor(workdir="/tmp/x", sandbox="danger-full-access")
    with pytest.raises(ValueError):
        ex._run("p")


# ──────────────────────────── (선택) 실제 codex 통합 ────────────────────────────


@pytest.mark.skipif(
    __import__("shutil").which("codex") is None
    or __import__("os").environ.get("HAETAE_CODEX_IT") != "1",
    reason="실제 codex 통합 테스트는 opt-in (HAETAE_CODEX_IT=1 + codex 설치 시에만)",
)
def test_codexexecutor_integration_creates_file(tmp_path):
    # 임시 workdir에서 CodexExecutor가 실제 파일 하나를 만드는지(쓰기 sandbox 검증).
    order = NextOrder(
        unit="it1",
        goal="이 디렉토리에 hello.txt 파일을 만들고 'hi'라고 써라.",
        deliverable="만든 파일명",
    )
    ex = CodexExecutor(workdir=str(tmp_path))
    ex.run(order)
    assert (tmp_path / "hello.txt").exists()
