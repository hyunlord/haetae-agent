"""CodexClient 테스트 — 실제 codex를 부르지 않는다.

complete 레벨은 _run을 가짜로 바꿔 검증하고,
_run 내부 에러 처리는 모듈의 subprocess.run을 가짜로 바꿔 검증한다.
"""

import json
import os
import shutil
from types import SimpleNamespace

import pytest

import haetae.providers.codex as codex_mod
from haetae.llm import CodexClient, CodexError, LLMClient


# ──────────────────────────── complete (seam = _run) ────────────────────────────


def test_complete_returns_run_output(monkeypatch):
    c = CodexClient()
    monkeypatch.setattr(c, "_run", lambda prompt: "spec_id: x")
    assert c.complete("SYS", "USER") == "spec_id: x"


def test_complete_merges_system_and_user_into_prompt(monkeypatch):
    c = CodexClient()
    seen = {}
    monkeypatch.setattr(c, "_run", lambda prompt: seen.setdefault("p", prompt) or "ok")
    c.complete("SYSTEM-PREAMBLE", "USER-ORDER")
    p = seen["p"]
    assert "SYSTEM-PREAMBLE" in p
    assert "USER-ORDER" in p
    # system이 user보다 앞(preamble)
    assert p.index("SYSTEM-PREAMBLE") < p.index("USER-ORDER")


def test_merge_prompt_without_system():
    assert CodexClient._merge_prompt("", "only user") == "only user"


def test_codexclient_satisfies_llmclient_protocol():
    c = CodexClient()
    assert isinstance(c, LLMClient)  # runtime_checkable Protocol


# ──────────────────────────── _run 에러 처리 (seam = subprocess.run) ──────────


def test_run_raises_on_nonzero_exit(monkeypatch):
    def fake_run(cmd, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="boom")

    monkeypatch.setattr(codex_mod.subprocess, "run", fake_run)
    c = CodexClient()
    with pytest.raises(CodexError) as ei:
        c._run("prompt")
    assert "boom" in str(ei.value)


def test_run_raises_on_empty_output(monkeypatch):
    # returncode 0이지만 -o 파일을 아무도 안 써서 빈 출력 → 예외
    def fake_run(cmd, **kwargs):
        return SimpleNamespace(returncode=0, stdout="done", stderr="")

    monkeypatch.setattr(codex_mod.subprocess, "run", fake_run)
    c = CodexClient()
    with pytest.raises(CodexError):
        c._run("prompt")


def test_run_returns_final_message_file(monkeypatch):
    # fake_run이 cmd의 -o 경로에 최종 메시지를 써주면 _run이 그걸 읽어 반환.
    def fake_run(cmd, **kwargs):
        out_path = cmd[cmd.index("-o") + 1]
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("  spec_id: gen-1  \n")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(codex_mod.subprocess, "run", fake_run)
    c = CodexClient()
    assert c._run("prompt") == "spec_id: gen-1"


def test_run_uses_model_flag_when_set(monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        out_path = cmd[cmd.index("-o") + 1]
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("ok")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(codex_mod.subprocess, "run", fake_run)
    CodexClient(model="some-model")._run("prompt")
    cmd = seen["cmd"]
    assert "-m" in cmd and cmd[cmd.index("-m") + 1] == "some-model"
    # 격리 플래그 존재
    assert "--skip-git-repo-check" in cmd
    assert "--ephemeral" in cmd
    assert cmd[cmd.index("-s") + 1] == "read-only"


def test_run_omits_model_flag_when_unset(monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        out_path = cmd[cmd.index("-o") + 1]
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("ok")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(codex_mod.subprocess, "run", fake_run)
    CodexClient()._run("prompt")
    assert "-m" not in seen["cmd"]


# ──────────────────────── usage 캡처 (WO#33 — 읽기만, sandbox 불변) ────────────────────────

_USAGE_JSONL = "\n".join(
    [
        json.dumps({"type": "thread.started", "thread_id": "x"}),
        json.dumps({"type": "turn.started"}),
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "4"}}),
        json.dumps(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 22370,
                    "cached_input_tokens": 3456,
                    "output_tokens": 268,
                    "reasoning_output_tokens": 261,
                },
            }
        ),
    ]
)


def test_parse_usage_reads_turn_completed():
    """codex --json stdout의 turn.completed.usage를 파싱한다."""
    u = codex_mod._parse_usage(_USAGE_JSONL, model="gpt-test")
    assert u is not None
    assert u.input_tokens == 22370
    assert u.output_tokens == 268
    assert u.model == "gpt-test"


def test_parse_usage_no_usage_returns_none():
    """usage 라인이 없으면 None(무크래시)."""
    assert codex_mod._parse_usage("not json\n{}\n", model=None) is None
    assert codex_mod._parse_usage("", model=None) is None


