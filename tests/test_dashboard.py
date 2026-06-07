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
    Check,
    CheckReport,
    CheckType,
    DecompositionUnit,
    Event,
    PlanItem,
    PlanState,
    ProjectSpec,
    RunEvidence,
    SpecCritique,
    SpecGap,
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
