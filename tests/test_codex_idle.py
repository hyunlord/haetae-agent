"""WO#54 — codex 호출 idle(무진행) timeout 테스트 (실제 codex 안 부름).

핵심 단언:
  - 이벤트가 계속 오는 *느린* 호출 → idle 안 걸림(완료) — 총 시간이 아니라 침묵만 잼.
  - *무음* 호출 → idle 초과 → 멈춘 프로세스 정리(좀비 없음) + CodexStalled, 무한 hang 없음.
  - 필수 경로는 bounded 재시도 후 전파, idle_timeout=None은 기존 subprocess.run 경로(무회귀).
  - 스트리밍 readline이 부분 출력에서도 usage 정상 파싱(#33–34 무회귀).
"""

import json
import threading
import time
from pathlib import Path

import pytest

import haetae.providers.codex as codex_mod
from haetae.providers.codex import (
    ALLOWED_SANDBOXES,
    CodexStalled,
    _stream_codex,
    exec_codex_with_usage,
)


# ──────────────────────────── 가짜 codex 프로세스 ────────────────────────────


class _FakeStdin:
    def __init__(self):
        self.written = []

    def write(self, s):
        self.written.append(s)

    def close(self):
        pass


class _StdoutIter:
    """proc.stdout 흉내 — 줄을 (선택 지연과 함께) 차례로 흘리고, stall이면 마지막에 막힌다.

    delay: 줄 사이 지연(느린-진행 시뮬레이션). stall: 줄을 다 흘린 뒤 stop 이벤트까지 블록
    (행 멈춘 codex = 무음). kill()이 stop을 set하면 블록이 풀려 generator가 끝난다(EOF).
    """

    def __init__(self, lines, delay, stall, stop):
        self._lines = list(lines)
        self._delay = delay
        self._stall = stall
        self._stop = stop

    def __iter__(self):
        for ln in self._lines:
            if self._delay:
                time.sleep(self._delay)
            yield ln
        if self._stall:
            self._stop.wait()
        return


class FakePopen:
    """subprocess.Popen 대역. 줄 스트리밍/지연/무음/종료코드/`-o` 최종메시지를 스크립트한다."""

    def __init__(
        self,
        *,
        lines=(),
        delay=0.0,
        stall=False,
        returncode=0,
        out_message=None,
        stderr_text="",
    ):
        self._stop = threading.Event()
        self.stdin = _FakeStdin()
        self.stdout = _StdoutIter(lines, delay, stall, self._stop)
        self.stderr = iter([stderr_text] if stderr_text else [])
        self.pid = 2147483646  # 존재하지 않을 법한 pid
        self._returncode = returncode
        self.killed = False
        self.waited = False
        self.kwargs = None
        self._out_message = out_message

    # subprocess.Popen 인터페이스 ----------------------------------------
    def poll(self):
        return self._returncode if self.killed else None

    def wait(self, timeout=None):
        self.waited = True
        return self._returncode

    def kill(self):
        self.killed = True
        self._returncode = -9
        self._stop.set()  # 막힌 reader 풀기


def _install_fake_popen(monkeypatch, fake_or_factory):
    """codex_mod.subprocess.Popen를 가짜로. cmd의 `-o`에 out_message를 써준다(있으면)."""

    def factory(cmd, **kwargs):
        fp = fake_or_factory() if callable(fake_or_factory) else fake_or_factory
        fp.kwargs = kwargs
        if fp._out_message is not None and "-o" in cmd:
            Path(cmd[cmd.index("-o") + 1]).write_text(fp._out_message, encoding="utf-8")
        return fp

    monkeypatch.setattr(codex_mod.subprocess, "Popen", factory)


def _silence_real_kill(monkeypatch):
    """테스트에서 진짜 시그널이 안 나가게: getpgid가 ProcessLookupError → proc.kill() 폴백."""

    def boom(_pid):
        raise ProcessLookupError()

    monkeypatch.setattr(codex_mod.os, "getpgid", boom)


_CMD = ["codex", "exec", "--json", "-o", "/tmp/x", "-"]


# ──────────────────────────── 느린-진행은 안 죽음 (핵심) ────────────────────────────


