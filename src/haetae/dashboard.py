"""Phase A 웹 대시보드 — state.yaml의 **read-only** 뷰 (WO#28).

LEAP의 협업 표면: "어떤 goal이 열려있고 *뭐가 진전을 막는지*"를 드러낸다(모니터링 아님).

핵심 불변식:
- **read-only** — state.yaml을 읽기만. 절대 쓰지 않는다.
- **엔진 무접촉** — loop/gate/executor/replan/scaffold 등 엔진 코드를 import하지 않는다.
  State/ProjectSpec 역직렬화용 `haetae.models`만 쓴다.
- **localhost 바인드** · **빌드 툴체인 없음**(stdlib http.server + 단일 HTML) · **zero 신규 런타임 dep**.

구조:
- `state_to_view(state, spec=None)` — 순수 변환(테스트 가능 코어). State만으로 동작하며,
  ProjectSpec이 있으면 goal/done_when/유닛 desc를 보강한다(스크래치엔 spec yaml이 없어 보통 None).
- 얇은 stdlib 서버: `GET /api/state`(매 요청 state 재로드), `GET /`(정적 HTML). 로드 실패는 200+{error}.
- CLI: `python -m haetae.dashboard --state-path <경로> --port 8000 [--spec-path P] [--poll-interval 2]`.
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from haetae.models import ProjectSpec, State

# 엔진 모듈을 절대 import하지 않는다(WO#28 불변식). models만 의존.

_DETAIL_CAP = 1500  # CheckReport.detail 요약 상한
_TRACE_CAP = 2000  # run_evidence.trace/stderr 표면화 상한
_DONE = "done"

INDEX_HTML_PATH = Path(__file__).resolve().parent / "dashboard.html"


# ──────────────────────────── 순수 변환 코어 ────────────────────────────


def _truncate(text: str | None, cap: int) -> str | None:
    if text is None:
        return None
    if len(text) <= cap:
        return text
    return text[:cap] + f"\n…(+{len(text) - cap} chars truncated)"


def _run_evidence_view(ev: Any) -> dict[str, Any] | None:
    if ev is None:
        return None
    return {
        "booted": ev.booted,
        "exit_code": ev.exit_code,
        "timed_out": ev.timed_out,
        "duration_s": ev.duration_s,
        "reason": ev.reason,
        "trace": _truncate(ev.trace, _TRACE_CAP),
        "stderr_tail": _truncate(ev.stderr_tail, _TRACE_CAP),
    }


def _check_view(c: Any) -> dict[str, Any]:
    return {
        "ac_id": c.ac_id,
        "check_type": c.check_type.value if hasattr(c.check_type, "value") else c.check_type,
        "status": c.status,
        "cmd": c.cmd,
        "exit_code": c.exit_code,
        "detail": _truncate(c.detail, _DETAIL_CAP),
        "run_evidence": _run_evidence_view(c.run_evidence),
    }


def _verdict_val(v: Any) -> str | None:
    if v is None:
        return None
    return v.value if hasattr(v, "value") else v


def _topo_levels(nodes: list[dict[str, Any]]) -> list[list[str]]:
    """의존 깊이로 층 배치 — level(u)=0(deps 없음) | 1+max(level(deps)). 순환/누락은 안전 흡수."""
    state_by_id = {n["id"]: n for n in nodes}
    level: dict[str, int] = {}

    def depth(uid: str, seen: frozenset[str]) -> int:
        if uid in level:
            return level[uid]
        node = state_by_id.get(uid)
        if node is None or uid in seen:  # 누락 dep / 순환 → 0 으로 흡수(진행 안 막음)
            return 0
        deps = [d for d in node["deps"] if d in state_by_id]
        lv = 0 if not deps else 1 + max(depth(d, seen | {uid}) for d in deps)
        level[uid] = lv
        return lv

    for n in nodes:
        depth(n["id"], frozenset())
    max_lv = max(level.values(), default=0)
    levels: list[list[str]] = [[] for _ in range(max_lv + 1)]
    for n in nodes:  # plan 순서 보존(결정론)
        levels[level.get(n["id"], 0)].append(n["id"])
    return levels


def state_to_view(state: State, spec: ProjectSpec | None = None) -> dict[str, Any]:
    """State(+옵션 ProjectSpec) → JSON 직렬화 가능 ViewModel(read-only).

    DAG/blocking은 `plan[].deps`로(스크래치엔 spec yaml이 없어 spec 보통 None). spec이 주어지면
    유닛 desc·spec goal/done_when을 보강한다. 유닛 goal 폴백: 그 유닛의 최신 event.work_order_ref.
    """
    plan = state.plan
    state_by_unit = {p.unit: p.state.value for p in plan}

    # 유닛별 최신 event(seq 최대) + 통합 event(unit=None) 분리.
    latest_event: dict[str, Any] = {}
    integration_events: list[Any] = []
    for ev in state.events:
        if ev.unit is None:
            integration_events.append(ev)
        else:
            prev = latest_event.get(ev.unit)
            if prev is None or ev.seq >= prev.seq:
                latest_event[ev.unit] = ev

    # spec decomposition desc 맵(있으면).
    spec_desc = {u.unit: u.desc for u in spec.decomposition} if spec else {}

    def unit_goal(uid: str) -> str | None:
        if uid in spec_desc:
            return spec_desc[uid]
        ev = latest_event.get(uid)
        return ev.work_order_ref if ev else None

    # ── DAG nodes / edges ──
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []
    for p in plan:
        deps = list(p.deps or [])
        nodes.append(
            {"id": p.unit, "goal": unit_goal(p.unit), "state": p.state.value, "deps": deps}
        )
        for d in deps:
            edges.append({"from": d, "to": p.unit})

    # ── blocking 분석: 미완 유닛의 *완료 안 된* 의존(LEAP "뭐가 막나") ──
    blocking: list[dict[str, Any]] = []
    for p in plan:
        if p.state.value == _DONE:
            continue
        blocked_by = [d for d in (p.deps or []) if state_by_unit.get(d) != _DONE]
        if blocked_by:
            blocking.append({"unit": p.unit, "blocked_by": blocked_by})

    # ── 유닛별 상세(최신 event) ──
    units: dict[str, Any] = {}
    for p in plan:
        ev = latest_event.get(p.unit)
        units[p.unit] = {
            "goal": unit_goal(p.unit),
            "state": p.state.value,
            "work_order_ref": ev.work_order_ref if ev else None,
            "verdict": _verdict_val(ev.verdict) if ev else None,
            "result": (ev.result if ev else None),
            "checks": [_check_view(c) for c in (ev.checks if ev else [])],
        }

    # ── 통합(unit=None) event: 통합 gate 증거 ──
    integration = [
        {
            "seq": ev.seq,
            "work_order_ref": ev.work_order_ref,
            "verdict": _verdict_val(ev.verdict),
            "result": ev.result,
            "checks": [_check_view(c) for c in ev.checks],
        }
        for ev in integration_events
    ]

    # ── 타임라인 ──
    timeline = [
        {
            "seq": ev.seq,
            "unit": ev.unit,
            "verdict": _verdict_val(ev.verdict),
            "work_order_ref": ev.work_order_ref,
            "n_checks": len(ev.checks),
            "ts": ev.ts,
        }
        for ev in state.events
    ]

    # ── spec 요약(spec 있으면 보강) ──
    spec_view: dict[str, Any] = {
        "spec_ref": state.spec_ref,
        "spec_version": state.spec_version,
        "goal": spec.goal if spec else None,
        "done_when": spec.done_when if spec else None,
        "constraints_count": len(spec.constraints) if spec else None,
        "ac_count": len(spec.acceptance_criteria) if spec else None,
    }

    critique = None
    if state.spec_critique is not None:
        sc = state.spec_critique
        critique = {
            "verdict": sc.verdict,
            "gaps": [
                {"area": g.area, "cheap_path": g.cheap_path, "strengthening": g.strengthening}
                for g in sc.gaps
            ],
            "note": sc.note,
            "resynthesized": sc.resynthesized,
        }

    budget = None
    if state.budget is not None:
        b = state.budget
        budget = {
            "spent": {"tokens": b.spent.tokens, "usd": b.spent.usd},
            "cap": {"tokens": b.cap.tokens, "usd": b.cap.usd},
        }

    return {
        "status": state.status.value,
        "spec": spec_view,
        "spec_critique": critique,
        "dag": {"nodes": nodes, "edges": edges, "levels": _topo_levels(nodes)},
        "blocking": blocking,
        "units": units,
        "integration": integration,
        "timeline": timeline,
        "pending_escalations": [_jsonable(e) for e in state.pending_escalations],
        "spec_changes": [
            {"seq": s.seq, "target": s.target, "reason": s.reason, "version": s.version}
            for s in state.spec_changes
        ],
        "budget": budget,
    }


def _jsonable(obj: Any) -> Any:
    """pending_escalations 등 자유형(dict/모델/스칼라)을 JSON 가능 형태로."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    return obj


