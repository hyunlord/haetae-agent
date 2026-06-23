"""run 계보(lineage) 테스트 (WO#167) — 파일 기반만(네트워크/엔진 없음).

비싼 런을 fix 후 이어가는 다-런 arc(런→fix→이어가기→fix…)를 추적: lineage.json에 parent_run_id +
fix_ref 기록(run.py), 대시보드가 parent 링크를 거슬러 트리 구성(read-only). verdict는 각 run의
state.status가 단일 출처(중복 저장 안 함). lineage = 기록 메타+표시지 *판정 아님*.
"""

import json
from pathlib import Path

import yaml

from haetae.dashboard import build_lineage, load_lineage, load_view
from haetae.models import Budget, Cost, Lineage, State, Status
from haetae.run import _resolve_fix_ref, _write_lineage

REPO_ROOT = Path(__file__).resolve().parents[1]


def _mk_run(runs_dir: Path, run_id: str, *, status="done", tokens=1000,
            parent: str | None = None, fix_ref: str | None = None) -> Path:
    """tmp runs/<id>/ 에 state.yaml(+옵션 lineage.json) 생성 → state_path 반환(테스트 헬퍼)."""
    d = runs_dir / run_id
    d.mkdir(parents=True)
    st = State(spec_ref="s", spec_version=1, status=Status(status),
               budget=Budget(spent=Cost(tokens=tokens)))
    (d / "state.yaml").write_text(yaml.safe_dump(st.model_dump(mode="json")), encoding="utf-8")
    if parent is not None or fix_ref is not None:
        (d / "lineage.json").write_text(
            json.dumps({"parent_run_id": parent, "fix_ref": fix_ref}), encoding="utf-8"
        )
    return d / "state.yaml"


# ──────────────────────────── Lineage 모델 ────────────────────────────


def test_lineage_model_defaults_and_fields():
    assert Lineage().parent_run_id is None and Lineage().fix_ref is None
    lin = Lineage(parent_run_id="p", fix_ref="abc123")
    d = lin.model_dump(mode="json")
    assert d == {"parent_run_id": "p", "fix_ref": "abc123"}
    # verdict는 lineage에 없다(state.status가 단일 출처 — 드리프트 방지)
    assert "verdict" not in Lineage.model_fields


# ──────────────────────────── 계보 기록 (run.py) ────────────────────────────


def test_write_lineage_records_parent_and_fix_ref(tmp_path):
    sp = tmp_path / "state.yaml"
    sp.write_text("x", encoding="utf-8")
    _write_lineage(sp, "20260610-090000-parent", fix_ref="deadbeef")
    rec = json.loads((tmp_path / "lineage.json").read_text(encoding="utf-8"))
    assert rec["parent_run_id"] == "20260610-090000-parent"
    assert rec["fix_ref"] == "deadbeef"


def test_write_lineage_preserves_parent_key_without_fix_ref(tmp_path):
    """fix_ref 없이 호출해도 parent_run_id 키 보존(구 read·#91 테스트 무영향)."""
    sp = tmp_path / "state.yaml"
    sp.write_text("x", encoding="utf-8")
    _write_lineage(sp, "p-only")
    rec = json.loads((tmp_path / "lineage.json").read_text(encoding="utf-8"))
    assert rec["parent_run_id"] == "p-only"
    assert rec.get("fix_ref") is None  # 추가형 — null


def test_resolve_fix_ref_prefers_nonblank_arg():
    assert _resolve_fix_ref("WO#167-commit") == "WO#167-commit"
    assert _resolve_fix_ref("  abc123  ") == "abc123"  # 비-blank 인자는 strip해서 우선


def test_resolve_fix_ref_blank_falls_back_to_head_best_effort():
    """blank 인자(None/""/공백)는 '미지정' → 현재 HEAD commit(짧은 해시) 또는 None(git 부재, best-effort).
    일관: arg OR HEAD — "" 와 "  " 와 None 이 모두 같게 동작."""
    for blank in (None, "", "  "):
        r = _resolve_fix_ref(blank)
        assert r is None or isinstance(r, str)  # repo면 해시 str, 아니면 None(둘 다 OK)


# ──────────────────────────── lineage 체인 (build_lineage, read-only) ────────────────────────────


def test_build_lineage_first_run_single_node_no_parent(tmp_path):
    """첫 런(lineage.json 없음) → 단일 노드·parent None."""
    runs = tmp_path / "runs"
    sp = _mk_run(runs, "20260623-000000-first")
    chain = build_lineage(sp)
    assert len(chain) == 1
    assert chain[0]["run_id"] == "20260623-000000-first"
    assert chain[0]["parent_run_id"] is None
    assert chain[0]["verdict"] == "done" and chain[0]["tokens"] == 1000


