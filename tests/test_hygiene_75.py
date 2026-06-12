"""WO#75 — 소소한 위생 묶음 테스트 (stale-status · .omx gitignore · CLI order 사이드카).

전부 추가형/표시/gitignore — gate/judge 판정·codex 실행·ALLOWED_SANDBOXES 무접촉.
mock/실git만, 네트워크 없음.
"""

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from haetae.dashboard import (
    RunManager,
    detect_stale_run,
    load_meta,
    load_view,
)
from haetae.deps import GITIGNORE_ENTRIES, _ensure_gitignore
from haetae.models import Status
from haetae.run import _write_cli_meta


# ──────────────────────────── A. stale-status ────────────────────────────


def _hb(idle_timeout, last_event_at):
    return {"updated_at": "2026-06-12T00:00:00Z",
            "activities": [{"call_kind": "빌드", "unit": "u1",
                            "last_event_at": last_event_at, "idle_timeout": idle_timeout}]}


def test_stale_detected_when_running_and_heartbeat_old():
    """running + last_event_at가 idle-timeout 2× 초과 → stale."""
    now = datetime(2026, 6, 12, 0, 5, 0, tzinfo=timezone.utc)
    # idle_timeout=60, last_event 4분 전(240s) ≥ 2×60 → stale
    hb = _hb(60, "2026-06-12T00:01:00Z")
    out = detect_stale_run("running", hb, now)
    assert out is not None and out["stale"] is True
    assert out["idle_age_s"] == 240.0
    assert "응답 없음" in out["reason"]


def test_fresh_run_not_stale():
    """신선한 run(최근 이벤트) → stale 아님."""
    now = datetime(2026, 6, 12, 0, 5, 0, tzinfo=timezone.utc)
    hb = _hb(60, "2026-06-12T00:04:50Z")  # 10s 전 < 2×60
    assert detect_stale_run("running", hb, now) is None


def test_non_running_never_stale():
    now = datetime(2026, 6, 12, 0, 5, 0, tzinfo=timezone.utc)
    hb = _hb(60, "2026-06-12T00:01:00Z")  # old, 하지만 done이면 stale 아님
    assert detect_stale_run("done", hb, now) is None
    assert detect_stale_run("stopped_interrupted", hb, now) is None


def test_no_heartbeat_or_missing_fields_graceful():
    now = datetime(2026, 6, 12, 0, 5, 0, tzinfo=timezone.utc)
    assert detect_stale_run("running", None, now) is None  # 하트비트 없음
    assert detect_stale_run("running", {"activities": []}, now) is None  # 활동 없음
    # idle_timeout/last_event_at 미상 → 판정 불가(구버전 graceful)
    assert detect_stale_run("running", {"activities": [{"call_kind": "x"}]}, now) is None
    assert detect_stale_run("running", _hb(60, "2026-06-12T00:01:00Z"), None) is None


def test_stopped_interrupted_enum_exists():
    """추가형 enum — 사용자 중단(SIGINT) 상태가 막힘과 구분된다."""
    assert Status.stopped_interrupted.value == "stopped_interrupted"
    # 구버전 read 무영향: 기존 enum 멤버 보존
    assert Status.stopped_stuck.value == "stopped_stuck"


def test_load_view_attaches_stale(tmp_path):
    """load_view가 running state + old heartbeat에 stale을 동봉(스모크)."""
    sp = tmp_path / "state.yaml"
    sp.write_text("spec_ref: x\nspec_version: 1\nstatus: running\n", encoding="utf-8")
    # heartbeat.json 사이드카(아주 오래된 last_event)
    old = "2000-01-01T00:00:00Z"
    (tmp_path / "heartbeat.json").write_text(
        json.dumps(_hb(60, old)), encoding="utf-8")
    view = load_view(sp)
    assert view.get("stale", {}).get("stale") is True


# ──────────────────────────── B. .omx gitignore ────────────────────────────


def test_omx_in_gitignore_entries():
    assert ".omx/" in GITIGNORE_ENTRIES


