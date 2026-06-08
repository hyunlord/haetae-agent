"""WO#28 — read-only 대시보드 테스트.

핵심은 순수 변환 `state_to_view`(웹 없이). + 얇은 서버 라이트 테스트 + 엔진 무접촉 가드.
"""

from __future__ import annotations

import ast
import json
import signal
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import pytest

from haetae.dashboard import (
    RunManager,
    _QuietThreadingHTTPServer,
    build_run_argv,
    generate_run_id,
    load_view,
    make_handler,
    state_to_view,
    valid_run_id,
    validate_options,
)
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


# ════════════════════ Phase E: 웹 제어 표면 (launch/stop/runs) WO#37 ════════════════════


# ──────────────────── 옵션 화이트리스트 / argv / run-id ────────────────────


def test_validate_options_defaults():
    """빈 입력 → 폼 디폴트(executor=codex, max-parallel=4, run-timeout=120, scaffold/skills on)."""
    o = validate_options({})
    assert o["executor"] == "codex"
    assert o["max_parallel"] == 4
    assert o["run_timeout"] == 120.0
    assert o["max_iters"] == 30
    assert o["unit_retries"] == 2
    assert o["scaffold"] is True and o["skills"] is True
    assert o["critic_model"] is None
    assert o["reasoning_effort"] is None  # 미설정 = codex 기본(기존 동작 불변)


def test_validate_options_rejects_unknown_key():
    with pytest.raises(ValueError, match="unknown option"):
        validate_options({"shell": "rm -rf /"})  # 미지 플래그 거부


def test_validate_options_rejects_out_of_range_and_bad_type():
    with pytest.raises(ValueError):
        validate_options({"max_parallel": 999})  # 범위 밖
    with pytest.raises(ValueError):
        validate_options({"max_parallel": 0})
    with pytest.raises(ValueError):
        validate_options({"run_timeout": 99999})  # 범위 밖
    with pytest.raises(ValueError):
        validate_options({"executor": "bash"})  # 화이트리스트 밖
    with pytest.raises(ValueError):
        validate_options({"scaffold": "yes"})  # bool 아님


def test_validate_options_rejects_bad_critic_model():
    with pytest.raises(ValueError):
        validate_options({"critic_model": "a; rm -rf /"})  # 셸 메타문자 거부
    assert validate_options({"critic_model": "gpt-5-codex"})["critic_model"] == "gpt-5-codex"


def test_validate_options_reasoning_effort(monkeypatch=None):
    """WO#38: 화이트리스트만 통과, 빈/미설정은 None, 나쁜 값 거부."""
    assert validate_options({"reasoning_effort": "xhigh"})["reasoning_effort"] == "xhigh"
    assert validate_options({"reasoning_effort": ""})["reasoning_effort"] is None  # 빈 = 미설정
    assert validate_options({})["reasoning_effort"] is None
    with pytest.raises(ValueError):
        validate_options({"reasoning_effort": "ultra"})  # 화이트리스트 밖
    with pytest.raises(ValueError):
        validate_options({"reasoning_effort": 5})  # 타입 불일치


def test_build_run_argv_reasoning_effort():
    """설정 시 `--reasoning-effort <effort>` 부착, 미설정이면 미부착(기존 동작 불변)."""
    set_opts = validate_options({"reasoning_effort": "high"})
    argv = build_run_argv("o", Path("/abs/runs/rid"), set_opts)
    assert argv[argv.index("--reasoning-effort") + 1] == "high"
    unset_opts = validate_options({})
    argv2 = build_run_argv("o", Path("/abs/runs/rid"), unset_opts)
    assert "--reasoning-effort" not in argv2


