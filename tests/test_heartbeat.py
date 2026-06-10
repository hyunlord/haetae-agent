"""WO#55 — 라이브 하트비트(사이드카 heartbeat.json) 테스트. 실제 codex/네트워크 없음.

- HeartbeatWriter: 레코드 갱신 · throttle · best-effort(쓰기 실패가 run 안 죽임) · 클리어.
- _event_summary: 액션 요약 추출 + 종류 폴백.
- codex 스트리밍 → on_event → 하트비트 레코드(call_kind/unit/summary) + 호출 끝 클리어.
- loop: director-side(합성/replan) 컨텍스트 active 기록(0 활성 아님).
- dashboard reader: heartbeat.json 렌더, 미생성 시 조용한 빈 상태(에러 아님).
"""

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import haetae.providers.codex as codex_mod
from haetae.heartbeat import HeartbeatWriter
from haetae.providers.codex import CodexClient, _event_summary

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPT_DIR = REPO_ROOT / "prompts"


class FakeClock:
    """수동 진행 시계(테스트). __call__ → timezone-aware datetime."""

    def __init__(self) -> None:
        self.t = 0.0
        self._base = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self._base + timedelta(seconds=self.t)

    def advance(self, s: float) -> None:
        self.t += s


# ──────────────────────────── HeartbeatWriter ────────────────────────────


def test_writer_records_call_kind_unit_summary():
    """start→beat가 call_kind/unit/last_event_summary를 레코드에 싣는다."""
    clock = FakeClock()
    seen: list[dict] = []
    w = HeartbeatWriter(clock=clock, writer=seen.append, throttle=1.0)
    h = w.start("빌드", "u5", idle_timeout=300.0)
    clock.advance(2.0)
    w.beat(h, "running: pytest -q")
    last = seen[-1]
    assert len(last["activities"]) == 1
    a = last["activities"][0]
    assert a["call_kind"] == "빌드"
    assert a["unit"] == "u5"
    assert a["last_event_summary"] == "running: pytest -q"
    assert a["idle_timeout"] == 300.0
    assert a["elapsed_s"] == pytest.approx(2.0, abs=0.01)
    assert a["started_at"] and a["last_event_at"]


def test_writer_throttle_coalesces_beats():
    """start/finish는 즉시(force) 쓰지만, 잦은 beat는 throttle 윈도우로 합친다."""
    clock = FakeClock()
    writes: list[dict] = []
    w = HeartbeatWriter(clock=clock, writer=writes.append, throttle=1.0)
    h = w.start("replan", "u1")        # force → write #1
    clock.advance(0.3); w.beat(h, "a") # 0.3<1 → throttled
    clock.advance(0.3); w.beat(h, "b") # 0.6<1 → throttled
    clock.advance(0.6); w.beat(h, "c") # 1.2>=1 → write #2
    w.finish(h)                        # force → write #3 (빈 활성)
    assert len(writes) == 3
    assert writes[-1]["activities"] == []


def test_writer_best_effort_write_failure_does_not_raise():
    """쓰기 콜백이 터져도 start/beat/finish는 절대 raise하지 않는다(run 안 죽임)."""
    def boom(_payload):
        raise OSError("disk full")

    w = HeartbeatWriter(clock=FakeClock(), writer=boom, throttle=0.0)
    h = w.start("합성", None)   # throttle=0 → 매번 쓰기 시도(전부 흡수돼야 함)
    w.beat(h, "x")
    w.finish(h)  # 여기까지 예외 0이면 통과


def test_writer_finish_clears_activity():
    """호출 끝(finish) → 그 활동이 사라진다(다 끝나면 빈 활성=idle 표기)."""
    clock = FakeClock()
    writes: list[dict] = []
    w = HeartbeatWriter(clock=clock, writer=writes.append, throttle=0.0)
    h = w.start("judge", "u2")
    assert writes[-1]["activities"][0]["call_kind"] == "judge"
    w.finish(h)
    assert writes[-1]["activities"] == []


def test_writer_concurrent_activities_both_present():
    """병렬: 동시 진행 호출 2개가 둘 다 활성으로 잡힌다(0 활성 아님)."""
    clock = FakeClock()
    writes: list[dict] = []
    w = HeartbeatWriter(clock=clock, writer=writes.append, throttle=0.0)
    w.start("빌드", "u3")
    w.start("빌드", "u4")
    kinds = {(a["call_kind"], a["unit"]) for a in writes[-1]["activities"]}
    assert ("빌드", "u3") in kinds and ("빌드", "u4") in kinds