def test_omx_files_untracked_in_worktree(tmp_path):
    """`.gitignore`에 .omx/가 있으면 .omx/ 하위가 git에서 무시돼 머지 충돌 안 남."""
    wd = tmp_path
    subprocess.run(["git", "init", "-q"], cwd=wd, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=wd, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=wd, check=True)
    # 로컬 oh-my-codex 노이즈 시뮬
    (wd / ".omx" / "logs").mkdir(parents=True)
    (wd / ".omx" / "logs" / "run.log").write_text("noise", encoding="utf-8")
    (wd / "src.txt").write_text("real", encoding="utf-8")
    _ensure_gitignore(wd)  # #23 패턴 — .omx/ 포함
    assert ".omx/" in (wd / ".gitignore").read_text(encoding="utf-8")
    # git status: .omx/ 는 무시되고 .gitignore + src.txt만 untracked로 잡힘.
    out = subprocess.run(
        ["git", "status", "--porcelain", "--ignored"], cwd=wd,
        capture_output=True, text=True, check=True).stdout
    assert "!! .omx/" in out                    # 무시됨(ignored)
    assert ".omx/logs/run.log" not in out.replace("!! ", "")  # untracked로 안 잡힘
    # add -A 해도 .omx/는 staging 안 됨(머지 정합)
    subprocess.run(["git", "add", "-A"], cwd=wd, check=True)
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"], cwd=wd,
        capture_output=True, text=True, check=True).stdout
    assert ".omx" not in staged
    assert "src.txt" in staged


# ──────────────────────────── C. CLI order 사이드카 ────────────────────────────


def test_write_cli_meta_creates_sidecar(tmp_path):
    """CLI run이 state.yaml 옆 meta.json(order 포함, 런처 형식)을 기록한다."""
    sp = tmp_path / "state.yaml"
    _write_cli_meta(sp, "내가 친 원 주문")
    meta = json.loads((tmp_path / "meta.json").read_text(encoding="utf-8"))
    assert meta["order"] == "내가 친 원 주문"
    assert meta["id"] == tmp_path.name
    assert meta["status"] == "running"
    assert "started_at" in meta


def test_write_cli_meta_does_not_clobber_launcher_meta(tmp_path):
    """런처가 이미 쓴 더 풍부한 meta(options/argv)를 덮지 않는다(추가형·비파괴)."""
    sp = tmp_path / "state.yaml"
    rich = {"id": "x", "order": "런처 주문", "options": {"executor": "codex"}, "argv": ["a"]}
    (tmp_path / "meta.json").write_text(json.dumps(rich), encoding="utf-8")
    _write_cli_meta(sp, "CLI가 덮어쓰려는 주문")
    meta = json.loads((tmp_path / "meta.json").read_text(encoding="utf-8"))
    assert meta["order"] == "런처 주문"           # 안 덮음
    assert meta["options"] == {"executor": "codex"}  # 풍부한 필드 보존


def test_load_view_attaches_meta_order(tmp_path):
    """load_view가 meta 사이드카(order)를 view에 동봉 → #57 주문 뷰가 CLI run 커버(직접 타겟)."""
    sp = tmp_path / "state.yaml"
    sp.write_text("spec_ref: x\nspec_version: 1\nstatus: running\n", encoding="utf-8")
    _write_cli_meta(sp, "CLI 원 주문")
    view = load_view(sp)
    assert view.get("meta", {}).get("order") == "CLI 원 주문"


def test_load_meta_missing_is_none(tmp_path):
    assert load_meta(tmp_path / "state.yaml") is None  # meta 없음 → None(graceful)
    assert load_meta(None) is None


def test_list_runs_surfaces_cli_order(tmp_path):
    """대시보드 /api/runs(list_runs)가 CLI run의 meta.json order를 표면화(스모크)."""
    runs = tmp_path / "runs"
    rid = "20260612-100000-cli-run"
    rdir = runs / rid
    rdir.mkdir(parents=True)
    sp = rdir / "state.yaml"
    sp.write_text("spec_ref: x\nspec_version: 1\nstatus: done\n", encoding="utf-8")
    _write_cli_meta(sp, "CLI 의뢰문", status="finished")
    rm = RunManager(runs_dir=runs)
    listed = {r["id"]: r for r in rm.list_runs()}
    assert rid in listed
    assert listed[rid]["order"] == "CLI 의뢰문"


# ──────────────────────────── HTML 스모크 ────────────────────────────


def test_dashboard_html_has_stale_and_meta_fallback():
    from haetae.dashboard import INDEX_HTML_PATH

    html = INDEX_HTML_PATH.read_text(encoding="utf-8")
    assert "v.stale" in html              # stale 표시
    assert "s-stale" in html              # stale 배지 클래스
    assert "RUN_META" in html             # CLI 원 주문 폴백
    assert "s-stopped_interrupted" in html  # 새 상태 배지 스타일