def load_view(state_path: str | Path, spec_path: str | Path | None = None) -> dict[str, Any]:
    """state(+옵션 spec) 재로드 → view. 실패는 {error}로 흡수(서버 안 죽음)."""
    try:
        state = State.from_yaml(state_path)
    except Exception as e:  # noqa: BLE001 — 파일없음/파싱에러 전부 흡수
        return {"error": f"{type(e).__name__}: {e}", "state_path": str(state_path)}
    spec = None
    if spec_path:
        try:
            spec = ProjectSpec.from_yaml(spec_path)
        except Exception:  # noqa: BLE001 — spec은 옵션, 실패해도 state 뷰는 제공
            spec = None
    try:
        return state_to_view(state, spec)
    except Exception as e:  # noqa: BLE001 — 변환 에러도 흡수
        return {"error": f"view build failed: {type(e).__name__}: {e}"}


# ──────────────────────────── 얇은 stdlib 서버 ────────────────────────────


def _index_html(poll_ms: int) -> bytes:
    try:
        html = INDEX_HTML_PATH.read_text(encoding="utf-8")
    except OSError:
        html = "<!doctype html><meta charset=utf-8><p>dashboard.html 없음</p>"
    return html.replace("__POLL_MS__", str(poll_ms)).encode("utf-8")