def test_writer_default_file_write_atomic(tmp_path):
    """기본 경로: heartbeat.json을 실제로 쓴다(atomic). 부분 .tmp 남기지 않음."""
    p = tmp_path / "heartbeat.json"
    w = HeartbeatWriter(p, clock=FakeClock(), throttle=0.0)
    h = w.start("합성", None)
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["activities"][0]["call_kind"] == "합성"
    w.finish(h)
    assert not (tmp_path / "heartbeat.json.tmp").exists()  # tmp 정리됨(atomic replace)


def test_writer_no_path_no_writer_is_noop():
    """path/writer 둘 다 없으면 인메모리 추적만(무사이드카) — 예외 없음."""
    w = HeartbeatWriter(clock=FakeClock())
    h = w.start("replan", "u1")
    w.beat(h, "x")
    w.finish(h)  # 예외 0이면 통과


# ──────────────────────────── _event_summary ────────────────────────────


def test_event_summary_command_execution():
    line = json.dumps({"type": "item.started", "item": {"type": "command_execution", "command": "pytest -q tests"}})
    assert _event_summary(line) == "running: pytest -q tests"


def test_event_summary_file_edit():
    line = json.dumps({"type": "item.completed", "item": {"type": "file_change", "path": "src/engine/checkout.ts"}})
    assert _event_summary(line) == "editing: src/engine/checkout.ts"


def test_event_summary_agent_message():
    line = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "..."}})
    assert _event_summary(line) == "reasoning/message"


def test_event_summary_falls_back_to_type():
    """요약 못 뽑으면 이벤트 종류로 폴백(WO: 종류 폴백)."""
    assert _event_summary(json.dumps({"type": "turn.started"})) == "turn started"
    # 미지 종류 → 종류 문자열 그대로
    assert _event_summary(json.dumps({"type": "weird.kind"})) == "weird.kind"


def test_event_summary_bad_json_is_none():
    assert _event_summary("not json") is None
    assert _event_summary("") is None


# ──────────────────── codex 스트리밍 → 하트비트 레코드(통합) ────────────────────


class _FakeStdin:
    def write(self, s): pass
    def close(self): pass


class _StdoutIter:
    def __init__(self, lines): self._lines = list(lines)
    def __iter__(self):
        for ln in self._lines:
            yield ln


class FakePopen:
    def __init__(self, lines, out_message):
        self.stdin = _FakeStdin()
        self.stdout = _StdoutIter(lines)
        self.stderr = iter([])
        self.pid = 2147483640
        self._out = out_message
    def wait(self, timeout=None): return 0
    def poll(self): return 0
    def kill(self): pass


def test_codex_streaming_updates_heartbeat_and_clears(monkeypatch):
    """codex 스트리밍 이벤트 → 하트비트 레코드 갱신(요약), 호출 끝나면 클리어."""
    lines = [
        json.dumps({"type": "thread.started"}) + "\n",
        json.dumps({"type": "item.started", "item": {"type": "command_execution", "command": "npm test"}}) + "\n",
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 2}}) + "\n",
    ]

    def factory(cmd, **kwargs):
        Path(cmd[cmd.index("-o") + 1]).write_text("done", encoding="utf-8")
        return FakePopen(lines, "done")

    monkeypatch.setattr(codex_mod.subprocess, "Popen", factory)

    writes: list[dict] = []
    hb = HeartbeatWriter(clock=FakeClock(), writer=writes.append, throttle=0.0)
    hb.set_context("replan", "u7")  # director-side 컨텍스트(루프가 깔아주는 것 시뮬)
    c = CodexClient(idle_timeout=0.5, heartbeat=hb)
    assert c._run("prompt") == "done"

    # 진행 중엔 replan/u7 활성(0 활성 아님) + 이벤트마다 요약 갱신.
    mid = [w for w in writes if w["activities"]]
    assert mid, "진행 중 활성 레코드가 하나도 안 잡힘"
    assert all(
        a["call_kind"] == "replan" and a["unit"] == "u7"
        for w in mid for a in w["activities"]
    )
    summaries = [a["last_event_summary"] for w in mid for a in w["activities"]]
    assert "running: npm test" in summaries  # 명령 실행 이벤트가 요약으로 잡힘
    # 호출 끝 → 마지막 write는 빈 활성(클리어).
    assert writes[-1]["activities"] == []


# ──────────────────── loop: director-side 활성 기록(0 활성 아님) ────────────────────


SPEC_YAML = """\
spec_id: hb-001
version: 1
order_raw: "x"
goal: "g"
task_type: feature_impl
verifiability: objective
mode: normal
constraints: []
acceptance_criteria:
  - id: ac1
    desc: "d"
    check: { type: test, cmd: "t" }
assumptions: []
non_goals: ["a", "b"]
done_when: "ac1 통과"
decomposition:
  - { unit: u1, desc: "스켈레톤", deps: [] }
open_questions: []
"""

