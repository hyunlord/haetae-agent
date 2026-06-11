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
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from haetae.models import ProjectSpec, State
from haetae.providers.launch_options import all_launch_options

# 엔진 모듈을 절대 import하지 않는다(WO#28/#37 불변식). models + launch_options(엔진-free
# 리프)만 의존. launch_options는 *순수 옵션 메타데이터*(+read-only config 읽기)뿐이라 엔진/
# 실행 코드를 끌어오지 않는다 — provider가 선언한 디스크립터를 폼이 그대로 읽기 위함(WO#45).
# 제어 표면(launch/stop)은 엔진을 *import해 돌리지 않고* `python -m haetae.run`을
# 별도 서브프로세스로 spawn할 뿐 — 런처는 stdlib(subprocess/signal/os)만 쓴다.

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


def _parse_ts(ts: str | None) -> datetime | None:
    """ISO 8601(끝 Z 허용)을 aware datetime으로. 실패하면 None(날조 금지)."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _elapsed_s(started_at: str | None, now_dt: datetime | None) -> float | None:
    """now - started_at(초). 둘 중 하나라도 못 파싱하면 None. 음수는 0으로 클램프."""
    start = _parse_ts(started_at)
    if start is None or now_dt is None:
        return None
    delta = (now_dt - start).total_seconds()
    return delta if delta >= 0 else 0.0


def _cost_view(c: Any) -> dict[str, Any] | None:
    """Cost 모델(또는 None) → JSON dict. 새 필드 부재도 getattr로 흡수."""
    if c is None:
        return None
    return {
        "tokens": getattr(c, "tokens", None),
        "usd": getattr(c, "usd", None),
        "input": getattr(c, "input", None),
        "output": getattr(c, "output", None),
        "source": getattr(c, "source", None),
        "note": getattr(c, "note", None),
    }


def _accumulate_cost(acc: dict[str, Any], c: Any) -> None:
    """source/unit 집계 버킷에 Cost를 합산(None-safe). count는 cost가 있는 event 수."""
    acc["count"] = acc.get("count", 0) + 1
    for field in ("tokens", "usd", "input", "output"):
        v = getattr(c, field, None)
        if v is not None:
            acc[field] = (acc.get(field) or 0) + v
        else:
            acc.setdefault(field, None)


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


# ──────────────────── WO#46 A: 생애주기 단계(phase) 파생 ────────────────────
#
# transitions의 stage를 생애주기 *버킷*으로 매핑한다(엔진 새 필드 요구 없이 파생만).
# stage 어휘(loop.py): synthesize/scaffold/build/verify/replan/done/escalate/
#   decomp-reject/or-alternative. verify는 unit 유무로 빌드(유닛 검증) vs 통합(unit=None) 구분.

_PHASE_ORDER = ("synthesize", "scaffold", "build", "integration", "or")
_PHASE_LABELS = {
    "synthesize": "합성",
    "scaffold": "스캐폴드",
    "build": "빌드",
    "integration": "통합 검증",
    "or": "OR 대안",
}


def _phase_bucket(stage: str | None, unit: str | None) -> str | None:
    """transition (stage, unit) → 생애주기 버킷. 매핑 없는 stage(done 등)는 None."""
    if stage == "synthesize":
        return "synthesize"
    if stage == "scaffold":
        return "scaffold"
    if stage in ("build", "replan", "decomp-reject", "escalate"):
        return "build"
    if stage == "verify":
        # 유닛 검증은 빌드 사이클의 일부, unit=None은 통합 gate.
        return "build" if unit is not None else "integration"
    if stage == "or-alternative":
        return "or"
    return None  # done/미지 stage → 단계 버킷 없음


def _derive_phases(
    transitions: list[Any],
    *,
    running: bool,
    unit_counts: dict[str, int],
    integration_verdict: str | None,
    or_alternatives: int,
    n_approaches: int,
    critique: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """transitions에서 생애주기 단계 섹션을 파생한다(best-effort, 파생만).

    각 phase {name,label,status(done|active|pending|skipped),summary}. 현재 phase=최신
    transition의 버킷. OR 단계는 approaches 있을 때만 포함. transitions 부재 → [](무크래시).
    """
    if not transitions:
        return []
    occurred: dict[str, bool] = {p: False for p in _PHASE_ORDER}
    current: str | None = None
    for t in transitions:
        b = _phase_bucket(getattr(t, "stage", None), getattr(t, "unit", None))
        if b is None:
            continue
        occurred[b] = True
        current = b  # 마지막으로 버킷 있는 transition = 현재 phase
    current_idx = _PHASE_ORDER.index(current) if current in _PHASE_ORDER else -1

    def _summary(name: str) -> str | None:
        if name == "synthesize":
            if critique is None:
                return None
            v = critique.get("verdict")
            if critique.get("resynthesized"):
                return f"재합성됨 · {v}" if v else "재합성됨"
            return v
        if name == "build":
            return f"완료 {unit_counts.get('done', 0)}/{unit_counts.get('total', 0)}"
        if name == "integration":
            return integration_verdict
        if name == "or":
            return f"대안 {or_alternatives}회" if or_alternatives else f"{n_approaches} 시도"
        return None  # scaffold 등은 상태 배지로 충분

    phases: list[dict[str, Any]] = []
    for i, name in enumerate(_PHASE_ORDER):
        if name == "or" and n_approaches <= 0:
            continue  # OR 대안은 실제 시도가 있을 때만 노출(WO#46: "있을 때만")
        if occurred[name]:
            status = "active" if (running and name == current) else "done"
        elif current_idx >= 0 and i < current_idx:
            status = "skipped"  # 현재보다 앞 단계인데 발생 안 함(예: --no-scaffold)
        else:
            status = "pending"
        phases.append(
            {"name": name, "label": _PHASE_LABELS[name], "status": status, "summary": _summary(name)}
        )
    return phases


def _unit_rounds(
    uid: str, transitions: list[Any], unit_events: list[Any]
) -> list[dict[str, Any]]:
    """유닛의 transitions를 *라운드*(코딩→검증 1사이클)로 그룹하고 gate 체크를 귀속한다(WO#46 B).

    `build` stage마다 새 라운드 시작(재시도면 복수 라운드). 각 event(gate 평가)를 ts 창으로
    해당 라운드에 귀속(ts 없으면 순번 폴백). transitions 없고 event만 있으면(구버전) event당
    1라운드. 전부 best-effort — 부재/누락은 빈 값으로 흡수.
    """
    utr = [t for t in transitions if getattr(t, "unit", None) == uid]
    uevents = sorted(unit_events, key=lambda e: e.seq)
    rounds: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for t in utr:
        stage = getattr(t, "stage", None)
        ts = getattr(t, "ts", None)
        if stage == "build" or cur is None:
            cur = {
                "round": len(rounds) + 1,
                "stages": [],
                "started_at": ts,
                "checks": [],
                "verdict": None,
                "result": None,
                "seq": None,
            }
            rounds.append(cur)
        cur["stages"].append({"stage": stage, "ts": ts})
        if ts and (cur["started_at"] is None or ts < cur["started_at"]):
            cur["started_at"] = ts

    if rounds:
        for idx, ev in enumerate(uevents):
            target: dict[str, Any] | None = None
            if ev.ts:
                for r in rounds:  # 그 event 이전 가장 최근 build 라운드(ts 창)
                    if r["started_at"] is not None and r["started_at"] <= ev.ts:
                        target = r
            if target is None:  # ts 없음/매칭 실패 → 순번 폴백(초과분은 마지막 라운드)
                target = rounds[min(idx, len(rounds) - 1)]
            target["checks"] = [_check_view(c) for c in ev.checks]
            target["verdict"] = _verdict_val(ev.verdict)
            target["result"] = ev.result
            target["seq"] = ev.seq
    else:
        # transitions 부재(구버전 state)지만 event는 있을 때 — event당 1라운드로 흡수.
        for ev in uevents:
            rounds.append(
                {
                    "round": len(rounds) + 1,
                    "stages": [],
                    "started_at": ev.ts,
                    "checks": [_check_view(c) for c in ev.checks],
                    "verdict": _verdict_val(ev.verdict),
                    "result": ev.result,
                    "seq": ev.seq,
                }
            )
    return rounds


def state_to_view(
    state: State, spec: ProjectSpec | None = None, *, now: str | None = None
) -> dict[str, Any]:
    """State(+옵션 ProjectSpec) → JSON 직렬화 가능 ViewModel(read-only).

    DAG/blocking은 `plan[].deps`로(스크래치엔 spec yaml이 없어 spec 보통 None). spec이 주어지면
    유닛 desc·spec goal/done_when을 보강한다. 유닛 goal 폴백: 그 유닛의 최신 event.work_order_ref.

    v2(WO#35) — #33/#34 계측을 라이브로 표면화(전부 best-effort, 부재 시 빈 섹션):
      - activity: 현재 in-flight 유닛 + 단계(build/verify) + 경과시간(now-started_at).
      - transitions: 단계 전이 이력(합성→build→verify→replan…)을 시간순.
      - cost: budget.spent(total) + event.cost를 source/unit별로 집계. usd 미상은 None 유지.
      - DAG 노드/유닛에 in_progress면 현재 단계 배지.
    now: 경과시간 계산 기준 시각(ISO; 테스트 주입용). 기본 None=실제 UTC.
    """
    now_dt = _parse_ts(now) if now is not None else datetime.now(timezone.utc)
    plan = state.plan
    state_by_unit = {p.unit: p.state.value for p in plan}

    # 라이브 activity: unit → 현재 단계(노드/유닛 배지에 사용). best-effort.
    activity_list = getattr(state, "activity", None) or []
    activity_by_unit: dict[str, str] = {
        a.unit: a.stage for a in activity_list if a.unit is not None
    }

    # 유닛별 최신 event(seq 최대) + 통합 event(unit=None) 분리.
    latest_event: dict[str, Any] = {}
    events_by_unit: dict[str, list[Any]] = {}  # WO#46 B: 유닛별 *전체* event(라운드 드릴다운용)
    integration_events: list[Any] = []
    for ev in state.events:
        if ev.unit is None:
            integration_events.append(ev)
        else:
            events_by_unit.setdefault(ev.unit, []).append(ev)
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
            {
                "id": p.unit,
                "goal": unit_goal(p.unit),
                "state": p.state.value,
                "deps": deps,
                # in_progress면 현재 세부 단계(build/verify) 배지, 아니면 None.
                "stage": activity_by_unit.get(p.unit),
            }
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
    # WO#46 B: 라운드 드릴다운용 transitions(파생만). 아래 v3 블록의 transitions_list와 동일 소스.
    _transitions = getattr(state, "transitions", None) or []
    units: dict[str, Any] = {}
    for p in plan:
        ev = latest_event.get(p.unit)
        units[p.unit] = {
            "goal": unit_goal(p.unit),
            "state": p.state.value,
            "stage": activity_by_unit.get(p.unit),  # in_progress면 현재 단계
            "work_order_ref": ev.work_order_ref if ev else None,
            "verdict": _verdict_val(ev.verdict) if ev else None,
            "result": (ev.result if ev else None),
            "checks": [_check_view(c) for c in (ev.checks if ev else [])],
            "cost": _cost_view(ev.cost) if ev else None,
            # 라운드별(코딩→검증 사이클) gate 체크 귀속 — 한 유닛이 몇 번 어떻게 시도됐나.
            "rounds": _unit_rounds(p.unit, _transitions, events_by_unit.get(p.unit, [])),
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

    # ── v2: 라이브 activity(+경과) / 단계 전이 이력 / 코스트 집계 ──
    activity = [
        {
            "unit": a.unit,
            "stage": a.stage,
            "started_at": a.started_at,
            "elapsed_s": _elapsed_s(a.started_at, now_dt),
        }
        for a in activity_list
    ]
    transitions = [
        {"stage": t.stage, "unit": t.unit, "ts": t.ts}
        for t in (getattr(state, "transitions", None) or [])
    ]

    # 코스트: total(budget.spent, 권위) + event.cost를 source/unit별로 집계(귀속 분해).
    spent = state.budget.spent if state.budget is not None else None
    cap = state.budget.cap if state.budget is not None else None
    by_source: dict[str, Any] = {}
    by_unit: dict[str, Any] = {}
    for ev in state.events:
        c = ev.cost
        if c is None:
            continue
        src = getattr(c, "source", None) or "unknown"
        _accumulate_cost(by_source.setdefault(src, {}), c)
        _accumulate_cost(by_unit.setdefault(ev.unit or "(integration)", {}), c)
    cost = {
        "total": {
            "tokens": getattr(spent, "tokens", None),
            "usd": getattr(spent, "usd", None),
            "input": getattr(spent, "input", None),
            "output": getattr(spent, "output", None),
        },
        "cap": {"tokens": getattr(cap, "tokens", None), "usd": getattr(cap, "usd", None)},
        "by_source": by_source,
        "by_unit": by_unit,
    }

    # ── v3.1(WO#44): #40/#41 표면화 + per-unit 집계(retry/OR/decomp-reject) ──
    # state.approaches(ApproachAttempt)·decomp_critiques(DecompCritique)를 뷰로 노출
    # (지금까진 state엔 기록되나 뷰엔 안 냄). 부재/구버전 state → 빈(무크래시, getattr).
    approaches_list = getattr(state, "approaches", None) or []
    decomp_list = getattr(state, "decomp_critiques", None) or []
    transitions_list = getattr(state, "transitions", None) or []

    # 유닛별 재dispatch 횟수: build 단계 전이 수 - 1(첫 dispatch는 재시도 아님). 부재 0.
    build_counts: dict[str, int] = {}
    for t in transitions_list:
        if t.stage == "build" and t.unit is not None:
            build_counts[t.unit] = build_counts.get(t.unit, 0) + 1

    # 유닛별 OR 접근 시도 수(scope=="unit:<id>"). approaches는 or_alternatives>0일 때만 채워짐.
    or_attempts_by_unit: dict[str, int] = {}
    or_alternatives = 0  # index>=1(원본 제외 '대안') 전역 합 — #41이 일한 흔적을 요약에.
    for a in approaches_list:
        scope = a.scope or ""
        if scope.startswith("unit:"):
            uid = scope[len("unit:"):]
            or_attempts_by_unit[uid] = or_attempts_by_unit.get(uid, 0) + 1
        if (getattr(a, "index", 0) or 0) >= 1:
            or_alternatives += 1

    # 유닛별 분해 reject 수(rejected=True) + 전역 합 — #40이 일한 흔적을 요약에.
    decomp_reject_by_unit: dict[str, int] = {}
    decomp_rejects = 0
    for d in decomp_list:
        if getattr(d, "rejected", False):
            decomp_rejects += 1
            if d.unit:
                decomp_reject_by_unit[d.unit] = decomp_reject_by_unit.get(d.unit, 0) + 1

    approaches_view = [
        {
            "scope": a.scope,
            "approach": a.approach,
            "outcome": a.outcome,
            "evidence": a.evidence,
            "index": getattr(a, "index", 0),
        }
        for a in approaches_list
    ]
    decomp_critiques_view = [
        {
            "verdict": d.verdict,
            "reason": d.reason,
            "unit": d.unit,
            "rejected": getattr(d, "rejected", False),
        }
        for d in decomp_list
    ]

    # ── v3(WO#42): DW식 밀도 라이브 리스트용 per-unit 행(plan 순서, 결정론) ──
    # 한 줄 = 유닛 · 단계/상태 · tokens · 경과 · 체크수(pass/fail). 전부 기존 view
    # 데이터(activity/by_unit/units/checks)에서 모은다. 부재 필드는 None/0(무크래시).
    activity_elapsed: dict[str, float | None] = {
        a["unit"]: a["elapsed_s"] for a in activity if a["unit"] is not None
    }
    unit_rows: list[dict[str, Any]] = []
    for p in plan:
        uid = p.unit
        ev = latest_event.get(uid)
        ev_checks = ev.checks if ev else []
        cp = sum(1 for c in ev_checks if c.status == "pass")
        cf = sum(1 for c in ev_checks if c.status == "fail")
        bu_u = by_unit.get(uid) or {}
        unit_rows.append(
            {
                "unit": uid,
                "goal": unit_goal(uid),
                "state": p.state.value,
                "stage": activity_by_unit.get(uid),  # in_progress면 현재 세부 단계
                "active": uid in activity_by_unit,  # in-flight 강조(DW의 '>' 커서)
                "elapsed_s": activity_elapsed.get(uid),  # in-flight면 경과(now-started)
                "tokens": bu_u.get("tokens"),
                "usd": bu_u.get("usd"),
                "checks_pass": cp,
                "checks_fail": cf,
                "checks_total": len(ev_checks),
                "verdict": _verdict_val(ev.verdict) if ev else None,
                # v3.1(WO#44 C): 재시도/OR 대안/분해 reject 배지용 카운트(부재 0, 무크래시).
                "retries": max(0, build_counts.get(uid, 0) - 1),
                "or_attempts": or_attempts_by_unit.get(uid, 0),
                "decomp_rejects": decomp_reject_by_unit.get(uid, 0),
            }
        )

    # ── v3.1(WO#44 B): DW식 한눈 요약 바(유닛 상태별·현재단계·활성·토큰·경과·status) ──
    # DW의 "Scope 1/1·Verify 75/75" 같은 한눈 진척에 대응. 기존 데이터 집계만(무크래시).
    unit_counts = {"total": len(plan), "done": 0, "in_progress": 0, "pending": 0, "failed": 0}
    for p in plan:
        sv = p.state.value
        if sv in unit_counts:
            unit_counts[sv] += 1

    # 경과: 가장 이른 ts(transitions+events)~현재(running)/마지막. 못 잡으면 None(날조 금지).
    all_ts = [d for d in (_parse_ts(t.ts) for t in transitions_list) if d is not None]
    all_ts += [d for d in (_parse_ts(ev.ts) for ev in state.events) if d is not None]
    start_dt = min(all_ts) if all_ts else None
    last_dt = max(all_ts) if all_ts else None
    running = state.status.value == "running"
    end_dt = now_dt if (running and now_dt is not None) else last_dt
    elapsed_summary: float | None = None
    if start_dt is not None and end_dt is not None:
        elapsed_summary = (end_dt - start_dt).total_seconds()
        if elapsed_summary < 0:
            elapsed_summary = 0.0

    summary = {
        "status": state.status.value,
        "units": unit_counts,
        # 현재 단계 = 가장 최근 transition stage(이력 없으면 None).
        "current_stage": transitions[-1]["stage"] if transitions else None,
        "active_count": len(activity_list),  # 동시 in-flight(에이전트) 수
        "tokens": {
            "total": getattr(spent, "tokens", None),
            "input": getattr(spent, "input", None),
            "output": getattr(spent, "output", None),
        },
        "elapsed_s": elapsed_summary,
        # #40/#41이 일한 흔적(전역): OR 대안 횟수 · 분해 reject 횟수.
        "or_alternatives": or_alternatives,
        "decomp_rejects": decomp_rejects,
    }

    # ── WO#46 A: 생애주기 단계(phase) 섹션 — transitions에서 파생(파생만, 새 엔진 필드 없음) ──
    integration_verdict = integration[-1]["verdict"] if integration else None
    phases = _derive_phases(
        transitions_list,
        running=running,
        unit_counts=unit_counts,
        integration_verdict=integration_verdict,
        or_alternatives=or_alternatives,
        n_approaches=len(approaches_list),
        critique=critique,
    )

    return {
        "status": state.status.value,
        "summary": summary,
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
        "activity": activity,
        "transitions": transitions,
        "cost": cost,
        "unit_rows": unit_rows,
        # v3.1(WO#44 C): #40/#41 산출물 표면화(부재/구버전 → 빈 리스트).
        "approaches": approaches_view,
        "decomp_critiques": decomp_critiques_view,
        # WO#46 A: 생애주기 단계 섹션(빈/구버전 state → [] 무크래시).
        "phases": phases,
    }


def _jsonable(obj: Any) -> Any:
    """pending_escalations 등 자유형(dict/모델/스칼라)을 JSON 가능 형태로."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    return obj


def _heartbeat_path(state_path: str | Path | None) -> Path | None:
    """state.yaml 옆 heartbeat.json 경로(WO#55 사이드카). state_path 없으면 None."""
    if state_path is None:
        return None
    return Path(state_path).parent / "heartbeat.json"


def load_heartbeat(state_path: str | Path | None) -> dict[str, Any] | None:
    """state.yaml 옆 heartbeat.json을 best-effort로 읽는다(WO#55, read-only).

    미생성/깨짐/부분쓰기는 *조용히 None*(에러 아님 — #44 빈 상태 패턴). 사이드카라
    실패해도 state 뷰엔 무영향. 반환 dict는 {updated_at, activities:[...]} 형태.
    """
    hb = _heartbeat_path(state_path)
    if hb is None:
        return None
    try:
        data = json.loads(hb.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    acts = data.get("activities")
    if not isinstance(acts, list):
        return None
    return data


def _heartbeat_active(heartbeat: dict[str, Any] | None) -> bool:
    """하트비트에 진행 중인 활동이 하나라도 있나(WO#66 ①: 합성/초기 활동 = 살아있음)."""
    if not heartbeat:
        return False
    acts = heartbeat.get("activities")
    return isinstance(acts, list) and len(acts) > 0


def _transcripts_path(state_path: str | Path | None) -> Path | None:
    """state.yaml 옆 transcripts.json 경로(WO#67 사이드카). state_path 없으면 None."""
    if state_path is None:
        return None
    return Path(state_path).parent / "transcripts.json"


def load_transcripts(state_path: str | Path | None) -> dict[str, Any] | None:
    """state.yaml 옆 transcripts.json을 best-effort로 읽는다(WO#67, read-only).

    미생성/깨짐/부분쓰기는 *조용히 None*(에러 아님 — #44/#55 빈 상태 패턴). 사이드카라
    실패해도 state 뷰엔 무영향. 반환 dict는 {updated_at, calls:[...]} 형태(스키마는 transcript.py).
    엔진을 import하지 않는다 — 사이드카 *파일*만 읽는다(read-only 위성 불변식 유지).
    """
    tp = _transcripts_path(state_path)
    if tp is None:
        return None
    try:
        data = json.loads(tp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    calls = data.get("calls")
    if not isinstance(calls, list):
        return None
    return data


def load_view(state_path: str | Path, spec_path: str | Path | None = None) -> dict[str, Any]:
    """state(+옵션 spec) 재로드 → view. 실패는 {error}로 흡수(서버 안 죽음).

    WO#55: 라이브 하트비트(heartbeat.json 사이드카)가 있으면 view["heartbeat"]로 동봉한다.
    state 로드 실패({error})에도 하트비트는 별도라 — 멈춤 진단을 위해 가능하면 같이 싣는다.

    WO#66 ①: state.yaml은 *합성 후* 생성된다. 합성 중엔 state가 아직 없어 FileNotFoundError가
    나는데, 그걸 빨간 에러로 보이면 "정상 부재"가 "고장"처럼 읽힌다. 그래서 **state.yaml이 아직
    없고(파일 부재) 하트비트에 활성 활동이 있으면** 에러가 아니라 `{synthesizing}` 정상 부재로
    구분한다(프런트가 차분한 "합성/준비 중" 패널을 띄움). 진짜 죽은 run(하트비트도 없고 state도
    없음)이나 파싱 에러(파일은 있는데 깨짐)는 그대로 {error}.
    """
    heartbeat = load_heartbeat(state_path)
    transcripts = load_transcripts(state_path)  # WO#67: 라이브 호출 트랜스크립트 사이드카(별도 파일)
    state_exists = False
    try:
        state_exists = Path(state_path).exists()
    except OSError:
        state_exists = False
    try:
        state = State.from_yaml(state_path)
    except Exception as e:  # noqa: BLE001 — 파일없음/파싱에러 전부 흡수
        # 합성 중 정상 부재(파일 자체가 아직 없음 + 하트비트 활성) → 에러 아님.
        if not state_exists and _heartbeat_active(heartbeat):
            syn: dict[str, Any] = {
                "synthesizing": True,
                "reason": "state.yaml 생성 전 (합성/준비 중)",
                "state_path": str(state_path),
                "heartbeat": heartbeat,
            }
            # WO#67: 합성 트랜스크립트(입력 프롬프트 + 라이브 출력)는 state 생성 *전*이 가장 값짐 —
            #   "합성이 오래 걸려도 지금 뭘 뱉는지"를 보이게 동봉(있으면).
            if transcripts is not None:
                syn["transcripts"] = transcripts
            return syn
        err: dict[str, Any] = {"error": f"{type(e).__name__}: {e}", "state_path": str(state_path)}
        if heartbeat is not None:
            err["heartbeat"] = heartbeat
        if transcripts is not None:
            err["transcripts"] = transcripts
        return err
    spec = None
    if spec_path:
        try:
            spec = ProjectSpec.from_yaml(spec_path)
        except Exception:  # noqa: BLE001 — spec은 옵션, 실패해도 state 뷰는 제공
            spec = None
    try:
        view = state_to_view(state, spec)
    except Exception as e:  # noqa: BLE001 — 변환 에러도 흡수
        view = {"error": f"view build failed: {type(e).__name__}: {e}"}
    if heartbeat is not None:
        view["heartbeat"] = heartbeat
    if transcripts is not None:
        view["transcripts"] = transcripts  # WO#67: 유닛/단계 드릴다운용 라이브 트랜스크립트
    return view


# ──────────────────── 제어 표면: 실행 레지스트리 + 서브프로세스 (WO#37) ────────────────────
#
# 안전 설계(위반 금지):
#   - 엔진 격리: loop/gate/executor를 import해 직접 돌리지 않고 `python -m haetae.run`을
#     별도 프로세스로 spawn할 뿐. 런처는 stdlib subprocess/signal/os만 쓴다.
#   - 제어 opt-in: allow_run일 때만 launch/stop. 기본은 read-only(엔진 무변).
#   - shell 금지: argv 리스트로 spawn(shell=True·문자열 보간 금지). order는 단일 argv 원소.
#   - 옵션 화이트리스트: 정해진 옵션·범위만. 미지 플래그·임의 값 거부.
#   - 경로 서버 생성: runs/<run-id>/ 아래를 서버가 만든다(사용자 입력 경로 안 받음). run-id는
#     서버 생성(타임스탬프+슬러그), 읽을 때 패턴 검증(`../` 등 거부 → traversal 차단).

_RUNS_DIR_DEFAULT = "runs"
_STOP_GRACE_S = 5.0  # SIGINT 후 정리 대기. 초과 시 SIGTERM→SIGKILL 에스컬레이트(best-effort).
# 라이브 작업 로그 tail(WO#42 C) — bounded. 끝에서 최대 N바이트만 읽고 마지막 N줄만 노출
# (대용량 로그 덤프 금지). 기본/상한을 둬 임의 tail 값으로도 안전하게 클램프.
_LOG_TAIL_DEFAULT_LINES = 200
_LOG_TAIL_MAX_LINES = 2000
_LOG_TAIL_MAX_BYTES = 256 * 1024
_EXECUTOR_CHOICES = ("codex", "human")
# codex 추론 강도 화이트리스트(WO#38). 엔진 격리 불변식상 providers.codex를 import하지
# 않으므로 여기서 미러링한다(run.py argparse + codex 헬퍼가 재검증 — 다중 가드).
_REASONING_EFFORT_CHOICES = ("minimal", "low", "medium", "high", "xhigh")
_RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")
_CRITIC_MODEL_RE = re.compile(r"[A-Za-z0-9._\-]+\Z")


def valid_run_id(run_id: str | None) -> bool:
    """run-id가 안전한 단일 경로 세그먼트인지(traversal 차단). `../`·`/`·빈값·선행점 거부."""
    if not run_id or ".." in run_id or "/" in run_id or "\\" in run_id:
        return False
    return bool(_RUN_ID_RE.match(run_id))


def _slugify(text: str, max_len: int = 24) -> str:
    words = re.findall(r"[A-Za-z0-9]+", (text or "").lower())
    slug = "-".join(words)[:max_len].strip("-")
    return slug or "run"


def generate_run_id(order: str, *, now: datetime | None = None) -> str:
    """타임스탬프+슬러그로 서버가 run-id 생성(결정론·사람이 읽기 쉬움)."""
    now = now or datetime.now(timezone.utc)
    return f"{now.strftime('%Y%m%d-%H%M%S')}-{_slugify(order)}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_options(raw: dict[str, Any] | None) -> dict[str, Any]:
    """런치 옵션을 화이트리스트/범위로 검증·정규화. 미지 키·범위 밖·타입 불일치는 ValueError.

    POST /api/run은 RCE 표면이므로 *정해진 옵션·범위만* 통과시킨다(임의 플래그 차단).
    `ALLOWED_SANDBOXES`/executor sandbox는 이 레이어가 건드리지 않는다 — 같은 엔진을 spawn할 뿐.
    """
    raw = raw or {}
    allowed = {
        "executor", "max_parallel", "run_timeout", "scaffold", "skills",
        "critic_model", "max_iters", "unit_retries", "reasoning_effort", "model",
        "auto",  # WO#65: 제로-config auto 모드(미설정 운영 knob 자동 해석). 기본 False(back-compat).
    }
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f"unknown option(s): {', '.join(sorted(unknown))}")

    def _int(name: str, default: int, lo: int, hi: int) -> int:
        v = raw.get(name, default)
        if isinstance(v, bool):  # bool은 int subclass — 옵션 오용 방지
            raise ValueError(f"{name} must be an integer")
        try:
            iv = int(v)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be an integer")
        if not (lo <= iv <= hi):
            raise ValueError(f"{name} out of range [{lo}, {hi}]")
        return iv

    def _bool(name: str, default: bool) -> bool:
        v = raw.get(name, default)
        if not isinstance(v, bool):
            raise ValueError(f"{name} must be a boolean")
        return v

    executor = raw.get("executor", "codex")
    if executor not in _EXECUTOR_CHOICES:
        raise ValueError(f"executor must be one of {_EXECUTOR_CHOICES}")

    rt = raw.get("run_timeout", 120.0)
    if isinstance(rt, bool):
        raise ValueError("run_timeout must be a number")
    try:
        run_timeout = float(rt)
    except (TypeError, ValueError):
        raise ValueError("run_timeout must be a number")
    if not (1.0 <= run_timeout <= 3600.0):
        raise ValueError("run_timeout out of range [1, 3600]")

    critic_model = raw.get("critic_model")
    if critic_model is not None:
        if not isinstance(critic_model, str):
            raise ValueError("critic_model must be a string")
        critic_model = critic_model.strip() or None
        if critic_model and not _CRITIC_MODEL_RE.match(critic_model):
            raise ValueError("critic_model has invalid characters")

    # model(WO#45): provider 모델 override. 미설정(None/빈문자)이면 argv에 `--model` 미부착
    # → codex 기본(최신) 자동(기존 동작 불변). critic_model과 같은 안전 문자 집합으로 검증.
    model = raw.get("model")
    if model is not None:
        if not isinstance(model, str):
            raise ValueError("model must be a string")
        model = model.strip() or None
        if model and not _CRITIC_MODEL_RE.match(model):
            raise ValueError("model has invalid characters")

    # reasoning_effort(WO#38): 미설정(None/빈문자)이면 플래그 미부착 → codex 기본(기존 동작
    # 불변). 설정 시 화이트리스트 밖이면 거부. sandbox 권한과 무관(가드 불변).
    reasoning_effort = raw.get("reasoning_effort")
    if reasoning_effort is not None:
        if not isinstance(reasoning_effort, str):
            raise ValueError("reasoning_effort must be a string")
        reasoning_effort = reasoning_effort.strip() or None
        if reasoning_effort and reasoning_effort not in _REASONING_EFFORT_CHOICES:
            raise ValueError(f"reasoning_effort must be one of {_REASONING_EFFORT_CHOICES}")

    return {
        "executor": executor,
        "max_parallel": _int("max_parallel", 4, 1, 16),
        "run_timeout": run_timeout,
        "scaffold": _bool("scaffold", True),
        "skills": _bool("skills", True),
        "critic_model": critic_model,
        "max_iters": _int("max_iters", 30, 1, 200),
        "unit_retries": _int("unit_retries", 2, 0, 10),
        "reasoning_effort": reasoning_effort,
        "model": model,
        "auto": _bool("auto", False),  # WO#65: 기본 OFF(back-compat). 켜면 run.py가 운영 knob 자동 해석.
    }


def build_run_argv(
    order: str, run_dir: Path, opts: dict[str, Any], parent_run_dir: Path | None = None
) -> list[str]:
    """서브프로세스 argv 리스트 — shell 아님, order는 단일 argv 원소, 경로는 서버 생성.

    workdir/state-path를 runs/<id>/ 아래로 강제(사용자 입력 경로 안 받음 → traversal 차단).

    parent_run_dir(WO#58 이어가기 ②a): 주어지면 `--continue-from <부모 dir>`를 부착하고
    **scaffold를 강제 OFF**(스택은 시딩으로 이미 존재). 부모 경로는 서버가 해석한 절대 경로.
    """
    argv = [
        sys.executable, "-m", "haetae.run",
        "--order", order,
        "--workdir", str(run_dir / "work"),
        "--state-path", str(run_dir / "state.yaml"),
        "--executor", opts["executor"],
        "--max-parallel", str(opts["max_parallel"]),
        "--run-timeout", str(opts["run_timeout"]),
        "--max-iters", str(opts["max_iters"]),
        "--unit-retries", str(opts["unit_retries"]),
    ]
    # WO#65: auto 모드면 --auto 부착 → run.py가 미설정 운영 knob 자동 해석(명시 옵션은 그대로 오버라이드).
    if opts.get("auto"):
        argv.append("--auto")
    if parent_run_dir is not None:
        argv += ["--continue-from", str(parent_run_dir)]
        argv.append("--no-scaffold")  # 이어가기: 스택 이미 시딩 → scaffold 스킵(opts 무시)
    else:
        argv.append("--scaffold" if opts["scaffold"] else "--no-scaffold")
    argv.append("--skills" if opts["skills"] else "--no-skills")
    if opts["critic_model"]:
        argv += ["--critic-model", opts["critic_model"]]
    # model(WO#45): 설정 시만 `--model` 부착. 비우면 미부착 → codex 기본(최신) 자동(기존 동작 불변).
    if opts.get("model"):
        argv += ["--model", opts["model"]]
    # reasoning_effort: 설정 시만 플래그 부착. 미설정이면 codex 기본(기존 동작 불변).
    if opts.get("reasoning_effort"):
        argv += ["--reasoning-effort", opts["reasoning_effort"]]
    return argv


class RunManager:
    """런 레지스트리 + 서브프로세스 런처/스토퍼 (엔진 격리 — subprocess만).

    엔진(loop/gate/executor)을 import하지 않고 `python -m haetae.run`을 *별도 프로세스*로
    spawn한다. 제어(launch/stop)는 allow_run일 때만; 읽기(list/state 타겟팅)는 항상.
    상태는 라이브로 lazily 해석(popen.poll) — 백그라운드 스레드 없이도 정직.
    """

    def __init__(
        self,
        runs_dir: str | Path = _RUNS_DIR_DEFAULT,
        *,
        allow_run: bool = False,
        stop_grace_s: float = _STOP_GRACE_S,
    ) -> None:
        self.runs_dir = Path(runs_dir).resolve()
        self.allow_run = allow_run
        self.stop_grace_s = stop_grace_s
        self._runs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    # ── 경로 헬퍼 ──
    def run_dir(self, run_id: str) -> Path:
        return self.runs_dir / run_id

    def state_path_for(self, run_id: str) -> Path | None:
        """뷰어 타겟팅: ?run=<id> → runs/<id>/state.yaml. 패턴 검증 실패는 None."""
        if not valid_run_id(run_id):
            return None
        return self.run_dir(run_id) / "state.yaml"

    def spec_path_for(self, run_id: str) -> Path | None:
        """spec.yaml 사이드카(WO#58) 자동탐지 → goal/done_when/유닛 desc 보강. 없으면 None."""
        if not valid_run_id(run_id):
            return None
        sp = self.run_dir(run_id) / "spec.yaml"
        return sp if sp.exists() else None

    # ── 라이브 작업 로그 tail (WO#42 C) — read-only, 경로 안전, bounded ──
    def read_log_tail(self, run_id: str, tail: int | str | None = None) -> dict[str, Any]:
        """runs/<id>/run.log의 마지막 tail줄을 **bounded**하게 반환(read-only).

        안전 설계:
          - 경로: valid_run_id로 traversal 차단(`../`·`/` 등 거부). runs/<id>/run.log만.
          - bounded: 파일 끝에서 최대 _LOG_TAIL_MAX_BYTES만 읽고 마지막 N줄만(대용량 덤프 금지).
            tail은 [1, _LOG_TAIL_MAX_LINES]로 클램프. bytes 컷에 걸린 부분 첫 줄은 버린다.
          - 무크래시: 로그 부재/읽기 실패는 빈 lines로 흡수(서버 안 죽음).
        반환: {lines:[...], missing:bool, truncated:bool, size:int}  (실패 시 error 동봉).
        """
        if not valid_run_id(run_id):
            return {"error": "invalid run id", "lines": [], "missing": False, "truncated": False}
        n = _LOG_TAIL_DEFAULT_LINES
        if tail is not None:
            try:
                n = int(tail)
            except (TypeError, ValueError):
                n = _LOG_TAIL_DEFAULT_LINES
        n = max(1, min(n, _LOG_TAIL_MAX_LINES))
        path = self.run_dir(run_id) / "run.log"
        try:
            size = path.stat().st_size
        except OSError:
            return {"lines": [], "missing": True, "truncated": False, "size": 0}
        read_bytes = min(size, _LOG_TAIL_MAX_BYTES)
        try:
            with open(path, "rb") as f:  # noqa: SIM115 — with로 즉시 닫음
                if size > read_bytes:
                    f.seek(size - read_bytes)
                data = f.read(read_bytes)
        except OSError as e:
            return {"lines": [], "missing": False, "truncated": False,
                    "error": f"read failed: {e}", "size": size}
        text = data.decode("utf-8", errors="replace")
        lines = text.splitlines()
        byte_cut = size > read_bytes
        if byte_cut and lines:
            lines = lines[1:]  # bytes 경계로 잘린 부분 첫 줄 제거(깔끔한 tail)
        truncated = byte_cut or len(lines) > n
        lines = lines[-n:]
        return {"lines": lines, "missing": False, "truncated": truncated, "size": size}

    # ── meta 영속 ──
    @staticmethod
    def _meta_path(run_dir: Path) -> Path:
        return run_dir / "meta.json"

    def _write_meta(self, run_dir: Path, meta: dict[str, Any]) -> None:
        try:
            self._meta_path(run_dir).write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass  # 영속 실패가 런을 막지 않는다

    def _read_meta(self, run_dir: Path) -> dict[str, Any] | None:
        try:
            return json.loads(self._meta_path(run_dir).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    @staticmethod
    def _close_log(rec: dict[str, Any]) -> None:
        logf = rec.get("logf")
        if logf is not None:
            try:
                logf.close()
            except OSError:
                pass
            rec["logf"] = None

    # ── launch ──
    def _unique_run_id(self, order: str) -> str:
        base = generate_run_id(order)
        run_id = base
        n = 2
        while self.run_dir(run_id).exists() or run_id in self._runs:
            run_id = f"{base}-{n}"
            n += 1
        return run_id

    def launch(
        self,
        order: str | None,
        options: dict[str, Any] | None = None,
        parent_run_id: str | None = None,
    ) -> str:
        """입력 검증 → run-id 생성 → runs/<id>/ 생성 → spawn. run-id 반환.

        order는 단일 argv 원소로 전달(shell 보간 없음). 옵션은 화이트리스트 통과만.

        parent_run_id(WO#58 이어가기 ②a): 주어지면 부모 run-id를 검증(traversal 차단 +
        존재 확인)하고 `--continue-from <부모 dir>`로 spawn(scaffold 강제 OFF), meta에 계보 기록.
        부모 없음/잘못된 id는 ValueError(명확한 에러).
        """
        if not order or not isinstance(order, str) or not order.strip():
            raise ValueError("order must be a non-empty string")
        opts = validate_options(options)
        parent_run_dir: Path | None = None
        if parent_run_id:
            if not valid_run_id(parent_run_id):  # traversal 차단(../ 등)
                raise ValueError("parent_run_id has invalid characters")
            parent_run_dir = self.run_dir(parent_run_id)
            if not (parent_run_dir / "state.yaml").exists():
                raise ValueError(f"parent run not found: {parent_run_id}")
        run_id = self._unique_run_id(order)
        rdir = self.run_dir(run_id)
        (rdir / "work").mkdir(parents=True, exist_ok=True)
        argv = build_run_argv(order, rdir, opts, parent_run_dir=parent_run_dir)
        # stdout/stderr → runs/<id>/run.log (장수명 핸들 — 런 동안 유지, 종료 시 close).
        logf = open(rdir / "run.log", "wb")  # noqa: SIM115
        try:
            # argv 리스트 spawn(shell 아님). cwd 상속 — 경로는 모두 절대(runs_dir.resolve()).
            popen = subprocess.Popen(argv, stdout=logf, stderr=subprocess.STDOUT)  # noqa: S603
        except Exception:
            logf.close()
            raise
        started_at = _now_iso()
        meta = {
            "id": run_id, "order": order, "options": opts,
            "started_at": started_at, "status": "running", "argv": argv,
        }
        if parent_run_id:
            meta["parent_run_id"] = parent_run_id  # WO#58 계보(append-only 감사·대시보드 트리)
        self._write_meta(rdir, meta)
        with self._lock:
            self._runs[run_id] = {
                "popen": popen, "pid": getattr(popen, "pid", None), "status": "running",
                "started_at": started_at, "order": order, "options": opts,
                "state_path": str(rdir / "state.yaml"), "logf": logf,
            }
        return run_id

    # ── stop ──
    def stop(self, run_id: str) -> bool:
        """SIGINT 먼저(루프 try/finally 정리가 Ctrl-C처럼 돌 기회) → grace 후 에스컬레이트.

        best-effort: hard-kill 시 worktree 잔존 가능(#21 정리는 인터럽트에 best-effort).
        """
        with self._lock:
            rec = self._runs.get(run_id)
        if rec is None:
            return False  # 이 대시보드가 관리하지 않는(또는 재시작으로 핸들 잃은) 런
        popen = rec["popen"]
        try:
            popen.send_signal(signal.SIGINT)  # ① SIGINT
        except (ProcessLookupError, OSError):
            pass
        try:
            popen.wait(timeout=self.stop_grace_s)
        except subprocess.TimeoutExpired:
            try:
                popen.terminate()  # ② SIGTERM
                popen.wait(timeout=self.stop_grace_s)
            except subprocess.TimeoutExpired:
                try:
                    popen.kill()  # ③ SIGKILL
                except OSError:
                    pass
            except OSError:
                pass
        except OSError:
            pass
        rec["status"] = "stopped"
        self._close_log(rec)
        self._persist_status(run_id, "stopped")
        return True

    # ── 상태 해석 ──
    def _persist_status(self, run_id: str, status: str) -> None:
        rdir = self.run_dir(run_id)
        meta = self._read_meta(rdir)
        if meta is not None and meta.get("status") != status:
            meta["status"] = status
            self._write_meta(rdir, meta)

    def _live_status(self, run_id: str, rec: dict[str, Any]) -> str:
        if rec["status"] == "stopped":
            return "stopped"
        try:
            code = rec["popen"].poll()
        except OSError:
            return "unknown"
        if code is None:
            return "running"
        terminal = "finished" if code == 0 else "failed"
        rec["status"] = terminal
        self._close_log(rec)
        self._persist_status(run_id, terminal)
        return terminal

    def status_of(self, run_id: str, meta: dict[str, Any] | None = None) -> str:
        """라이브 핸들이 있으면 poll로 해석; 없고 meta가 running이면 '미상'(정직)."""
        with self._lock:
            rec = self._runs.get(run_id)
        if rec is not None:
            return self._live_status(run_id, rec)
        if meta is None:
            meta = self._read_meta(self.run_dir(run_id)) or {}
        st = meta.get("status")
        if st in (None, "running"):
            return "unknown"  # 핸들 잃음(대시보드 재시작 등) → 상태 미상으로 정직 표기
        return st

    def list_runs(self) -> list[dict[str, Any]]:
        """runs/ 디스크에서 meta.json 읽어 목록+상태. 읽기 — allow_run 불필요."""
        out: list[dict[str, Any]] = []
        if not self.runs_dir.exists():
            return out
        try:
            entries = list(self.runs_dir.iterdir())
        except OSError:
            return out
        for d in entries:
            try:
                if not d.is_dir() or not valid_run_id(d.name):
                    continue
            except OSError:
                continue
            meta = self._read_meta(d)
            if meta is None:
                continue
            out.append({
                "id": meta.get("id", d.name),
                "order": meta.get("order"),
                "options": meta.get("options"),
                "started_at": meta.get("started_at"),
                "status": self.status_of(d.name, meta),
                "parent_run_id": meta.get("parent_run_id"),  # WO#58 계보(없으면 None)
            })
        out.sort(key=lambda r: r.get("started_at") or "", reverse=True)
        return out


# ──────────────────────────── 얇은 stdlib 서버 ────────────────────────────


def _index_html(poll_ms: int, allow_run: bool = False) -> bytes:
    try:
        html = INDEX_HTML_PATH.read_text(encoding="utf-8")
    except OSError:
        html = "<!doctype html><meta charset=utf-8><p>dashboard.html 없음</p>"
    return (
        html.replace("__POLL_MS__", str(poll_ms))
        .replace("__ALLOW_RUN__", "true" if allow_run else "false")
        .encode("utf-8")
    )


def make_handler(
    state_path: str | Path | None,
    spec_path: str | Path | None,
    poll_ms: int,
    stream_interval: float = 1.0,
    run_manager: RunManager | None = None,
) -> type[BaseHTTPRequestHandler]:
    allow_run = bool(run_manager and run_manager.allow_run)

    def _view_payload(
        sp: str | Path | None, spec: str | Path | None, err: str | None = None
    ) -> bytes:
        # A(WO#44): run 미선택 + state-path 없음은 '에러'가 아니라 '빈 상태'(차분한 안내).
        # 단 명시된 run의 패턴 검증 실패(traversal 등)나 실제 로드 실패는 {error}로 구분.
        if err is not None:
            view: dict[str, Any] = {"error": err}
        elif sp is None:
            view = {"empty": True, "reason": "no run selected"}
        else:
            view = load_view(sp, spec)
        return json.dumps(view, ensure_ascii=False, default=str).encode("utf-8")

    class DashboardHandler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, code: int, obj: Any) -> None:
            self._send(
                code,
                json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"),
                "application/json; charset=utf-8",
            )

        def _resolve_target(
            self,
        ) -> tuple[str | Path | None, str | Path | None, str | None]:
            """?run=<id> → runs/<id>/state.yaml(타겟팅, spec 보강 없음); 없으면 기본 --state-path.

            반환 (sp, spec, err). run-id 패턴 검증 실패는 err="invalid run id"(traversal 차단,
            빈 상태 아닌 에러). run 미명시 + state-path 없음은 (None, None, None) → 빈 상태.
            """
            query = urllib.parse.urlsplit(self.path).query
            run = (urllib.parse.parse_qs(query).get("run") or [None])[0]
            if run:
                sp = run_manager.state_path_for(run) if run_manager else None
                err = None if sp is not None else "invalid run id"
                # WO#58: spec.yaml 사이드카 자동탐지 → goal/done_when/유닛 desc 보강(없으면 기존 동작).
                spec_p = run_manager.spec_path_for(run) if (run_manager and sp is not None) else None
                return (sp, spec_p, err)
            return (state_path, spec_path, None)

        def _stream(
            self, sp: str | Path | None, spec: str | Path | None, err: str | None = None
        ) -> None:
            """SSE: state.yaml mtime을 폴링해 변경 시 새 view를 push(라이브).

            ThreadingHTTPServer가 요청마다 (daemon) 스레드를 주므로 장수명 응답이 가능.
            파일 부재도 {error} view를 한 번 push(서버 안 죽음). 클라이언트 끊김은
            BrokenPipe로 조용히 종료. ~15s 하트비트 주석으로 끊김 감지 + 연결 유지.
            """
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")  # 프록시 버퍼링 비활성
            self.end_headers()
            last_sig: Any = object()  # 센티넬 → 첫 루프에서 무조건 push
            idle = 0
            ping_every = max(1, int(15.0 / stream_interval))
            hb_path = _heartbeat_path(sp)
            tr_path = _transcripts_path(sp)  # WO#67: 트랜스크립트 사이드카도 감시(라이브 출력 tail)
            try:
                while True:
                    # WO#55: state.yaml + heartbeat.json mtime 둘 다 감시 → 라이브 하트비트가
                    #   단계 경계 사이(state 미변경)에도 배너를 갱신(멈춤 차오름이 보이게).
                    # WO#66 ①: 둘을 *독립적으로* 본다 — state가 아직 없어도(합성 중) heartbeat
                    #   mtime 변화가 push를 트리거해 "합성/준비 중" 패널이 라이브로 갱신되게.
                    #   (이전엔 state getmtime이 OSError면 hb_m을 못 잡아 합성 중 SSE가 한 번 뒤 침묵)
                    try:
                        st_m = os.path.getmtime(sp) if sp is not None else None
                    except OSError:
                        st_m = None  # state 아직 없음(합성 전) — 에러 아님
                    try:
                        hb_m = os.path.getmtime(hb_path) if hb_path is not None else None
                    except OSError:
                        hb_m = None
                    try:
                        tr_m = os.path.getmtime(tr_path) if tr_path is not None else None
                    except OSError:
                        tr_m = None
                    sig = (st_m, hb_m, tr_m)
                    if sig != last_sig:
                        last_sig = sig
                        self.wfile.write(b"data: " + _view_payload(sp, spec, err) + b"\n\n")
                        self.wfile.flush()
                        idle = 0
                    else:
                        idle += 1
                        if idle >= ping_every:  # 하트비트(끊김 감지/keepalive)
                            self.wfile.write(b": ping\n\n")
                            self.wfile.flush()
                            idle = 0
                    time.sleep(stream_interval)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return  # 클라이언트 끊김 — 조용히 종료

        def _read_json_body(self) -> dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length", 0))
            except (TypeError, ValueError):
                length = 0
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            obj = json.loads(raw.decode("utf-8"))
            return obj if isinstance(obj, dict) else {}

        def do_GET(self) -> None:  # noqa: N802 — http.server 계약
            path = urllib.parse.urlsplit(self.path).path
            if path == "/api/state":
                sp, spec, err = self._resolve_target()
                self._send(200, _view_payload(sp, spec, err), "application/json; charset=utf-8")
            elif path == "/api/stream":
                sp, spec, err = self._resolve_target()
                self._stream(sp, spec, err)
            elif path == "/api/runs":
                runs = run_manager.list_runs() if run_manager else []
                self._send_json(200, {"runs": runs, "allow_run": allow_run})
            elif path == "/api/launch-options":
                # provider가 선언한 실행 폼 옵션 디스크립터(WO#45) — read-only·best-effort.
                # config pre-fill을 매 요청 신선하게 반영. 실패해도 정적 기본으로 폴백(무크래시).
                self._handle_launch_options()
            elif path.startswith("/api/runs/") and path.endswith("/log"):
                self._handle_log(path[len("/api/runs/"):-len("/log")])
            elif path == "/":
                self._send(200, _index_html(poll_ms, allow_run), "text/html; charset=utf-8")
            else:
                self._send(404, b'{"error":"not found"}', "application/json; charset=utf-8")

        def do_POST(self) -> None:  # noqa: N802 — http.server 계약
            path = urllib.parse.urlsplit(self.path).path
            if path == "/api/run":
                self._handle_launch()
            elif path.startswith("/api/run/") and path.endswith("/stop"):
                self._handle_stop(path[len("/api/run/"):-len("/stop")])
            else:
                self._send_json(404, {"error": "not found"})

        def _handle_launch(self) -> None:
            # 제어 opt-in: --allow-run 없으면 read-only 디폴트 유지(403).
            if run_manager is None or not run_manager.allow_run:
                self._send_json(
                    403, {"error": "control disabled — start with --allow-run (read-only by default)"}
                )
                return
            try:
                body = self._read_json_body()
            except (ValueError, OSError):
                self._send_json(400, {"error": "invalid JSON body"})
                return
            try:
                run_id = run_manager.launch(
                    body.get("order"), body.get("options"),
                    parent_run_id=body.get("parent_run_id"),  # WO#58 이어가기(②a)
                )
            except ValueError as e:
                self._send_json(400, {"error": str(e)})  # 화이트리스트/범위/빈 order 거부
                return
            except Exception as e:  # noqa: BLE001 — spawn 실패도 흡수(서버 안 죽음)
                self._send_json(500, {"error": f"launch failed: {type(e).__name__}: {e}"})
                return
            self._send_json(200, {"run_id": run_id})

        def _handle_launch_options(self) -> None:
            # provider 디스크립터를 JSON으로(폼이 읽어 렌더). best-effort: config 읽기 실패도
            # launch_options 내부에서 정적 기본으로 흡수하므로 여기선 추가 폴백만 둔다.
            try:
                executors = all_launch_options()
            except Exception as e:  # noqa: BLE001 — 디스크립터 빌드 실패도 서버를 안 죽인다
                self._send_json(
                    500, {"error": f"launch options failed: {type(e).__name__}: {e}", "executors": {}}
                )
                return
            self._send_json(200, {"executors": executors})

        def _handle_log(self, run_id: str) -> None:
            # 라이브 작업 로그 tail(WO#42 C) — read-only(allow_run 불필요, /api/runs와 동급).
            # 경로 안전(valid_run_id)·bounded는 read_log_tail이 강제. 부재/실패는 흡수.
            if run_manager is None:
                self._send_json(404, {"error": "no run manager (use --runs-dir)", "lines": []})
                return
            if not valid_run_id(run_id):
                self._send_json(400, {"error": "invalid run id", "lines": []})
                return
            query = urllib.parse.urlsplit(self.path).query
            tail = (urllib.parse.parse_qs(query).get("tail") or [None])[0]
            try:
                payload = run_manager.read_log_tail(run_id, tail)
            except Exception as e:  # noqa: BLE001 — 로그 읽기 실패도 흡수(서버 안 죽음)
                self._send_json(500, {"error": f"log read failed: {type(e).__name__}: {e}", "lines": []})
                return
            self._send_json(200, payload)

        def _handle_stop(self, run_id: str) -> None:
            if run_manager is None or not run_manager.allow_run:
                self._send_json(403, {"error": "control disabled — start with --allow-run"})
                return
            if not valid_run_id(run_id):
                self._send_json(400, {"error": "invalid run id"})
                return
            try:
                ok = run_manager.stop(run_id)
            except Exception as e:  # noqa: BLE001
                self._send_json(500, {"error": f"stop failed: {type(e).__name__}: {e}"})
                return
            if not ok:
                self._send_json(404, {"error": "run not found or not managed by this dashboard"})
                return
            self._send_json(200, {"run_id": run_id, "status": "stopped"})

        def log_message(self, *args: Any) -> None:  # 조용히(접속 로그 억제)
            return

    return DashboardHandler


# SSE는 장수명 연결이라 브라우저 재연결/탭닫기/새로고침으로 *예상되게* 끊긴다. 이때
# stdlib(socketserver)는 ConnectionResetError 등을 traceback으로 찍어 무해한 끊김이
# 에러처럼 보인다. 예상된 끊김만 조용히 무시하고, **그 외 예외는 그대로 surface**한다
# (진짜 에러를 숨기면 안 됨). #37이 BrokenPipe는 SSE write에서 잡았고, 여기선 서버
# 레벨에서 ConnectionReset/Aborted까지 동급으로 조용히 흡수한다.
_EXPECTED_DISCONNECTS = (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)


class _QuietThreadingHTTPServer(ThreadingHTTPServer):
    """예상된 클라이언트 끊김의 traceback만 억제하는 ThreadingHTTPServer.

    handle_error는 요청 처리 중 핸들러에서 예외가 새어나올 때 socketserver가 부른다.
    예상된 끊김이면 조용히(traceback/raise 없이) 무시, 그 외는 super()로 surface.
    """

    def handle_error(self, request: Any, client_address: Any) -> None:  # noqa: N802 — socketserver 계약
        exc = sys.exc_info()[1]
        if isinstance(exc, _EXPECTED_DISCONNECTS):
            return  # 예상된 끊김 — traceback 없이 조용히(무해). 진짜 에러가 아님.
        super().handle_error(request, client_address)


def serve(
    state_path: str | Path | None = None,
    *,
    spec_path: str | Path | None = None,
    host: str = "127.0.0.1",
    port: int = 8000,
    poll_interval: float = 2.0,
    stream_interval: float = 1.0,
    allow_run: bool = False,
    runs_dir: str | Path = _RUNS_DIR_DEFAULT,
) -> None:
    run_manager = RunManager(runs_dir, allow_run=allow_run)
    handler = make_handler(
        state_path, spec_path, int(poll_interval * 1000),
        stream_interval=stream_interval, run_manager=run_manager,
    )
    # 예상된 SSE 끊김 traceback을 억제하는 서브클래스(그 외 예외는 surface). localhost 바인드.
    httpd = _QuietThreadingHTTPServer((host, port), handler)
    print(
        f"haetae dashboard → http://{host}:{port}  (SSE {stream_interval}s, runs: {run_manager.runs_dir})",
        flush=True,
    )
    if host not in ("127.0.0.1", "localhost", "::1"):
        # launch는 RCE 표면 — 공개 노출 금지(WO#37 안전 설계).
        print("⚠ 경고: localhost가 아닌 호스트에 바인드됨. launch는 RCE 표면이다 — 공개 호스팅 금지.", flush=True)
    if allow_run:
        print("⚠ 제어 활성(--allow-run): 웹 폼으로 run 띄움/정지 가능. localhost 전용 유지 권장.", flush=True)
    else:
        print("read-only(기본) · live(SSE) — 제어 비활성. 켜려면 --allow-run. Ctrl-C 종료", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="haetae.dashboard",
        description="state.yaml 웹 대시보드 (read-only 기본; --allow-run으로 launch/stop 제어)",
    )
    parser.add_argument(
        "--state-path", default=None,
        help="기본 뷰 State YAML 경로(레거시/외부 run). 제어 모드에선 생략 가능(?run=<id>로 타겟)",
    )
    parser.add_argument("--spec-path", default=None, help="ProjectSpec YAML(옵션 · goal/desc 보강)")
    parser.add_argument("--host", default="127.0.0.1", help="바인드 호스트(기본 localhost)")
    parser.add_argument("--port", type=int, default=8000, help="포트(기본 8000)")
    parser.add_argument(
        "--poll-interval", type=float, default=2.0, help="프런트 폴링 폴백 간격 초(기본 2)"
    )
    parser.add_argument(
        "--stream-interval", type=float, default=1.0,
        help="SSE 서버측 state.yaml mtime 폴링 간격 초(기본 1, 라이브 갱신 주기)",
    )
    parser.add_argument(
        "--allow-run", action="store_true",
        help=(
            "제어 표면 활성: 웹 폼으로 `python -m haetae.run`을 서브프로세스로 launch/stop. "
            "기본 off=read-only(launch/stop 403, 엔진 무변). localhost 전용 — 공개 노출 금지."
        ),
    )
    parser.add_argument(
        "--runs-dir", default=_RUNS_DIR_DEFAULT,
        help="런 산출물 디렉토리(runs/<id>/work·state.yaml·run.log·meta.json). 기본 runs/",
    )
    args = parser.parse_args(argv)
    serve(
        args.state_path,
        spec_path=args.spec_path,
        host=args.host,
        port=args.port,
        poll_interval=args.poll_interval,
        stream_interval=args.stream_interval,
        allow_run=args.allow_run,
        runs_dir=args.runs_dir,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