def test_progressing_slow_call_not_killed(monkeypatch):
    """이벤트가 계속(느리게) 오는 호출은 총 시간이 idle을 넘겨도 안 죽고 완료된다.

    5줄을 각 0.1s 간격으로(총 ~0.5s) 흘림. idle_timeout=0.4s — 줄 간격(0.1s)<idle이라
    절대 idle 안 걸림(총 0.5s>idle인데도). ← '총 시간 cap 아님, idle만 잼'의 증명.
    """
    lines = [f'{{"type":"event","i":{i}}}\n' for i in range(5)]
    fp = FakePopen(lines=lines, delay=0.1, returncode=0)
    _install_fake_popen(monkeypatch, fp)

    rc, out, err = _stream_codex(_CMD, "prompt", idle_timeout=0.4, max_duration=None)

    assert rc == 0
    assert out.count('"type":"event"') == 5  # 모든 이벤트 수신
    assert fp.killed is False  # 진행 중이라 절대 안 죽임
    assert fp.waited is True  # 정상 종료 거둠


# ──────────────────────────── 무음은 idle로 잡힘 + 정리 ────────────────────────────


def test_silent_call_idle_timeout_kills_and_raises(monkeypatch):
    """무음(이벤트 0) 호출 → idle 초과 → 멈춘 프로세스 정리(좀비 없음) + CodexStalled."""
    fp = FakePopen(lines=[], stall=True)  # 아무 이벤트도 안 옴 → 영원히 침묵
    _install_fake_popen(monkeypatch, fp)
    _silence_real_kill(monkeypatch)

    t0 = time.monotonic()
    with pytest.raises(CodexStalled) as ei:
        _stream_codex(_CMD, "prompt", idle_timeout=0.15, max_duration=None)
    elapsed = time.monotonic() - t0

    assert "무진행" in str(ei.value) or "idle" in str(ei.value)
    assert fp.killed is True  # 멈춘 프로세스 kill
    assert fp.waited is True  # wait로 거둠 → 좀비 없음
    assert elapsed < 2.0  # 무한 hang 없음(즉시 잡힘)


def test_idle_fires_after_last_event_not_total(monkeypatch):
    """이벤트가 몇 개 오다 끊기면(부분 진행 후 무음) 마지막 이벤트 이후 idle을 잰다."""
    fp = FakePopen(lines=['{"type":"a"}\n', '{"type":"b"}\n'], delay=0.05, stall=True)
    _install_fake_popen(monkeypatch, fp)
    _silence_real_kill(monkeypatch)

    with pytest.raises(CodexStalled) as ei:
        _stream_codex(_CMD, "prompt", idle_timeout=0.15, max_duration=None)
    # 부분 출력은 보존(디버깅용 stdout tail)
    assert '"type":"a"' in ei.value.stdout
    assert fp.killed and fp.waited


def test_start_new_session_passed_for_child_cleanup(monkeypatch):
    """Popen에 start_new_session=True를 넘겨 멈춤 시 killpg가 *자식까지* 잡게 한다."""
    fp = FakePopen(lines=['{"type":"x"}\n'], returncode=0)
    _install_fake_popen(monkeypatch, fp)
    _stream_codex(_CMD, "prompt", idle_timeout=0.4, max_duration=None)
    assert fp.kwargs.get("start_new_session") is True


# ──────────────────────────── max_duration 절대 backstop ────────────────────────────


def test_max_duration_backstop_kills_even_when_progressing(monkeypatch):
    """진행 중이어도 절대 시간(max_duration) 초과면 차단(pathological 대비)."""
    lines = [f'{{"i":{i}}}\n' for i in range(50)]
    fp = FakePopen(lines=lines, delay=0.05)  # 계속 진행하지만 길게
    _install_fake_popen(monkeypatch, fp)
    _silence_real_kill(monkeypatch)

    with pytest.raises(CodexStalled) as ei:
        _stream_codex(_CMD, "prompt", idle_timeout=1.0, max_duration=0.15)
    assert "max_duration" in str(ei.value)
    assert fp.killed and fp.waited


# ──────────────────────────── bounded 재시도 (필수 경로) ────────────────────────────


def test_stall_retries_retries_then_raises(monkeypatch):
    """stall_retries=1이면 무음 호출을 2번 시도(1+재시도1) 후 CodexStalled 전파."""
    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        return FakePopen(lines=[], stall=True)

    _install_fake_popen(monkeypatch, factory)
    _silence_real_kill(monkeypatch)

    with pytest.raises(CodexStalled):
        codex_mod._run_streaming_with_retries(
            _CMD, "p", idle_timeout=0.1, max_duration=None, stall_retries=1
        )
    assert calls["n"] == 2  # 1 + 재시도 1