def test_build_run_argv_is_arglist_not_shell():
    """argv 리스트(shell 아님) · order는 단일 argv 원소 · 경로는 runs/<id>/ 아래 서버 생성."""
    opts = validate_options({"max_parallel": 2, "scaffold": False, "skills": False})
    argv = build_run_argv("build a sim; echo hi", Path("/abs/runs/rid"), opts)
    assert isinstance(argv, list)
    assert argv[0] == sys.executable and "-m" in argv and "haetae.run" in argv
    # order는 보간 없이 단일 원소로 그대로 — 셸 분리 안 됨
    assert argv[argv.index("--order") + 1] == "build a sim; echo hi"
    assert argv[argv.index("--workdir") + 1] == str(Path("/abs/runs/rid/work"))
    assert argv[argv.index("--state-path") + 1] == str(Path("/abs/runs/rid/state.yaml"))
    assert argv[argv.index("--max-parallel") + 1] == "2"
    assert "--no-scaffold" in argv and "--no-skills" in argv


def test_valid_run_id_rejects_traversal():
    assert valid_run_id("20260608-120000-foo")
    assert not valid_run_id("../etc/passwd")
    assert not valid_run_id("a/b")
    assert not valid_run_id("..")
    assert not valid_run_id("")
    assert not valid_run_id(None)


def test_generate_run_id_pattern():
    from datetime import datetime, timezone
    rid = generate_run_id("Build A Retail Sim!", now=datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc))
    assert rid.startswith("20260608-120000-")
    assert valid_run_id(rid)


# ──────────────────── launch: 서브프로세스 (mock) ────────────────────


def test_launch_spawns_arglist_and_creates_dir(tmp_path: Path):
    with mock.patch("haetae.dashboard.subprocess.Popen") as mp:
        mp.return_value.poll.return_value = None  # running
        rm = RunManager(tmp_path / "runs", allow_run=True)
        rid = rm.launch("build a sim", {"max_parallel": 2})
        assert valid_run_id(rid)
        # runs/<id>/ + work/ + meta.json 생성(서버가 만든 경로)
        assert (rm.runs_dir / rid / "work").is_dir()
        assert (rm.runs_dir / rid / "meta.json").exists()
        # Popen은 argv 리스트로 호출 — shell 아님
        args, kwargs = mp.call_args
        argv = args[0]
        assert isinstance(argv, list) and argv[0] == sys.executable
        assert argv[argv.index("--order") + 1] == "build a sim"
        assert argv[argv.index("--max-parallel") + 1] == "2"
        assert kwargs.get("shell", False) is False
        assert kwargs.get("stdout") is not None  # run.log로 리다이렉트


def test_launch_applies_defaults_in_argv(tmp_path: Path):
    with mock.patch("haetae.dashboard.subprocess.Popen") as mp:
        mp.return_value.poll.return_value = None
        rm = RunManager(tmp_path / "runs", allow_run=True)
        rm.launch("x", {})
        argv = mp.call_args[0][0]
        assert argv[argv.index("--executor") + 1] == "codex"
        assert argv[argv.index("--max-parallel") + 1] == "4"
        assert "--scaffold" in argv and "--skills" in argv


def test_launch_rejects_empty_order(tmp_path: Path):
    rm = RunManager(tmp_path / "runs", allow_run=True)
    with pytest.raises(ValueError):
        rm.launch("   ", {})


# ──────────────────── stop: SIGINT 먼저 ────────────────────


def test_stop_sends_sigint_first_then_status_stopped(tmp_path: Path):
    with mock.patch("haetae.dashboard.subprocess.Popen") as mp:
        proc = mp.return_value
        proc.poll.return_value = None
        proc.wait.return_value = 0  # grace 내 종료(에스컬레이트 안 함)
        rm = RunManager(tmp_path / "runs", allow_run=True)
        rid = rm.launch("order", {})
        assert rm.stop(rid) is True
        proc.send_signal.assert_called_once_with(signal.SIGINT)  # SIGINT 먼저
        assert rm.status_of(rid) == "stopped"


def test_stop_unknown_run_returns_false(tmp_path: Path):
    rm = RunManager(tmp_path / "runs", allow_run=True)
    assert rm.stop("nope-not-here") is False


# ──────────────────── runs 목록: meta + 상태 해석 ────────────────────


