"""WO#28 — read-only 대시보드 테스트.

핵심은 순수 변환 `state_to_view`(웹 없이). + 얇은 서버 라이트 테스트 + 엔진 무접촉 가드.
"""

from __future__ import annotations

import ast
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from haetae.dashboard import load_view, make_handler, state_to_view
from haetae.models import (
    AcceptanceCriterion,
    Activity,
    Budget,
    Check,
    CheckReport,
    CheckType,
    Cost,
    DecompositionUnit,
    Event,
    PlanItem,
    PlanState,
    ProjectSpec,
    RunEvidence,
    SpecCritique,
    SpecGap,
    StageTransition,
    State,
    Status,
    Verdict,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DASH = REPO_ROOT / "src" / "haetae" / "dashboard.py"


def _state() -> State:
    """u1 done → u2 done → u3 in_progress(deps 충족) · u4 pending(blocked by u3) · u5 failed."""
    plan = [
        PlanItem(unit="u1", state=PlanState.done, deps=None),
        PlanItem(unit="u2", state=PlanState.done, deps=["u1"]),
        PlanItem(unit="u3", state=PlanState.in_progress, deps=["u1"]),
        PlanItem(unit="u4", state=PlanState.pending, deps=["u3"]),
        PlanItem(unit="u5", state=PlanState.failed, deps=["u2"]),
    ]
    events = [
        Event(seq=1, unit="u1", work_order_ref="엔진 모듈 구현", verdict=Verdict.pass_,
              checks=[CheckReport(ac_id="ac1", check_type=CheckType.test, cmd="npm test",
                                  status="pass", exit_code=0, detail="3 passed")]),
        Event(seq=2, unit="u2", work_order_ref="충돌 불변식", verdict=Verdict.pass_,
              checks=[CheckReport(ac_id="ac2", check_type=CheckType.run, cmd="npm run sim",
                                  status="pass", exit_code=0,
                                  run_evidence=RunEvidence(booted=True, exit_code=0,
                                                           trace="tick=100 ok", duration_s=1.2))]),
        # 통합 gate(unit=None) — 1 fail 포함
        Event(seq=6, unit=None, work_order_ref="(integration)", verdict=Verdict.fail_recoverable,
              checks=[
                  CheckReport(ac_id="ac8", check_type=CheckType.run, cmd="npm run sim:trace",
                              status="fail", exit_code=1,
                              run_evidence=RunEvidence(booted=False, exit_code=1,
                                                       reason="cross-unit breakage", timed_out=False)),
                  CheckReport(ac_id="ac9", check_type=CheckType.test, cmd="npm test", status="pass"),
              ]),
    ]
    return State(
        spec_ref="retail-flow-sim-001", spec_version=2, status=Status.escalated, plan=plan,
        events=events,
        pending_escalations=[{"reason": "통합 gate 실패 — cross-unit breakage", "verdict": "fail_recoverable"}],
        spec_critique=SpecCritique(verdict="soft", resynthesized=False,
                                   gaps=[SpecGap(area="시뮬 결정론", cheap_path="고정 seed",
                                                 strengthening="다중 seed 통계 단언")]),
    )


# ──────────────────────────── 순수 변환 코어 ────────────────────────────


def test_dag_nodes_edges_and_states():
    v = state_to_view(_state())
    ids = {n["id"]: n for n in v["dag"]["nodes"]}
    assert set(ids) == {"u1", "u2", "u3", "u4", "u5"}
    assert ids["u1"]["state"] == "done" and ids["u4"]["state"] == "pending"
    assert ids["u5"]["state"] == "failed" and ids["u3"]["state"] == "in_progress"
    assert ids["u2"]["deps"] == ["u1"]
    # edges: dep→unit
    assert {"from": "u1", "to": "u2"} in v["dag"]["edges"]
    assert {"from": "u3", "to": "u4"} in v["dag"]["edges"]
    # goal 폴백 = 유닛 event work_order_ref
    assert ids["u1"]["goal"] == "엔진 모듈 구현"


def test_topo_levels():
    v = state_to_view(_state())
    levels = v["dag"]["levels"]
    assert levels[0] == ["u1"]  # deps 없는 루트
    assert "u2" in levels[1] and "u3" in levels[1]  # u1 의존 → level 1
    assert "u4" in levels[2]  # u3(lvl1) 의존 → level 2


def test_blocking_analysis():
    v = state_to_view(_state())
    blk = {b["unit"]: b["blocked_by"] for b in v["blocking"]}
    # u4 pending, dep u3 미완(in_progress) → blocked_by [u3]
    assert blk.get("u4") == ["u3"]
    # u2(done)·u3(deps u1 done)·u5(deps u2 done) → blocking 목록에 없음
    assert "u2" not in blk and "u3" not in blk and "u5" not in blk


def test_unit_checks_and_run_evidence_surfaced():
    v = state_to_view(_state())
    u2 = v["units"]["u2"]
    assert u2["verdict"] == "pass" and u2["work_order_ref"] == "충돌 불변식"
    chk = u2["checks"][0]
    assert chk["ac_id"] == "ac2" and chk["check_type"] == "run" and chk["status"] == "pass"
    assert chk["cmd"] == "npm run sim"
    ev = chk["run_evidence"]
    assert ev["booted"] is True and ev["exit_code"] == 0 and "tick=100" in ev["trace"]


def test_integration_event_surfaced():
    v = state_to_view(_state())
    assert len(v["integration"]) == 1
    it = v["integration"][0]
    assert it["seq"] == 6 and it["verdict"] == "fail_recoverable"
    assert len(it["checks"]) == 2
    failed = [c for c in it["checks"] if c["status"] == "fail"][0]
    assert failed["run_evidence"]["booted"] is False
    assert failed["run_evidence"]["reason"] == "cross-unit breakage"


def test_critique_and_escalations_and_status():
    v = state_to_view(_state())
    assert v["status"] == "escalated"
    assert v["spec_critique"]["verdict"] == "soft"
    assert v["spec_critique"]["gaps"][0]["area"] == "시뮬 결정론"
    assert v["spec_critique"]["gaps"][0]["cheap_path"] == "고정 seed"
    assert len(v["pending_escalations"]) == 1
    assert v["spec"]["spec_ref"] == "retail-flow-sim-001" and v["spec"]["spec_version"] == 2


def test_timeline_and_json_serializable():
    v = state_to_view(_state())
    seqs = [t["seq"] for t in v["timeline"]]
    assert seqs == [1, 2, 6]
    assert [t["unit"] for t in v["timeline"]][-1] is None  # 통합 event
    json.dumps(v)  # 전체 ViewModel이 JSON 직렬화 가능해야


def test_spec_enrichment_optional():
    spec = ProjectSpec(
        spec_id="s1", version=2, order_raw="o", goal="리테일 시뮬", task_type="feature_impl",
        verifiability="objective", mode="normal", constraints=["c1", "c2"], non_goals=["ng"],
        done_when="통합 통과", acceptance_criteria=[
            AcceptanceCriterion(id="ac1", desc="d", check=Check(type="test"))],
        decomposition=[DecompositionUnit(unit="u1", desc="엔진 코어", deps=[])],
    )
    v = state_to_view(_state(), spec)
    assert v["spec"]["goal"] == "리테일 시뮬" and v["spec"]["done_when"] == "통합 통과"
    assert v["spec"]["constraints_count"] == 2 and v["spec"]["ac_count"] == 1
    # spec.decomposition desc가 유닛 goal 보강
    assert {n["id"]: n["goal"] for n in v["dag"]["nodes"]}["u1"] == "엔진 코어"


# ──────────────────── v2: activity / transitions / cost (WO#35) ────────────────────


def _state_v2() -> State:
    """activity(라이브) + transitions(이력) + budget/event.cost(계측)를 가진 state."""
    plan = [
        PlanItem(unit="u1", state=PlanState.done, deps=None),
        PlanItem(unit="u2", state=PlanState.in_progress, deps=["u1"]),
        PlanItem(unit="u3", state=PlanState.in_progress, deps=["u1"]),
    ]
    events = [
        Event(seq=1, unit="u1", work_order_ref="코어", verdict=Verdict.pass_, ts="2026-06-07T14:00:10Z",
              cost=Cost(tokens=150, usd=0.0015, input=100, output=50, source="orchestration")),
        Event(seq=2, unit="u2", work_order_ref="기능", verdict=Verdict.pass_, ts="2026-06-07T14:00:20Z",
              cost=Cost(tokens=4000, usd=0.04, input=3000, output=1000, source="executor")),
        Event(seq=3, unit="u3", work_order_ref="검증", verdict=Verdict.pass_, ts="2026-06-07T14:00:25Z",
              cost=Cost(tokens=600, usd=0.006, input=500, output=100, source="judge")),
    ]
    return State(
        spec_ref="sim-001", spec_version=1, status=Status.running, plan=plan, events=events,
        budget=Budget(spent=Cost(tokens=4750, usd=0.0475, input=3600, output=1150),
                      cap=Cost(usd=5.0)),
        activity=[
            Activity(unit="u2", stage="build", started_at="2026-06-07T14:00:15Z"),
            Activity(unit="u3", stage="verify", started_at="2026-06-07T14:00:24Z"),
        ],
        transitions=[
            StageTransition(stage="synthesize", unit=None, ts="2026-06-07T14:00:00Z"),
            StageTransition(stage="build", unit="u1", ts="2026-06-07T14:00:05Z"),
            StageTransition(stage="verify", unit="u1", ts="2026-06-07T14:00:09Z"),
            StageTransition(stage="build", unit="u2", ts="2026-06-07T14:00:15Z"),
        ],
    )


def test_activity_surfaced_with_elapsed():
    v = state_to_view(_state_v2(), now="2026-06-07T14:00:30Z")
    act = {a["unit"]: a for a in v["activity"]}
    assert set(act) == {"u2", "u3"}
    assert act["u2"]["stage"] == "build"
    assert act["u3"]["stage"] == "verify"
    # 경과 = now - started_at
    assert act["u2"]["elapsed_s"] == 15.0  # 14:00:30 - 14:00:15
    assert act["u3"]["elapsed_s"] == 6.0


def test_activity_empty_when_absent():
    v = state_to_view(_state())  # 구버전 state엔 activity 없음
    assert v["activity"] == []


def test_activity_bad_timestamp_elapsed_none():
    s = State(spec_ref="x", spec_version=1, status=Status.running,
              activity=[Activity(unit="u1", stage="build", started_at="not-a-ts")])
    v = state_to_view(s, now="2026-06-07T14:00:30Z")
    assert v["activity"][0]["elapsed_s"] is None  # 파싱 실패 → null(날조 금지)


def test_dag_node_and_unit_carry_current_stage():
    v = state_to_view(_state_v2(), now="2026-06-07T14:00:30Z")
    nodes = {n["id"]: n for n in v["dag"]["nodes"]}
    assert nodes["u2"]["stage"] == "build"   # in_progress → 현재 단계 배지
    assert nodes["u3"]["stage"] == "verify"
    assert nodes["u1"]["stage"] is None      # done → 단계 없음
    assert v["units"]["u2"]["stage"] == "build"


def test_transitions_surfaced_in_order():
    v = state_to_view(_state_v2())
    stages = [(t["stage"], t["unit"]) for t in v["transitions"]]
    assert stages == [
        ("synthesize", None), ("build", "u1"), ("verify", "u1"), ("build", "u2"),
    ]
    assert v["transitions"][0]["ts"] == "2026-06-07T14:00:00Z"


def test_transitions_empty_when_absent():
    v = state_to_view(_state())
    assert v["transitions"] == []


def test_cost_total_from_budget():
    v = state_to_view(_state_v2())
    assert v["cost"]["total"]["tokens"] == 4750
    assert v["cost"]["total"]["usd"] == 0.0475
    assert v["cost"]["total"]["input"] == 3600
    assert v["cost"]["cap"]["usd"] == 5.0


def test_cost_by_source_aggregation():
    v = state_to_view(_state_v2())
    bs = v["cost"]["by_source"]
    assert bs["orchestration"]["tokens"] == 150
    assert bs["executor"]["tokens"] == 4000
    assert bs["judge"]["tokens"] == 600
    assert bs["judge"]["usd"] == 0.006
    assert bs["orchestration"]["count"] == 1


def test_cost_by_unit_aggregation():
    v = state_to_view(_state_v2())
    bu = v["cost"]["by_unit"]
    assert bu["u1"]["tokens"] == 150
    assert bu["u2"]["tokens"] == 4000
    assert bu["u3"]["tokens"] == 600


def test_cost_usd_null_is_tokens_only():
    """usd 미상(구독 codex/미상 모델) → usd=None 보존(가짜 비용 금지, 프런트가 '—')."""
    s = State(spec_ref="x", spec_version=1, status=Status.running,
              budget=Budget(spent=Cost(tokens=1000, usd=None, input=700, output=300)),
              events=[Event(seq=1, unit="u1", cost=Cost(tokens=1000, usd=None, source="executor"))])
    v = state_to_view(s)
    assert v["cost"]["total"]["tokens"] == 1000
    assert v["cost"]["total"]["usd"] is None
    assert v["cost"]["by_source"]["executor"]["usd"] is None
    assert v["cost"]["by_source"]["executor"]["tokens"] == 1000


def test_cost_empty_when_no_data():
    v = state_to_view(_state())  # 구버전: event.cost/budget.spent 비어있음
    assert v["cost"]["total"]["tokens"] is None
    assert v["cost"]["by_source"] == {}


def test_v2_view_json_serializable():
    json.dumps(state_to_view(_state_v2(), now="2026-06-07T14:00:30Z"))


# ──────────────────────────── 서버 라이트 ────────────────────────────


def _serve(tmp_state: Path):
    handler = make_handler(str(tmp_state), None, 2000)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


def _get(port: int, path: str) -> tuple[int, str]:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
        return r.status, r.read().decode("utf-8")


def test_server_api_state_returns_view(tmp_path: Path):
    sp = tmp_path / "s.yaml"
    sp.write_text(_state_yaml(), encoding="utf-8")
    httpd, port = _serve(sp)
    try:
        code, body = _get(port, "/api/state")
        assert code == 200
        v = json.loads(body)
        assert v["status"] == "escalated" and len(v["dag"]["nodes"]) == 5
    finally:
        httpd.shutdown()


def test_server_root_returns_html(tmp_path: Path):
    sp = tmp_path / "s.yaml"
    sp.write_text(_state_yaml(), encoding="utf-8")
    httpd, port = _serve(sp)
    try:
        code, body = _get(port, "/")
        assert code == 200 and "<html" in body.lower()
        assert "2000" in body  # poll_ms 템플릿 치환됨
    finally:
        httpd.shutdown()


def test_server_missing_file_absorbed(tmp_path: Path):
    httpd, port = _serve(tmp_path / "nope.yaml")
    try:
        code, body = _get(port, "/api/state")
        assert code == 200  # 서버 안 죽음
        assert "error" in json.loads(body)  # 에러를 흡수해 200+{error}
    finally:
        httpd.shutdown()


def test_load_view_parse_error_absorbed(tmp_path: Path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("status: not_a_valid_enum_value\nplan: [\n", encoding="utf-8")  # 깨진 YAML
    v = load_view(bad)
    assert "error" in v  # 파싱 에러 흡수


# ──────────────────────── SSE 라이브 스트림 (WO#35) ────────────────────────


def _serve_stream(tmp_state: Path, stream_interval: float = 0.05):
    handler = make_handler(str(tmp_state), None, 2000, stream_interval=stream_interval)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


def _read_sse_event(resp, timeout_lines: int = 200) -> dict:
    """스트림에서 다음 `data:` 페이로드 한 건을 읽어 JSON 파싱(주석/하트비트 무시)."""
    for _ in range(timeout_lines):
        line = resp.readline()
        if not line:
            raise AssertionError("stream closed before data event")
        if line.startswith(b"data:"):
            return json.loads(line[len(b"data:"):].strip().decode("utf-8"))
    raise AssertionError("no data event within line budget")


def test_stream_headers_and_initial_event(tmp_path: Path):
    sp = tmp_path / "s.yaml"
    sp.write_text(_state_yaml(), encoding="utf-8")
    httpd, port = _serve_stream(sp)
    try:
        resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/api/stream", timeout=5)
        assert resp.headers.get("Content-Type", "").startswith("text/event-stream")
        v = _read_sse_event(resp)
        assert v["status"] == "escalated" and len(v["dag"]["nodes"]) == 5
        resp.close()
    finally:
        httpd.shutdown()


def test_stream_pushes_on_file_change(tmp_path: Path):
    import os
    import time

    sp = tmp_path / "s.yaml"
    sp.write_text(_state_yaml(), encoding="utf-8")
    httpd, port = _serve_stream(sp)
    try:
        resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/api/stream", timeout=5)
        first = _read_sse_event(resp)
        assert first["status"] == "escalated"
        # state를 done으로 바꾸고 mtime을 확실히 갱신 → 서버가 변경 감지 후 push
        done = _state()
        done.status = Status.done
        import yaml
        sp.write_text(yaml.safe_dump(done.model_dump(mode="json", by_alias=True),
                                     allow_unicode=True), encoding="utf-8")
        os.utime(sp, (time.time() + 5, time.time() + 5))
        nxt = _read_sse_event(resp)
        assert nxt["status"] == "done"
        resp.close()
    finally:
        httpd.shutdown()


def test_stream_missing_file_sends_error_event_not_crash(tmp_path: Path):
    httpd, port = _serve_stream(tmp_path / "nope.yaml")
    try:
        resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/api/stream", timeout=5)
        v = _read_sse_event(resp)
        assert "error" in v  # 파일 부재 → {error} 흡수(서버 안 죽음)
        resp.close()
    finally:
        httpd.shutdown()


# ──────────────────────────── 엔진 무접촉 가드 ────────────────────────────


def test_dashboard_does_not_import_engine_modules():
    """대시보드는 models만 의존 — loop/gate/executor/replan/scaffold 등 엔진 미import."""
    tree = ast.parse(DASH.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            for a in node.names:
                imported.add(a.name)
    haetae_imports = {m for m in imported if m.startswith("haetae")}
    assert haetae_imports <= {"haetae.models"}, f"엔진 모듈 import 발견: {haetae_imports}"
    forbidden = {"loop", "gate", "executors", "replan", "scaffold", "run_harness",
                 "scheduler", "worktree", "deps", "judge"}
    assert not any(f in m.split(".") for m in imported for f in forbidden)


# state.yaml 텍스트(서버 테스트용) — State.from_yaml가 읽을 직렬화.
def _state_yaml() -> str:
    import yaml
    return yaml.safe_dump(_state().model_dump(mode="json", by_alias=True), allow_unicode=True)
