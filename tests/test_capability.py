"""WO#53 Phase F.1 — 능력 획득 거버넌스 테스트 (mock만, 네트워크 없음).

요청(gap) → 큐레이션 발견 → POC(증거) → 사람 승인(escalate) → 채택(+provenance).
안전 불변: **자동 채택 없음**·executor sandbox 불변·큐레이션 소스만·opt-in no-op.
"""

import ast
from pathlib import Path

from haetae import capability as cap
from haetae.capability import (
    CapabilityEntry,
    build_capability_escalation,
    build_provenance,
    discover,
    governed_capability_preflight,
    is_approved,
    load_capability_registry,
    run_poc,
    to_candidate,
)
from haetae.llm import MockClient
from haetae.loop import MockExecutor, MockGate, run_loop
from haetae.models import CapabilityPOC, CapabilityRequest, ProjectSpec, State, Status, Verdict

REPO_ROOT = Path(__file__).resolve().parents[1]
CAP_DIR = REPO_ROOT / "capabilities"
PROMPT_DIR = REPO_ROOT / "prompts"
CAP_SRC = REPO_ROOT / "src" / "haetae" / "capability.py"


def _entry(**kw) -> CapabilityEntry:
    base = dict(
        capability="pathfinding", keywords=("pathfinding", "길찾기"), identifier="pathfinding",
        ecosystem="npm", source="curated:npm/pathfinding", license="MIT",
        install=("npm", "install", "pathfinding"), imports=("pathfinding",), note="n",
    )
    base.update(kw)
    return CapabilityEntry(**base)


# ──────────────────── 레지스트리 로드(큐레이션, best-effort) ────────────────────


def test_registry_loads_seed_entries():
    reg = load_capability_registry(CAP_DIR)
    ids = {e.identifier for e in reg}
    assert "pathfinding" in ids and "date-fns" in ids
    pf = next(e for e in reg if e.identifier == "pathfinding")
    assert pf.license == "MIT" and pf.source.startswith("curated:")
    assert "pathfinding" in pf.keywords


def test_registry_missing_dir_is_empty_no_raise():
    assert load_capability_registry(CAP_DIR / "nope") == []
    assert load_capability_registry(None) == []  # None → 빈(능력 없음)


# ──────────────────── 발견(키워드 매칭) ────────────────────


def test_discover_matches_keyword():
    reg = load_capability_registry(CAP_DIR)
    req = CapabilityRequest(capability="need pathfinding for unit movement AI", unit="u1")
    found = discover(req, reg)
    assert [e.identifier for e in found] == ["pathfinding"]


def test_discover_no_match_returns_empty():
    reg = load_capability_registry(CAP_DIR)
    assert discover(CapabilityRequest(capability="quantum teleporter"), reg) == []
    assert discover(CapabilityRequest(capability=""), reg) == []


# ──────────────────── POC (기본=메타데이터·코드 미실행) ────────────────────


def test_poc_default_is_metadata_no_execution():
    poc = run_poc(_entry())
    assert poc.ok is None                       # 미실행(메타데이터만)
    assert poc.imports == ["pathfinding"]
    assert any("install" in n for n in poc.needs)
    assert "미실행" in (poc.detail or "")


def test_poc_injected_runner_used():
    def live(entry):
        return CapabilityPOC(identifier=entry.identifier, ok=True, imports=["pathfinding"],
                             detail="라이브 스모크 통과")
    poc = run_poc(_entry(), runner=live)
    assert poc.ok is True and poc.detail == "라이브 스모크 통과"


def test_poc_runner_exception_absorbed_no_raise():
    def boom(entry):
        raise RuntimeError("poc 폭발")
    poc = run_poc(_entry(), runner=boom)   # best-effort: 흡수
    assert poc.ok is False and "예외" in (poc.detail or "")


# ──────────────────── 승인 / provenance ────────────────────


def test_is_approved_only_when_in_allowlist():
    e = _entry()
    assert is_approved(e, []) is False         # 빈 allowlist → 미승인(자동 채택 없음)
    assert is_approved(e, None) is False
    assert is_approved(e, ["pathfinding"]) is True       # identifier 매칭
    assert is_approved(e, ["other", "pathfinding"]) is True
    assert is_approved(e, ["unrelated"]) is False


def test_build_provenance_records_what_where_who():
    poc = CapabilityPOC(identifier="pathfinding", ok=True)
    prov = build_provenance(_entry(), approved_by="human-allowlist",
                            approved_at="2026-06-09T00:00:00Z", poc=poc)
    assert prov.identifier == "pathfinding" and prov.source.startswith("curated:")
    assert prov.license == "MIT" and prov.approved_by == "human-allowlist"
    assert prov.approved_at == "2026-06-09T00:00:00Z" and prov.poc_ok is True