_NEXT_ORDER = """\
verdict: pass
action: next_order
rationale: "u1 진행"
next_order:
  unit: u1
  goal: "u1 구현"
  local_checks: [{ type: test, cmd: "t u1" }]
  executor: codex
  deliverable: "요약"
"""

_STOP = "verdict: done\naction: stop\nrationale: \"done_when 충족\"\n"


class SpyHeartbeat:
    """루프가 codex 호출 직전 set_context로 깐 (call_kind, unit)를 기록하는 스파이."""

    def __init__(self): self.contexts: list[tuple] = []
    def set_context(self, kind, unit): self.contexts.append((kind, unit))
    def get_context(self): return (None, None)
    def start(self, *a, **k): return 0
    def beat(self, *a, **k): pass
    def finish(self, *a, **k): pass


def test_loop_records_director_side_activity():
    """run_loop이 합성/replan/빌드/judge 코드ex 호출 직전 컨텍스트를 active로 깐다(0 활성 해소)."""
    from haetae.llm import MockClient
    from haetae.loop import MockExecutor, MockGate, run_loop
    from haetae.models import Verdict

    spy = SpyHeartbeat()
    client = MockClient([SPEC_YAML, _NEXT_ORDER, _STOP])
    run_loop(
        order="x", client=client,
        executor=MockExecutor("ok"), gate=MockGate([Verdict.pass_, Verdict.done]),
        prompt_dir=PROMPT_DIR, heartbeat=spy,
    )
    kinds = [k for (k, _u) in spy.contexts]
    # director-side(합성·replan)도 active로 잡힘 → "0 활성" 해소.
    assert "합성" in kinds
    assert "replan" in kinds
    # 빌드/judge도 unit과 함께.
    assert ("빌드", "u1") in spy.contexts
    assert ("judge", "u1") in spy.contexts


# ──────────────────── dashboard reader: heartbeat.json 렌더 / 빈 상태 ────────────────────


_STATE_MIN = "spec_ref: x\nspec_version: 1\nstatus: running\n"


def _write_heartbeat(d: Path) -> None:
    payload = {
        "updated_at": "2026-01-01T00:00:10Z",
        "activities": [
            {
                "call_kind": "빌드", "unit": "u5",
                "started_at": "2026-01-01T00:00:00Z", "last_event_at": "2026-01-01T00:00:08Z",
                "elapsed_s": 10.0, "idle_seconds": 2.0,
                "last_event_summary": "editing: src/x.ts", "idle_timeout": 300.0,
            }
        ],
    }
    (d / "heartbeat.json").write_text(json.dumps(payload), encoding="utf-8")


def test_load_view_includes_heartbeat(tmp_path):
    """state.yaml 옆 heartbeat.json이 있으면 view['heartbeat']로 동봉된다."""
    from haetae.dashboard import load_view

    (tmp_path / "state.yaml").write_text(_STATE_MIN, encoding="utf-8")
    _write_heartbeat(tmp_path)
    view = load_view(tmp_path / "state.yaml")
    assert "heartbeat" in view
    acts = view["heartbeat"]["activities"]
    assert acts[0]["call_kind"] == "빌드" and acts[0]["unit"] == "u5"
    assert acts[0]["last_event_summary"] == "editing: src/x.ts"


def test_load_view_no_heartbeat_is_quiet(tmp_path):
    """heartbeat.json 미생성 → view에 heartbeat 키 없음(에러 아님, 조용한 빈 상태)."""
    from haetae.dashboard import load_view

    (tmp_path / "state.yaml").write_text(_STATE_MIN, encoding="utf-8")
    view = load_view(tmp_path / "state.yaml")
    assert "error" not in view
    assert "heartbeat" not in view


def test_load_heartbeat_bad_json_returns_none(tmp_path):
    """깨진/부분 heartbeat.json → None(조용히, 에러 아님)."""
    from haetae.dashboard import load_heartbeat

    (tmp_path / "heartbeat.json").write_text("{partial", encoding="utf-8")
    assert load_heartbeat(tmp_path / "state.yaml") is None
    # 미생성도 None.
    assert load_heartbeat(tmp_path / "nope" / "state.yaml") is None


def test_load_view_state_error_still_surfaces_heartbeat(tmp_path):
    """state 로드 실패에도 하트비트는 별도 — 멈춤 진단 위해 함께 실린다."""
    from haetae.dashboard import load_view

    (tmp_path / "state.yaml").write_text(":::not yaml::: [", encoding="utf-8")
    _write_heartbeat(tmp_path)
    view = load_view(tmp_path / "state.yaml")
    assert "error" in view
    assert view.get("heartbeat", {}).get("activities")  # 멈춤 진단 가능