def test_build_lineage_walks_chain_A_B_C(tmp_path):
    """런A→fix→런B(continue)→fix→런C → C서 거슬러 A까지 트리 재구성(현재→조상 순)."""
    runs = tmp_path / "runs"
    _mk_run(runs, "run-a", status="escalated", tokens=2_000_000)
    _mk_run(runs, "run-b", status="escalated", tokens=1_500_000, parent="run-a", fix_ref="fixAB")
    sc = _mk_run(runs, "run-c", status="done", tokens=800_000, parent="run-b", fix_ref="fixBC")
    chain = build_lineage(sc)
    assert [n["run_id"] for n in chain] == ["run-c", "run-b", "run-a"]
    # 노드 = verdict/토큰(state.status·budget서 — 단일 출처)
    assert chain[0]["verdict"] == "done" and chain[0]["tokens"] == 800_000
    assert chain[1]["verdict"] == "escalated"
    assert chain[2]["verdict"] == "escalated" and chain[2]["parent_run_id"] is None
    # 엣지 = fix_ref(자식이 부모로부터 이어올 때 적용한 fix)
    assert chain[0]["fix_ref"] == "fixBC" and chain[1]["fix_ref"] == "fixAB"


def test_build_lineage_stops_at_missing_ancestor(tmp_path):
    """조상 dir 부재(삭제 등) → 체인 거기서 중단(무한루프/크래시 없음)."""
    runs = tmp_path / "runs"
    sb = _mk_run(runs, "run-b", parent="ghost-parent", fix_ref="x")  # 부모 dir 없음
    chain = build_lineage(sb)
    assert [n["run_id"] for n in chain] == ["run-b"]  # 부모 부재 → 자기만


def test_build_lineage_cycle_guard(tmp_path):
    """사이클(잘못된 parent로 자기참조)도 max_depth/visited 가드로 멈춤(서버 안전)."""
    runs = tmp_path / "runs"
    sp = _mk_run(runs, "run-x", parent="run-x", fix_ref="loop")  # 자기참조
    chain = build_lineage(sp)
    assert [n["run_id"] for n in chain] == ["run-x"]  # visited 가드 → 1회만


def test_build_lineage_is_read_only(tmp_path):
    """build_lineage는 *읽기만* — run dir 내용을 바꾸지 않는다(엔진/state 무접촉, #28/#35 위험 0)."""
    runs = tmp_path / "runs"
    _mk_run(runs, "run-a")
    sb = _mk_run(runs, "run-b", parent="run-a", fix_ref="f")
    before = {p.name: p.read_bytes() for p in (runs / "run-a").iterdir()}
    build_lineage(sb)
    after = {p.name: p.read_bytes() for p in (runs / "run-a").iterdir()}
    assert before == after  # 조상 run dir 무변경(read-only)


def test_load_lineage_reads_sidecar(tmp_path):
    sp = tmp_path / "state.yaml"
    sp.write_text("x", encoding="utf-8")
    (tmp_path / "lineage.json").write_text(json.dumps({"parent_run_id": "p", "fix_ref": "c"}), encoding="utf-8")
    assert load_lineage(sp) == {"parent_run_id": "p", "fix_ref": "c"}
    # 사이드카 없음 → None(best-effort)
    assert load_lineage(tmp_path / "nope" / "state.yaml") is None


# ──────────────────────────── load_view 동봉 ────────────────────────────


def test_load_view_attaches_lineage_when_chain(tmp_path):
    """부모가 있는 run → view['lineage']에 체인 동봉(트리 렌더 입력)."""
    runs = tmp_path / "runs"
    _mk_run(runs, "run-a")
    sb = _mk_run(runs, "run-b", parent="run-a", fix_ref="fixAB")
    view = load_view(sb)
    assert "lineage" in view
    assert [n["run_id"] for n in view["lineage"]] == ["run-b", "run-a"]


def test_load_view_no_lineage_for_first_run(tmp_path):
    """첫 런(부모 없음) → lineage 패널 없음(노이즈 0)."""
    runs = tmp_path / "runs"
    sp = _mk_run(runs, "run-solo")
    view = load_view(sp)
    assert "lineage" not in view  # 체인 길이 1 → 미동봉


# ──────────────────────────── 적대 분리·HTML(read-only) ────────────────────────────


def test_lineage_not_judgment_verdict_unchanged(tmp_path):
    """lineage는 표시지 판정 아님 — build_lineage가 state.status(verdict)를 *읽기만* 하고 안 바꾼다."""
    runs = tmp_path / "runs"
    sa = _mk_run(runs, "run-a", status="escalated")
    sb = _mk_run(runs, "run-b", parent="run-a", fix_ref="f")
    build_lineage(sb)
    # 부모 verdict 그대로(lineage가 verdict를 바꾸지 않음 — state.status 단일 출처)
    assert State.from_yaml(sa).status == Status.escalated


def test_dashboard_html_has_lineage_render():
    """대시보드 HTML에 lineage 트리 렌더(renderLineage·lineage-card·lineage-chain)가 있다."""
    html = (REPO_ROOT / "src" / "haetae" / "dashboard.html").read_text(encoding="utf-8")
    assert "renderLineage" in html
    assert 'id="lineage-card"' in html and 'id="lineage"' in html
    assert "lineage-chain" in html


def test_lineage_functions_no_engine_import():
    """적대 분리: 계보 함수가 사는 dashboard.py는 엔진(loop/gate/run_judge)을 *직접 import*하지
    않는다(#28/#35 read-only — 엔진은 subprocess 격리). lineage는 state 읽기 메타일 뿐."""
    src = (REPO_ROOT / "src" / "haetae" / "dashboard.py").read_text(encoding="utf-8")
    for forbidden in ("from haetae.loop import", "from haetae.gate import",
                      "import run_judge", "from haetae.judge import"):
        assert forbidden not in src, f"dashboard가 {forbidden}(엔진 직접 import — read-only 위반)"