def make_handler(
    state_path: str | Path, spec_path: str | Path | None, poll_ms: int
) -> type[BaseHTTPRequestHandler]:
    class DashboardHandler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 — http.server 계약
            path = self.path.split("?", 1)[0]
            if path == "/api/state":
                body = json.dumps(
                    load_view(state_path, spec_path), ensure_ascii=False, default=str
                ).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")
            elif path == "/":
                self._send(200, _index_html(poll_ms), "text/html; charset=utf-8")
            else:
                self._send(404, b'{"error":"not found"}', "application/json; charset=utf-8")

        def log_message(self, *args: Any) -> None:  # 조용히(접속 로그 억제)
            return

    return DashboardHandler


def serve(
    state_path: str | Path,
    *,
    spec_path: str | Path | None = None,
    host: str = "127.0.0.1",
    port: int = 8000,
    poll_interval: float = 2.0,
) -> None:
    handler = make_handler(state_path, spec_path, int(poll_interval * 1000))
    httpd = ThreadingHTTPServer((host, port), handler)  # localhost 바인드
    print(f"haetae dashboard → http://{host}:{port}  (state: {state_path}, poll {poll_interval}s)")
    print("read-only · Ctrl-C 종료")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="haetae.dashboard", description="state.yaml read-only 웹 대시보드 (Phase A)"
    )
    parser.add_argument("--state-path", required=True, help="State YAML 경로")
    parser.add_argument("--spec-path", default=None, help="ProjectSpec YAML(옵션 · goal/desc 보강)")
    parser.add_argument("--host", default="127.0.0.1", help="바인드 호스트(기본 localhost)")
    parser.add_argument("--port", type=int, default=8000, help="포트(기본 8000)")
    parser.add_argument(
        "--poll-interval", type=float, default=2.0, help="프런트 폴링 간격 초(기본 2)"
    )
    args = parser.parse_args(argv)
    serve(
        args.state_path,
        spec_path=args.spec_path,
        host=args.host,
        port=args.port,
        poll_interval=args.poll_interval,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