def _write_run(runs_dir: Path, rid: str, status: str, order: str = "o") -> None:
    d = runs_dir / rid
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(
        json.dumps({"id": rid, "order": order, "status": status,
                    "started_at": "2026-06-08T12:00:00Z"}),
        encoding="utf-8",
    )


def test_list_runs_reads_meta(tmp_path: Path):
    rm = RunManager(tmp_path / "runs", allow_run=False)
    _write_run(rm.runs_dir, "20260608-120000-a", "finished")
    _write_run(rm.runs_dir, "20260608-130000-b", "failed")
    runs = {r["id"]: r for r in rm.list_runs()}
    assert runs["20260608-120000-a"]["status"] == "finished"
    assert runs["20260608-130000-b"]["status"] == "failed"


def test_list_runs_lost_handle_running_is_unknown(tmp_path: Path):
    """핸들 없는(레지스트리 부재) running → 상태 미상으로 정직 표기."""
    rm = RunManager(tmp_path / "runs", allow_run=False)
    _write_run(rm.runs_dir, "20260608-140000-c", "running")  # 디스크엔 running이나 핸들 없음
    runs = {r["id"]: r for r in rm.list_runs()}
    assert runs["20260608-140000-c"]["status"] == "unknown"


def test_list_runs_skips_bad_dir_names(tmp_path: Path):
    rm = RunManager(tmp_path / "runs", allow_run=False)
    rm.runs_dir.mkdir(parents=True, exist_ok=True)
    (rm.runs_dir / "not-a-run").mkdir()  # meta.json 없음 → 스킵
    assert rm.list_runs() == []


# ──────────────────── 서버 엔드포인트 ────────────────────