def test_stall_retries_zero_raises_immediately(monkeypatch):
    """stall_retries=0(best-effort)이면 첫 멈춤에서 즉시 전파(1회 시도)."""
    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        return FakePopen(lines=[], stall=True)

    _install_fake_popen(monkeypatch, factory)
    _silence_real_kill(monkeypatch)

    with pytest.raises(CodexStalled):
        codex_mod._run_streaming_with_retries(
            _CMD, "p", idle_timeout=0.1, max_duration=None, stall_retries=0
        )
    assert calls["n"] == 1


# ──────────────────── 스트리밍 경로 usage 파싱 (#33–34 무회귀) ────────────────────


_USAGE_JSONL_LINES = [
    json.dumps({"type": "thread.started", "thread_id": "x"}) + "\n",
    json.dumps({"type": "turn.started"}) + "\n",
    json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "4"}}) + "\n",
    json.dumps(
        {"type": "turn.completed", "usage": {"input_tokens": 22370, "output_tokens": 268}}
    ) + "\n",
]


def test_streaming_path_parses_usage_from_streamed_events(monkeypatch):
    """exec_codex_with_usage(idle_timeout 설정) → 스트리밍으로 받은 JSONL서 usage 정상 파싱."""
    fp = FakePopen(lines=_USAGE_JSONL_LINES, delay=0.02, returncode=0, out_message="4")
    _install_fake_popen(monkeypatch, fp)

    text, usage = exec_codex_with_usage(
        "p", sandbox="read-only", cwd=None, model="gpt-test", idle_timeout=0.5
    )
    assert text == "4"
    assert usage is not None
    assert usage.input_tokens == 22370
    assert usage.output_tokens == 268
    assert usage.model == "gpt-test"


def test_streaming_path_nonzero_exit_raises_codex_error(monkeypatch):
    """스트림이 끝나고 exit!=0이면 기존처럼 CodexError(스트리밍 경로도 동일)."""
    from haetae.providers.codex import CodexError

    fp = FakePopen(lines=['{"type":"x"}\n'], returncode=1, stderr_text="boom\n")
    _install_fake_popen(monkeypatch, fp)
    with pytest.raises(CodexError) as ei:
        exec_codex_with_usage("p", sandbox="read-only", cwd=None, idle_timeout=0.5)
    assert "boom" in str(ei.value)


# ──────────────────── idle_timeout=None → 기존 subprocess.run 경로(무회귀) ────────────────────


def test_idle_none_uses_subprocess_run_not_popen(monkeypatch):
    """idle_timeout 미설정(None)이면 Popen이 아니라 기존 subprocess.run 경로(테스트 seam 보존)."""
    from types import SimpleNamespace

    def fake_run(cmd, **kwargs):
        Path(cmd[cmd.index("-o") + 1]).write_text("ok", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def boom_popen(*a, **k):
        raise AssertionError("idle_timeout=None인데 Popen이 호출됨 — subprocess.run 경로여야 함")

    monkeypatch.setattr(codex_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(codex_mod.subprocess, "Popen", boom_popen)
    text, _ = exec_codex_with_usage("p", sandbox="read-only", cwd=None)  # idle_timeout=None 기본
    assert text == "ok"


# ──────────────────────────── 안전 불변 ────────────────────────────


def test_allowed_sandboxes_unchanged_under_idle():
    """WO#54: idle 경로를 더해도 sandbox 화이트리스트는 절대 불변(danger-full-access 금지)."""
    assert ALLOWED_SANDBOXES == ("read-only", "workspace-write")
    assert "danger-full-access" not in ALLOWED_SANDBOXES


def test_streaming_path_still_rejects_bad_sandbox(monkeypatch):
    """스트리밍 경로에서도 sandbox 가드는 그대로(허용 밖이면 ValueError, Popen 미호출)."""
    def boom_popen(*a, **k):
        raise AssertionError("거부된 sandbox인데 Popen 호출됨")

    monkeypatch.setattr(codex_mod.subprocess, "Popen", boom_popen)
    with pytest.raises(ValueError):
        exec_codex_with_usage(
            "p", sandbox="danger-full-access", cwd=None, idle_timeout=0.5
        )