def test_build_escalation_carries_candidates_and_poc():
    req = CapabilityRequest(capability="pathfinding", unit="u1", reason="이동 AI")
    esc = build_capability_escalation([(req, [(_entry(), run_poc(_entry()))])])
    assert esc["capability_gate"] is True and "승인 대기" in esc["reason"]
    r0 = esc["requests"][0]
    assert r0["capability"] == "pathfinding" and r0["unit"] == "u1"
    cand = r0["candidates"][0]
    assert cand["candidate"]["identifier"] == "pathfinding"
    assert cand["license"] == "MIT" and "poc" in cand


# ──────────────────── preflight: 발견→POC→승인 분기 (핵심 거버넌스) ────────────────────


def test_preflight_unapproved_escalates_no_provenance():
    """미승인 능력 → escalation(후보+증거), provenance 0 = **자동 채택 없음**."""
    reqs = [CapabilityRequest(capability="pathfinding for AI", unit="u1", reason="이동")]
    out = governed_capability_preflight(
        reqs, registry_dir=CAP_DIR, allowlist=[], approved_at="2026-06-09T00:00:00Z")
    assert out.provenance == []                 # 승인 없으면 채택 0
    assert out.escalation is not None and out.escalation["capability_gate"] is True
    assert out.escalation["requests"][0]["candidates"][0]["candidate"]["identifier"] == "pathfinding"


def test_preflight_approved_records_provenance_no_escalation():
    """승인(allowlist)된 능력 → provenance 기록 + escalation 없음(진행)."""
    reqs = [CapabilityRequest(capability="pathfinding for AI", unit="u1")]
    out = governed_capability_preflight(
        reqs, registry_dir=CAP_DIR, allowlist=["pathfinding"], approved_at="2026-06-09T00:00:00Z")
    assert out.escalation is None
    assert [p.identifier for p in out.provenance] == ["pathfinding"]
    assert out.provenance[0].license == "MIT" and out.provenance[0].approved_by == "human-allowlist"


def test_preflight_no_candidate_escalates_for_review():
    """큐레이션에 후보 없음 → 그래도 escalate(사람 검토: '후보 없음'). 후보 리스트는 빈."""
    reqs = [CapabilityRequest(capability="quantum teleporter")]
    out = governed_capability_preflight(
        reqs, registry_dir=CAP_DIR, allowlist=[], approved_at="t")
    assert out.escalation is not None
    assert out.escalation["requests"][0]["candidates"] == []
    assert out.provenance == []


def test_preflight_no_requests_is_noop():
    out = governed_capability_preflight([], registry_dir=CAP_DIR, allowlist=[], approved_at="t")
    assert out.escalation is None and out.provenance == []


# ──────────────────── 안전 가드: 큐레이션 소스만(인터넷·subprocess 코드 부재) ────────────────────


