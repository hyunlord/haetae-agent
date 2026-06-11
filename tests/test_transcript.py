"""WO#67 — 라이브 호출 트랜스크립트(사이드카 transcripts.json) 테스트. 실제 codex/네트워크 없음.

- TranscriptWriter: 입력(head cap) + 출력 rolling tail(cap) + 런당 보관(run_cap) + best-effort + atomic.
- _event_output_text: 완료 item에서 출력 텍스트 추출(명령/편집/메시지) + 비완료/깨짐 None.
- observe_call: heartbeat+transcript 결합, 입력/출력 캡처, status(done/error), best-effort(실패가 run 안 죽임).
- codex 스트리밍 → 트랜스크립트(입력+출력 tail) 통합.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import haetae.providers.codex as codex_mod
from haetae.providers.codex import CodexClient, _event_output_text, observe_call
from haetae.transcript import TranscriptWriter


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0
        self._base = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self._base + timedelta(seconds=self.t)

    def advance(self, s: float) -> None:
        self.t += s


# ──────────────────────────── TranscriptWriter ────────────────────────────


def test_writer_records_input_and_output_with_kind_unit():
    """start가 입력(head)·kind/unit/phase 기록, output이 tail 누적."""
    writes: list[dict] = []
    w = TranscriptWriter(clock=FakeClock(), writer=writes.append, throttle=0.0)
    cid = w.start(kind="빌드", unit="u5", input_text="이 work order를 구현하라")
    w.output(cid, "$ npm test")
    w.output(cid, "3 passed")
    last = writes[-1]["calls"][0]
    assert last["kind"] == "빌드" and last["unit"] == "u5"
    assert last["phase"] is None  # unit 있으면 director-side phase 아님
    assert last["input"] == "이 work order를 구현하라"
    assert "$ npm test" in last["output_tail"] and "3 passed" in last["output_tail"]
    assert last["status"] == "active"


def test_writer_director_side_phase_is_kind_when_unitless():
    """unit 없는 director-side 호출 → phase=kind(합성/스캐폴드/replan)."""
    writes: list[dict] = []
    w = TranscriptWriter(clock=FakeClock(), writer=writes.append, throttle=0.0)
    w.start(kind="합성", unit=None, input_text="order 원문")
    c = writes[-1]["calls"][0]
    assert c["unit"] is None and c["phase"] == "합성"


def test_writer_input_head_capped():
    """입력은 head cap — 앞 N자만 + input_truncated(work order는 앞에 있으므로 머리 보존)."""
    writes: list[dict] = []
    w = TranscriptWriter(clock=FakeClock(), writer=writes.append, throttle=0.0, input_cap=10)
    w.start(kind="빌드", unit="u1", input_text="abcdefghijklmnop")
    c = writes[-1]["calls"][0]
    assert c["input"] == "abcdefghij" and c["input_truncated"] is True


def test_writer_output_rolling_tail_bounded():
    """출력은 rolling *tail* cap — 초과 시 앞부분 버리고 최근만, output_truncated + 전체 글자수."""
    writes: list[dict] = []
    w = TranscriptWriter(clock=FakeClock(), writer=writes.append, throttle=0.0, output_cap=20)
    cid = w.start(kind="빌드", unit="u1", input_text="x")
    w.output(cid, "A" * 15)
    w.output(cid, "B" * 15)  # 누적 31자 > 20 → tail 20자만
    c = writes[-1]["calls"][0]
    assert len(c["output_tail"]) <= 20
    assert c["output_tail"].endswith("B")            # 최근(tail) 보존
    assert c["output_truncated"] is True
    assert c["output_chars"] == 30                    # 본 전체 글자수는 정직히 보고


def test_writer_run_cap_evicts_oldest_finished_keeps_active():
    """런당 보관 한도 초과 시 가장 오래된 *완료* 호출 드롭, 활성은 보존."""
    writes: list[dict] = []
    w = TranscriptWriter(clock=FakeClock(), writer=writes.append, throttle=0.0, run_cap=2)
    c0 = w.start(kind="빌드", unit="u0", input_text="i")
    w.finish(c0)  # 완료
    c1 = w.start(kind="빌드", unit="u1", input_text="i")
    w.finish(c1)  # 완료
    c2 = w.start(kind="빌드", unit="u2", input_text="i")
    w.finish(c2)  # 완료 → 3개 > run_cap 2 → 가장 오래된 완료(c0) 드롭
    units = {c["unit"] for c in writes[-1]["calls"]}
    assert units == {"u1", "u2"}  # u0(가장 오래된 완료) 드롭됨


def test_writer_run_cap_does_not_drop_active():
    """전부 활성이면 run_cap 초과여도 드롭 안 함(지금 보임 보존)."""
    writes: list[dict] = []
    w = TranscriptWriter(clock=FakeClock(), writer=writes.append, throttle=0.0, run_cap=1)
    w.start(kind="빌드", unit="a", input_text="i")
    a1 = w.start(kind="빌드", unit="b", input_text="i")
    w.finish(a1)  # 완료 1, 활성 1 → 2개지만 활성은 못 버리고 완료(b)부터, 그래도 활성 a는 남음
    units = {c["unit"] for c in writes[-1]["calls"]}
    assert "a" in units  # 활성은 절대 안 버림


def test_writer_finish_sets_status():
    writes: list[dict] = []
    w = TranscriptWriter(clock=FakeClock(), writer=writes.append, throttle=0.0)
    cid = w.start(kind="judge", unit="u2", input_text="i")
    w.finish(cid, "done")
    assert writes[-1]["calls"][0]["status"] == "done"


def test_writer_best_effort_write_failure_does_not_raise():
    """쓰기 콜백이 터져도 start/output/finish는 절대 raise 안 함(run 안 죽임)."""
    def boom(_payload):
        raise OSError("disk full")

    w = TranscriptWriter(clock=FakeClock(), writer=boom, throttle=0.0)
    cid = w.start(kind="합성", unit=None, input_text="i")
    w.output(cid, "x")
    w.finish(cid)  # 여기까지 예외 0이면 통과


def test_writer_default_file_atomic(tmp_path):
    """기본 경로: transcripts.json을 atomic하게 쓴다(.tmp 안 남김)."""
    p = tmp_path / "transcripts.json"
    w = TranscriptWriter(p, clock=FakeClock(), throttle=0.0)
    cid = w.start(kind="합성", unit=None, input_text="order")
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["calls"][0]["kind"] == "합성"
    w.finish(cid)
    assert not (tmp_path / "transcripts.json.tmp").exists()


def test_writer_no_path_no_writer_is_noop():
    """path/writer 둘 다 없으면 인메모리 추적만 — 예외 없음."""
    w = TranscriptWriter(clock=FakeClock())
    cid = w.start(kind="replan", unit="u1", input_text="i")
    w.output(cid, "x")
    w.finish(cid)


def test_writer_output_unknown_call_id_is_noop():
    """없는 call_id로 output → 조용히 무시(무크래시)."""
    w = TranscriptWriter(clock=FakeClock())
    w.output(999, "x")  # 예외 0이면 통과


# ──────────────────────────── _event_output_text ────────────────────────────


def test_event_output_command():
    line = json.dumps({"type": "item.completed",
                       "item": {"type": "command_execution", "command": "pytest -q"}})
    assert _event_output_text(line) == "$ pytest -q"


def test_event_output_file_edit():
    line = json.dumps({"type": "item.completed",
                       "item": {"type": "file_change", "path": "src/x.ts"}})
    assert _event_output_text(line) == "[edit] src/x.ts"


def test_event_output_agent_message_text():
    line = json.dumps({"type": "item.completed",
                       "item": {"type": "agent_message", "text": "구현 완료 보고"}})
    assert _event_output_text(line) == "구현 완료 보고"


def test_event_output_reasoning_prefixed():
    line = json.dumps({"type": "item.completed",
                       "item": {"type": "reasoning", "text": "계획 수립"}})
    assert _event_output_text(line) == "[reasoning] 계획 수립"


def test_event_output_non_completed_is_none():
    """item.started 등 비완료 이벤트는 출력 텍스트 아님(중복 방지 → None)."""
    line = json.dumps({"type": "item.started",
                       "item": {"type": "command_execution", "command": "x"}})
    assert _event_output_text(line) is None


def test_event_output_bad_json_and_turn_events_none():
    assert _event_output_text("not json") is None
    assert _event_output_text("") is None
    assert _event_output_text(json.dumps({"type": "turn.completed", "usage": {}})) is None


# ──────────────────────────── observe_call ────────────────────────────


class FakeTranscript:
    """observe_call이 호출하는 start/output/finish를 기록하는 스파이(duck-typed)."""

    def __init__(self):
        self.started = None
        self.outputs: list[str] = []
        self.finished = None
        self._n = 0

    def start(self, *, kind, unit, input_text):
        self.started = (kind, unit, input_text)
        return 7

    def output(self, cid, text):
        self.outputs.append((cid, text))

    def finish(self, cid, status="done"):
        self.finished = (cid, status)


def test_observe_call_captures_input_output_and_done():
    """observe_call: 입력 기록 + on_output로 출력 캡처 + 정상 종료 시 status done."""
    tr = FakeTranscript()

    def run_fn(on_event, on_output):
        on_event("running: x")
        on_output("출력 한 조각")
        return ("결과", None)

    out = observe_call(None, tr, "합성", 0.5, "받은 프롬프트", run_fn)
    assert out == ("결과", None)
    assert tr.started == ("합성", None, "받은 프롬프트")  # heartbeat None → default_kind
    assert tr.outputs == [(7, "출력 한 조각")]
    assert tr.finished == (7, "done")


def test_observe_call_marks_error_status_on_exception():
    """run_fn이 던지면 transcript status=error로 남기고 예외 전파."""
    tr = FakeTranscript()

    def run_fn(on_event, on_output):
        raise RuntimeError("boom")

    try:
        observe_call(None, tr, "빌드", None, "p", run_fn)
        assert False, "예외가 전파되어야 함"
    except RuntimeError:
        pass
    assert tr.finished == (7, "error")


def test_observe_call_best_effort_transcript_failure_does_not_kill_run():
    """트랜스크립트 start/output/finish가 터져도 run_fn 결과는 그대로 반환(run 안 죽임)."""
    class BoomTranscript:
        def start(self, **k): raise OSError("nope")
        def output(self, *a): raise OSError("nope")
        def finish(self, *a, **k): raise OSError("nope")

    def run_fn(on_event, on_output):
        on_output("x")  # tr_id None이라 no-op
        return "ok"

    assert observe_call(None, BoomTranscript(), "합성", None, "p", run_fn) == "ok"


def test_observe_call_reads_context_from_heartbeat():
    """kind/unit은 heartbeat 컨텍스트에서 읽는다(루프가 set_context로 깔아둔 것)."""
    tr = FakeTranscript()

    class HB:
        def get_context(self): return ("빌드", "u9")
        def start(self, *a, **k): return 0
        def beat(self, *a, **k): pass
        def finish(self, *a, **k): pass

    observe_call(HB(), tr, "기본", 0.5, "p", lambda oe, oo: None)
    assert tr.started[0] == "빌드" and tr.started[1] == "u9"


# ──────────────────── codex 스트리밍 → 트랜스크립트(통합) ────────────────────


class _FakeStdin:
    def write(self, s): pass
    def close(self): pass


class _StdoutIter:
    def __init__(self, lines): self._lines = list(lines)
    def __iter__(self):
        for ln in self._lines:
            yield ln


class FakePopen:
    def __init__(self, lines):
        self.stdin = _FakeStdin()
        self.stdout = _StdoutIter(lines)
        self.stderr = iter([])
        self.pid = 2147483640
    def wait(self, timeout=None): return 0
    def poll(self): return 0
    def kill(self): pass


def test_codex_streaming_captures_transcript(monkeypatch):
    """codex 스트리밍 이벤트 → 트랜스크립트(받은 입력 + 출력 tail) 캡처, 끝나면 status done."""
    from haetae.heartbeat import HeartbeatWriter

    lines = [
        json.dumps({"type": "thread.started"}) + "\n",
        json.dumps({"type": "item.completed",
                    "item": {"type": "agent_message", "text": "hello world 출력"}}) + "\n",
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 2}}) + "\n",
    ]

    def factory(cmd, **kwargs):
        Path(cmd[cmd.index("-o") + 1]).write_text("done", encoding="utf-8")
        return FakePopen(lines)

    monkeypatch.setattr(codex_mod.subprocess, "Popen", factory)

    writes: list[dict] = []
    hb = HeartbeatWriter(clock=FakeClock(), writer=lambda p: None, throttle=0.0)
    hb.set_context("합성", None)  # director-side 컨텍스트(루프 시뮬)
    tr = TranscriptWriter(clock=FakeClock(), writer=writes.append, throttle=0.0)
    c = CodexClient(idle_timeout=0.5, heartbeat=hb, transcript=tr)
    assert c._run("나의 합성 프롬프트") == "done"

    final = writes[-1]["calls"][-1]
    assert final["kind"] == "합성" and final["unit"] is None and final["phase"] == "합성"
    assert final["input"] == "나의 합성 프롬프트"          # 받은 입력
    assert "hello world 출력" in final["output_tail"]     # 실시간 출력 tail
    assert final["status"] == "done"                       # 호출 끝 → 완료 기록