def test_run_passes_json_flag_and_captures_usage(monkeypatch):
    """_run이 --json을 넘기고, stdout usage를 파싱해 last_usage에 싣는다(읽기만)."""
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        out_path = cmd[cmd.index("-o") + 1]
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("4")
        return SimpleNamespace(returncode=0, stdout=_USAGE_JSONL, stderr="")

    monkeypatch.setattr(codex_mod.subprocess, "run", fake_run)
    c = CodexClient(model="gpt-test")
    assert c._run("prompt") == "4"
    assert "--json" in seen["cmd"]
    assert c.last_usage is not None
    assert c.last_usage.input_tokens == 22370
    assert c.last_usage.output_tokens == 268
    assert c.last_usage.model == "gpt-test"


def test_run_no_usage_sets_last_usage_none(monkeypatch):
    """usage 미노출(stdout에 turn.completed 없음) → last_usage=None(날조 안 함)."""

    def fake_run(cmd, **kwargs):
        out_path = cmd[cmd.index("-o") + 1]
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("ok")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(codex_mod.subprocess, "run", fake_run)
    c = CodexClient()
    assert c._run("prompt") == "ok"
    assert c.last_usage is None


def test_allowed_sandboxes_unchanged():
    """안전 불변(WO#33 SAFETY): danger-full-access는 절대 허용 목록에 없다."""
    assert codex_mod.ALLOWED_SANDBOXES == ("read-only", "workspace-write")
    assert "danger-full-access" not in codex_mod.ALLOWED_SANDBOXES


def test_codex_tempdir_ignores_cleanup_errors():
    """WO#88: codex temp cleanup이 .omx/state 비동기 쓰기와 경합해도 크래시 안 함(ignore_cleanup_errors).
    ALLOWED_SANDBOXES·실행 로직 불변 — temp cleanup 인자만."""
    import inspect
    src = inspect.getsource(codex_mod.exec_codex_with_usage)
    assert "ignore_cleanup_errors=True" in src
    assert 'TemporaryDirectory(prefix="haetae-codex-"' in src
    # 안전 가드: 이 변경이 sandbox 화이트리스트를 건드리지 않았다
    assert codex_mod.ALLOWED_SANDBOXES == ("read-only", "workspace-write")
    assert "danger-full-access" not in codex_mod.ALLOWED_SANDBOXES


# ──────────────────── reasoning-effort (WO#38 — sandbox 권한과 무관) ────────────────────


def test_allowed_reasoning_efforts_whitelist():
    """추론 강도 화이트리스트는 codex 값과 일치하며 ALLOWED_SANDBOXES를 건드리지 않는다."""
    assert codex_mod.ALLOWED_REASONING_EFFORTS == (
        "minimal", "low", "medium", "high", "xhigh",
    )
    # 추론강도 화이트리스트는 sandbox 가드를 절대 오염시키지 않는다(불변).
    assert codex_mod.ALLOWED_SANDBOXES == ("read-only", "workspace-write")


def test_exec_codex_with_usage_rejects_bad_reasoning_effort(monkeypatch):
    """헬퍼 레벨에서도 화이트리스트 밖 추론강도를 ValueError로 거부(다중 가드)."""
    def fake_run(cmd, **kwargs):  # 호출되면 안 됨
        raise AssertionError("subprocess should not run for rejected reasoning_effort")

    monkeypatch.setattr(codex_mod.subprocess, "run", fake_run)
    with pytest.raises(ValueError):
        codex_mod.exec_codex_with_usage(
            "p", sandbox="read-only", cwd=None, reasoning_effort="bogus"
        )


def test_exec_codex_with_usage_adds_reasoning_effort_flag(monkeypatch):
    """설정 시 `-c model_reasoning_effort=<effort>`만 부착하고 sandbox는 불변."""
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        out_path = cmd[cmd.index("-o") + 1]
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("ok")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(codex_mod.subprocess, "run", fake_run)
    codex_mod.exec_codex_with_usage(
        "p", sandbox="read-only", cwd=None, reasoning_effort="high"
    )
    cmd = seen["cmd"]
    assert cmd[cmd.index("-c") + 1] == "model_reasoning_effort=high"
    assert cmd[cmd.index("-s") + 1] == "read-only"


# ──────────────────────────── (선택) 실제 codex 통합 ────────────────────────────


@pytest.mark.skipif(
    shutil.which("codex") is None or os.environ.get("HAETAE_CODEX_IT") != "1",
    reason="실제 codex 통합 테스트는 opt-in (HAETAE_CODEX_IT=1 + codex 설치 시에만)",
)
def test_codex_integration_smoke():
    # 실제 codex 한 턴: 간단한 산술만 시켜 결정성/저비용 유지.
    c = CodexClient()
    out = c.complete("You output only the final answer, nothing else.", "What is 2+2?")
    assert "4" in out