def test_capability_module_has_no_network_or_subprocess_imports():
    """F.1: 인터넷 검색·임의 fetch·코드실행 코드 부재 — 큐레이션 소스만(데이터 처리)."""
    tree = ast.parse(CAP_SRC.read_text(encoding="utf-8"))
    mods: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods.update(a.name.split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module:
            mods.add(n.module.split(".")[0])
    banned = {"socket", "urllib", "requests", "httpx", "aiohttp", "http", "ftplib",
              "subprocess", "telnetlib", "urllib3"}
    assert not (mods & banned), f"F.1은 네트워크/subprocess 코드 금지: {mods & banned}"


# ──────────────────── 루프 통합: opt-in + escalate + provenance ────────────────────


SPEC_CAP = """\
spec_id: cap-001
version: 1
order_raw: "유닛 이동 AI가 있는 시뮬"
goal: "이동 AI 시뮬"
task_type: feature_impl
verifiability: objective
mode: normal
constraints: []
acceptance_criteria:
  - id: ac1
    desc: "이동"
    check: { type: test, cmd: "true" }
assumptions: []
non_goals: ["n1", "n2"]
done_when: "ac1 통과"
decomposition:
  - { unit: u1, desc: "이동 로직", deps: [] }
capability_requests:
  - { capability: "pathfinding for movement AI", unit: u1, reason: "경로탐색 라이브러리 필요" }
open_questions: []
"""


def _next_order(unit: str) -> str:
    return (
        "verdict: pass\naction: next_order\n"
        f'rationale: "{unit} 진행"\n'
        f"next_order:\n  unit: {unit}\n  goal: \"{unit}\"\n"
        '  local_checks: [{ type: test, cmd: "true" }]\n  executor: codex\n  deliverable: "요약"\n'
    )


def test_loop_capability_off_is_noop_backward_compatible():
    """플래그 OFF(기본): capability_requests가 있어도 *완전 no-op* — 게이트 안 탐, 정상 진행."""
    client = MockClient([SPEC_CAP, _next_order("u1")])
    state = run_loop(order="x", client=client, executor=MockExecutor("a"),
                     gate=MockGate(Verdict.done), prompt_dir=PROMPT_DIR)  # capabilities_on 기본 False
    assert state.status is Status.done                       # 정상 완주(기존 동작)
    assert state.capability_requests == []                   # OFF면 기록조차 안 함
    assert state.capability_provenance == []
    assert not any(isinstance(e, dict) and e.get("capability_gate") for e in state.pending_escalations)


def test_loop_capability_on_unapproved_escalates_before_dispatch():
    """플래그 ON + 미승인 → dispatch 전 escalate. executor 미호출·provenance 0(자동 채택 없음)."""
    client = MockClient([SPEC_CAP])  # 합성만 — 게이트가 dispatch 전에 멈춘다
    state = run_loop(order="x", client=client, executor=MockExecutor("a"),
                     gate=MockGate(Verdict.done), prompt_dir=PROMPT_DIR,
                     capabilities_on=True, capability_registry_path=CAP_DIR, capability_allowlist=[])
    assert state.status is Status.escalated
    assert state.events == []                                # dispatch 안 일어남(executor 미호출)
    assert state.capability_provenance == []                 # 승인 없음 → 채택 0
    assert [r.capability for r in state.capability_requests] == ["pathfinding for movement AI"]
    assert any(isinstance(e, dict) and e.get("capability_gate") for e in state.pending_escalations)
    assert len(client.calls) == 1                            # 합성 1회뿐(재계획 안 감)


def test_loop_capability_on_approved_records_provenance_and_proceeds():
    """플래그 ON + 승인(allowlist) → provenance 기록 후 정상 진행(escalate 안 함)."""
    client = MockClient([SPEC_CAP, _next_order("u1")])
    state = run_loop(order="x", client=client, executor=MockExecutor("a"),
                     gate=MockGate(Verdict.done), prompt_dir=PROMPT_DIR,
                     capabilities_on=True, capability_registry_path=CAP_DIR,
                     capability_allowlist=["pathfinding"])
    assert state.status is Status.done                       # 승인됐으니 진행
    assert [p.identifier for p in state.capability_provenance] == ["pathfinding"]
    assert state.capability_provenance[0].source.startswith("curated:")
    assert not any(isinstance(e, dict) and e.get("capability_gate") for e in state.pending_escalations)


def test_loop_capability_poc_runner_never_installs(monkeypatch=None):
    """POC는 기본 메타데이터(코드 미실행) — 승인 전 어떤 install/네트워크도 안 일어남."""
    # 승인 없이 escalate 경로에서도 POC는 메타데이터만(ok=None) — install 호출 0.
    out = governed_capability_preflight(
        [CapabilityRequest(capability="pathfinding")], registry_dir=CAP_DIR,
        allowlist=[], approved_at="t")
    poc = out.escalation["requests"][0]["candidates"][0]["poc"]
    assert poc["ok"] is None  # 미실행 — 승인 전 install/실행 없음


# ──────────────────── 안전: executor ALLOWED_SANDBOXES 불변 ────────────────────


def test_allowed_sandboxes_unchanged_by_capability():
    import haetae.providers.codex as codex_mod
    assert codex_mod.ALLOWED_SANDBOXES == ("read-only", "workspace-write")
    assert "danger-full-access" not in codex_mod.ALLOWED_SANDBOXES


# ──────────────────── 후방호환: capability_requests 없는 spec ────────────────────


def test_projectspec_without_capability_requests_defaults_empty():
    spec = ProjectSpec(
        spec_id="s", version=1, order_raw="o", goal="g", task_type="feature_impl",
        verifiability="objective", mode="normal",
        acceptance_criteria=[], non_goals=["a", "b"], done_when="d")
    assert spec.capability_requests == []  # 기존 spec 무영향(기본 빈 리스트)
    st = State(spec_ref="s", spec_version=1, status=Status.running)
    assert st.capability_requests == [] and st.capability_provenance == []