def _serve_ctl(run_manager: RunManager, state_path: Path | None = None):
    handler = make_handler(
        str(state_path) if state_path else None, None, 2000, run_manager=run_manager
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


def _post(port: int, path: str, payload: dict | None = None) -> tuple[int, str]:
    data = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def test_post_run_403_without_allow_run(tmp_path: Path):
    """제어 opt-in: --allow-run 없이 POST /api/run → 403(read-only 디폴트 보존)."""
    rm = RunManager(tmp_path / "runs", allow_run=False)
    httpd, port = _serve_ctl(rm)
    try:
        code, body = _post(port, "/api/run", {"order": "x"})
        assert code == 403
        assert "control disabled" in json.loads(body)["error"]
    finally:
        httpd.shutdown()


def test_readonly_runs_and_state_still_work(tmp_path: Path):
    """플래그 없어도 runs 목록·state 보기(읽기)는 동작."""
    rm = RunManager(tmp_path / "runs", allow_run=False)
    _write_run(rm.runs_dir, "20260608-120000-a", "finished")
    httpd, port = _serve_ctl(rm)
    try:
        code, body = _get(port, "/api/runs")
        assert code == 200
        payload = json.loads(body)
        assert payload["allow_run"] is False
        assert payload["runs"][0]["id"] == "20260608-120000-a"
    finally:
        httpd.shutdown()


def test_post_run_launches_with_allow_run(tmp_path: Path):
    with mock.patch("haetae.dashboard.subprocess.Popen") as mp:
        mp.return_value.poll.return_value = None
        rm = RunManager(tmp_path / "runs", allow_run=True)
        httpd, port = _serve_ctl(rm)
        try:
            code, body = _post(port, "/api/run",
                               {"order": "build x", "options": {"executor": "codex"}})
            assert code == 200
            rid = json.loads(body)["run_id"]
            assert valid_run_id(rid)
            assert (rm.runs_dir / rid / "work").is_dir()
        finally:
            httpd.shutdown()


def test_post_run_bad_options_400(tmp_path: Path):
    rm = RunManager(tmp_path / "runs", allow_run=True)
    httpd, port = _serve_ctl(rm)
    try:
        code, body = _post(port, "/api/run", {"order": "x", "options": {"max_parallel": 999}})
        assert code == 400
    finally:
        httpd.shutdown()


def test_post_stop_endpoint(tmp_path: Path):
    with mock.patch("haetae.dashboard.subprocess.Popen") as mp:
        proc = mp.return_value
        proc.poll.return_value = None
        proc.wait.return_value = 0
        rm = RunManager(tmp_path / "runs", allow_run=True)
        rid = rm.launch("order", {})
        httpd, port = _serve_ctl(rm)
        try:
            code, body = _post(port, f"/api/run/{rid}/stop")
            assert code == 200 and json.loads(body)["status"] == "stopped"
            proc.send_signal.assert_called_once_with(signal.SIGINT)
        finally:
            httpd.shutdown()


def test_post_stop_403_without_allow_run(tmp_path: Path):
    rm = RunManager(tmp_path / "runs", allow_run=False)
    httpd, port = _serve_ctl(rm)
    try:
        code, _ = _post(port, "/api/run/20260608-120000-x/stop")
        assert code == 403
    finally:
        httpd.shutdown()


def test_state_targets_run_by_id(tmp_path: Path):
    """GET /api/state?run=<id> → runs/<id>/state.yaml 를 읽어 그 run의 뷰."""
    rm = RunManager(tmp_path / "runs", allow_run=True)
    rid = "20260608-120000-test"
    d = rm.runs_dir / rid
    d.mkdir(parents=True)
    (d / "state.yaml").write_text(_state_yaml(), encoding="utf-8")
    httpd, port = _serve_ctl(rm)
    try:
        code, body = _get(port, f"/api/state?run={rid}")
        assert code == 200
        v = json.loads(body)
        assert v["status"] == "escalated" and len(v["dag"]["nodes"]) == 5
    finally:
        httpd.shutdown()


def test_state_invalid_run_id_returns_error(tmp_path: Path):
    """경로 안전: ?run=../ 같은 traversal 시도는 {error}(서버 안 죽음)."""
    rm = RunManager(tmp_path / "runs", allow_run=True)
    httpd, port = _serve_ctl(rm)
    try:
        code, body = _get(port, "/api/state?run=..%2F..%2Fetc")
        assert code == 200
        assert "error" in json.loads(body)
    finally:
        httpd.shutdown()


def test_index_html_shows_allow_run_flag(tmp_path: Path):
    """--allow-run 토큰이 HTML에 치환되어 프런트가 폼 표시 여부를 안다."""
    rm = RunManager(tmp_path / "runs", allow_run=True)
    httpd, port = _serve_ctl(rm)
    try:
        code, body = _get(port, "/")
        assert code == 200
        assert "const ALLOW_RUN = true" in body
    finally:
        httpd.shutdown()


def test_launcher_uses_only_subprocess_not_engine_import():
    """런처가 엔진을 import하지 않고 subprocess로 격리하는지 — 가드 보강.

    test_dashboard_does_not_import_engine_modules가 haetae import ⊆ {models}를 강제하므로
    여기선 런처가 subprocess/signal을 쓰는지(격리 메커니즘)만 추가 확인.
    """
    src = DASH.read_text(encoding="utf-8")
    assert "subprocess.Popen" in src  # 서브프로세스 spawn(엔진 직접 호출 아님)
    assert "signal.SIGINT" in src  # stop은 SIGINT 신호로


# ──────────────────── WO#38 Part A: 예상된 SSE 끊김 traceback 억제 ────────────────────


def _handle_error_with_exc(server, exc: BaseException) -> None:
    """현재 예외 컨텍스트(sys.exc_info)를 세워두고 handle_error를 호출한다."""
    try:
        raise exc
    except BaseException:  # noqa: BLE001 — 의도적으로 exc 컨텍스트만 세움
        server.handle_error(None, ("127.0.0.1", 12345))


@pytest.mark.parametrize(
    "exc",
    [ConnectionResetError(54, "reset"), BrokenPipeError(), ConnectionAbortedError()],
)
def test_handle_error_silences_expected_disconnects(exc, capsys):
    """예상된 클라이언트 끊김은 traceback/raise 없이 조용히 무시(무-로그스팸)."""
    server = _QuietThreadingHTTPServer(("127.0.0.1", 0), make_handler(None, None, 1000))
    try:
        _handle_error_with_exc(server, exc)  # raise하면 테스트가 깨짐 → 조용함을 보장
    finally:
        server.server_close()
    captured = capsys.readouterr()
    # 기본 handle_error는 stderr에 traceback/구분선을 찍는다 — 조용히면 아무것도 없어야.
    assert captured.err == ""
    assert "Traceback" not in captured.out


def test_handle_error_surfaces_real_errors(capsys):
    """예상치 못한 예외(ValueError)는 기존대로 surface(진짜 에러를 숨기지 않음)."""
    server = _QuietThreadingHTTPServer(("127.0.0.1", 0), make_handler(None, None, 1000))
    try:
        _handle_error_with_exc(server, ValueError("진짜 버그"))
    finally:
        server.server_close()
    captured = capsys.readouterr()
    # stdlib 기본 handle_error는 stderr로 traceback을 찍는다 → surface됨을 단언.
    assert "Traceback" in captured.err
    assert "ValueError" in captured.err


# ════════════════════ v3 대시보드 폴리시 (WO#42) ════════════════════


# ──────────────────── A. 밀도 라이브 리스트용 per-unit 행 ────────────────────


def test_unit_rows_dense_list_fields():
    """unit_rows: plan 순서 한 줄씩 + in-flight 유닛의 stage/active/elapsed/tokens."""
    v = state_to_view(_state_v2(), now="2026-06-07T14:00:30Z")
    assert [r["unit"] for r in v["unit_rows"]] == ["u1", "u2", "u3"]  # plan 순서(결정론)
    rows = {r["unit"]: r for r in v["unit_rows"]}
    # in-flight(activity 있는) 유닛: 현재 단계 + active + 경과
    assert rows["u2"]["stage"] == "build" and rows["u2"]["active"] is True
    assert rows["u2"]["elapsed_s"] == 15.0  # now - started_at
    assert rows["u3"]["stage"] == "verify" and rows["u3"]["active"] is True
    # tokens는 cost.by_unit 집계에서
    assert rows["u1"]["tokens"] == 150 and rows["u2"]["tokens"] == 4000
    # done 유닛: 비활성, 단계 없음
    assert rows["u1"]["active"] is False and rows["u1"]["stage"] is None


def test_unit_rows_check_counts():
    """체크 pass/fail/total 카운트가 유닛 최신 event에서 집계된다."""
    v = state_to_view(_state())
    rows = {r["unit"]: r for r in v["unit_rows"]}
    assert rows["u1"]["checks_pass"] == 1 and rows["u1"]["checks_fail"] == 0
    assert rows["u1"]["checks_total"] == 1


def test_unit_rows_empty_fields_when_absent_no_crash():
    """데이터 부재(activity/cost 없음) → stage/elapsed/tokens None, active False (무크래시)."""
    v = state_to_view(_state())  # 구버전 state: activity/cost 없음
    rows = {r["unit"]: r for r in v["unit_rows"]}
    # u3은 in_progress이지만 activity가 없음 → 단계/경과 None, active False
    assert rows["u3"]["stage"] is None
    assert rows["u3"]["elapsed_s"] is None
    assert rows["u3"]["active"] is False
    assert rows["u3"]["tokens"] is None
    assert rows["u3"]["checks_total"] == 0  # u3 event 없음


def test_unit_rows_json_serializable():
    json.dumps(state_to_view(_state_v2(), now="2026-06-07T14:00:30Z")["unit_rows"])


# ──────────────────── C. 라이브 작업 로그 tail 엔드포인트 ────────────────────


def _get_status(port: int, path: str) -> tuple[int, str]:
    """_get과 같지만 4xx/5xx도 (code, body)로 반환(HTTPError 흡수) — 거부 경로 검증용."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def _write_log(runs_dir: Path, rid: str, content: str) -> None:
    d = runs_dir / rid
    d.mkdir(parents=True, exist_ok=True)
    (d / "run.log").write_text(content, encoding="utf-8")


def test_log_endpoint_returns_tail(tmp_path: Path):
    """GET /api/runs/<id>/log?tail=N → run.log 마지막 N줄."""
    rm = RunManager(tmp_path / "runs", allow_run=False)  # 읽기 — allow_run 불필요
    _write_log(rm.runs_dir, "20260608-120000-a",
               "\n".join(f"line{i}" for i in range(100)) + "\n")
    httpd, port = _serve_ctl(rm)
    try:
        code, body = _get(port, "/api/runs/20260608-120000-a/log?tail=5")
        assert code == 200
        data = json.loads(body)
        assert data["lines"] == ["line95", "line96", "line97", "line98", "line99"]
        assert data["missing"] is False
    finally:
        httpd.shutdown()


def test_log_endpoint_rejects_traversal(tmp_path: Path):
    """경로 안전: run id에 `..`가 들어가면 400(valid_run_id 거부).

    `..%2F..` 형태는 urllib 클라이언트가 전송 전 정규화해버리므로, 단일 세그먼트로
    서버까지 그대로 도달하는 `a..b`(`..` 포함)로 엔드포인트의 valid_run_id 가드를 검증한다.
    (실제 `../` traversal 차단은 read_log_tail 단위 테스트가 직접 검증한다.)
    """
    rm = RunManager(tmp_path / "runs", allow_run=False)
    httpd, port = _serve_ctl(rm)
    try:
        code, body = _get_status(port, "/api/runs/a..b/log?tail=5")
        assert code == 400
        assert "invalid run id" in json.loads(body)["error"]
    finally:
        httpd.shutdown()


def test_log_endpoint_missing_log_absorbed(tmp_path: Path):
    """로그 부재 → missing=True, 빈 lines, 200(서버 안 죽음)."""
    rm = RunManager(tmp_path / "runs", allow_run=False)
    (rm.runs_dir / "20260608-120000-b").mkdir(parents=True)  # 디렉토리만, run.log 없음
    httpd, port = _serve_ctl(rm)
    try:
        code, body = _get(port, "/api/runs/20260608-120000-b/log")
        assert code == 200
        data = json.loads(body)
        assert data["missing"] is True and data["lines"] == []
    finally:
        httpd.shutdown()


def test_read_log_tail_rejects_bad_id(tmp_path: Path):
    rm = RunManager(tmp_path / "runs", allow_run=False)
    out = rm.read_log_tail("../etc/passwd")
    assert "error" in out and out["lines"] == []


def test_read_log_tail_bounded_and_clamps(tmp_path: Path):
    """대용량 로그도 bounded — tail 상한 클램프 + 끝에서 제한 바이트만(덤프 금지)."""
    rm = RunManager(tmp_path / "runs", allow_run=False)
    # 5만 줄(수백 KB) — 상한을 훨씬 넘김
    _write_log(rm.runs_dir, "20260608-120000-c",
               "\n".join(f"L{i}" for i in range(50000)) + "\n")
    out = rm.read_log_tail("20260608-120000-c", 999999)  # 상한 초과 요청
    assert len(out["lines"]) <= 2000  # _LOG_TAIL_MAX_LINES로 클램프
    assert out["truncated"] is True
    # 마지막 줄은 보존(끝에서 읽으므로)
    assert out["lines"][-1] == "L49999"


def test_log_endpoint_no_run_manager_404(tmp_path: Path):
    """run_manager 없는(레거시 단일 state) 모드 → 로그 엔드포인트 404(서버 안 죽음)."""
    sp = tmp_path / "s.yaml"
    sp.write_text(_state_yaml(), encoding="utf-8")
    httpd, port = _serve(sp)  # run_manager 없이
    try:
        code, body = _get_status(port, "/api/runs/20260608-120000-x/log")
        assert code == 404
        assert "lines" in json.loads(body)
    finally:
        httpd.shutdown()
