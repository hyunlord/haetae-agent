"""WO#61 Phase F.2 — 인터넷 능력 발견(discovery-only) 테스트. mock searcher·실네트워크 없음.

원격 후보가 *기존 사람-게이트 escalation*에 흐른다: 실행 0(메타데이터 POC, ok=None)·자동 채택
없음(allowlist 불변)·source=remote 표기. 네트워크는 capability_search.py에만(capability.py는 net-free).
"""

import ast
import os
from pathlib import Path

import pytest

from haetae.capability import (
    CapabilityEntry,
    discover_remote,
    governed_capability_preflight,
)
from haetae import capability_search as cs
from haetae.capability_search import PypiSearcher, _candidate_names, make_searcher
from haetae.models import CapabilityPOC, CapabilityRequest

REPO_ROOT = Path(__file__).resolve().parents[1]
CAP_DIR = REPO_ROOT / "capabilities"
CAP_SRC = REPO_ROOT / "src" / "haetae" / "capability.py"
CAP_SEARCH_SRC = REPO_ROOT / "src" / "haetae" / "capability_search.py"


def _remote_searcher(items):
    """주어진 dict 리스트를 그대로 돌려주는 mock CapabilitySearcher."""
    def searcher(_req):
        return list(items)
    return searcher


_ONE_REMOTE = [{
    "identifier": "remotepkg", "registry": "pypi", "ecosystem": "pip",
    "license": "MIT", "imports": ["remotepkg"], "note": "found on pypi",
}]


# ──────────────────────────── discover_remote (정규화·best-effort) ────────────────────────────


def test_discover_remote_normalizes_with_remote_source():
    entries = discover_remote(CapabilityRequest(capability="x"), searcher=_remote_searcher(_ONE_REMOTE))
    assert len(entries) == 1
    e = entries[0]
    assert isinstance(e, CapabilityEntry)
    assert e.identifier == "remotepkg"
    assert e.source == "remote:pypi"        # 명확 표기(curated:와 구분)
    assert e.ecosystem == "pip" and e.license == "MIT"


def test_discover_remote_none_searcher_is_empty():
    """searcher 미주입 → 원격 후보 0(off-by-default, 기존 F.1 동작)."""
    assert discover_remote(CapabilityRequest(capability="x"), searcher=None) == []


def test_discover_remote_absorbs_searcher_exception():
    def boom(_req):
        raise RuntimeError("network down")
    assert discover_remote(CapabilityRequest(capability="x"), searcher=boom) == []


def test_discover_remote_skips_malformed_items():
    items = [{"no_identifier": 1}, "not a dict", {"identifier": "ok", "registry": "pypi"}]
    entries = discover_remote(CapabilityRequest(capability="x"), searcher=_remote_searcher(items))
    assert [e.identifier for e in entries] == ["ok"]


def test_discover_remote_bad_return_type_absorbed():
    assert discover_remote(CapabilityRequest(capability="x"), searcher=lambda r: "nope") == []


# ──────────────────────────── preflight 통합 (사람 게이트 불변) ────────────────────────────


def test_remote_candidate_surfaces_in_escalation_execution_zero():
    """원격 후보 → escalation에 source=remote·ok=None(실행 0)·trust=remote-unverified."""
    out = governed_capability_preflight(
        [CapabilityRequest(capability="quantumthing", unit="u1")],
        registry_dir=None, allowlist=[], approved_at="2026-06-10T00:00:00Z",
        searcher=_remote_searcher(_ONE_REMOTE),
    )
    assert out.provenance == []                 # 자동 채택 없음
    cands = out.escalation["requests"][0]["candidates"]
    assert any(c["source"] == "remote:pypi" for c in cands)
    assert all(c["poc"]["ok"] is None for c in cands)        # 실행 0(메타데이터)
    assert any(c["trust"] == "remote-unverified" and c["needs_scrutiny"] for c in cands)


def test_remote_not_in_allowlist_escalation_only_no_provenance():
    out = governed_capability_preflight(
        [CapabilityRequest(capability="x")],
        registry_dir=None, allowlist=["something-else"], approved_at="t",
        searcher=_remote_searcher(_ONE_REMOTE),
    )
    assert out.provenance == []                 # allowlist에 없음 → 미채택
    assert out.escalation is not None


def test_remote_in_allowlist_adopted_with_remote_source():
    """사람이 allowlist에 원격 identifier를 올리면 → 채택(provenance, source=remote, 실행 0)."""
    out = governed_capability_preflight(
        [CapabilityRequest(capability="x")],
        registry_dir=None, allowlist=["remotepkg"], approved_at="t",
        searcher=_remote_searcher(_ONE_REMOTE),
    )
    assert out.escalation is None
    assert len(out.provenance) == 1
    assert out.provenance[0].source == "remote:pypi"
    assert out.provenance[0].poc_ok is None     # 실행 0(메타데이터 POC)


def test_searcher_none_identical_to_f1_no_remote():
    """searcher=None(기본) → 원격 후보 0, 기존 F.1과 동일(no-op)."""
    out = governed_capability_preflight(
        [CapabilityRequest(capability="quantum teleporter")],
        registry_dir=None, allowlist=[], approved_at="t",
    )
    # 큐레이션 0 + 원격 0 → 후보 없는 escalation.
    assert out.provenance == []
    assert out.escalation["requests"][0]["candidates"] == []


def test_remote_candidate_never_runs_poc_runner():
    """**실행 0 핵심**: poc_runner가 주입돼도 *원격* 후보엔 적용 안 됨(ok=None). 큐레이션엔 적용."""
    def live_runner(entry):
        return CapabilityPOC(identifier=entry.identifier, ok=True, detail="live smoke ran")

    out = governed_capability_preflight(
        [CapabilityRequest(capability="pathfinding for AI", unit="u1")],
        registry_dir=CAP_DIR,           # 큐레이션 pathfinding 매칭됨
        allowlist=[], approved_at="t",
        poc_runner=live_runner,         # F.1b 라이브 runner
        searcher=_remote_searcher([{"identifier": "remote-astar", "registry": "pypi"}]),
    )
    cands = {c["source"]: c["poc"]["ok"] for c in out.escalation["requests"][0]["candidates"]}
    # 큐레이션 후보는 runner 적용(ok=True), 원격 후보는 runner 무시(ok=None — 실행 0).
    assert cands.get("remote:pypi") is None
    assert any(v is True for s, v in cands.items() if s.startswith("curated:"))


def test_escalation_distinguishes_curated_vs_remote():
    out = governed_capability_preflight(
        [CapabilityRequest(capability="pathfinding for AI", unit="u1")],
        registry_dir=CAP_DIR, allowlist=[], approved_at="t",
        searcher=_remote_searcher([{"identifier": "remote-astar", "registry": "pypi"}]),
    )
    req0 = out.escalation["requests"][0]
    assert req0["curated_count"] >= 1 and req0["remote_count"] == 1
    trusts = {c["trust"] for c in req0["candidates"]}
    assert "curated-verified" in trusts and "remote-unverified" in trusts
    # 큐레이션이 원격보다 앞(검토 우선순위).
    assert req0["candidates"][0]["needs_scrutiny"] is False
    assert "정밀검토" in out.escalation["reason"]


# ──────────────────────────── 네트워크 격리(정적) ────────────────────────────


def _imported_modules(src: Path) -> set[str]:
    tree = ast.parse(src.read_text(encoding="utf-8"))
    mods: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods.update(a.name.split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module:
            mods.add(n.module.split(".")[0])
    return mods


def test_capability_py_stays_network_free():
    """capability.py는 F.2 후에도 네트워크/subprocess import 없음(검색은 주입 searcher로만)."""
    banned = {"socket", "urllib", "requests", "httpx", "aiohttp", "http", "ftplib",
              "subprocess", "telnetlib", "urllib3"}
    assert not (_imported_modules(CAP_SRC) & banned)


def test_capability_search_py_is_the_network_module():
    """네트워크는 capability_search.py에만 격리(urllib 보유)."""
    mods = _imported_modules(CAP_SEARCH_SRC)
    assert "urllib" in mods


# ──────────────────────────── PypiSearcher (주입 opener, 실네트워크 없음) ────────────────────────────


def test_candidate_names_bounded_and_deterministic():
    names = _candidate_names("pathfinding for grid")
    assert "pathfinding-for-grid" in names
    assert "pathfinding" in names
    assert len(names) <= cs._MAX_CANDIDATES
    assert _candidate_names("") == []


def test_pypi_searcher_normalizes_with_injected_opener():
    def opener(name):
        if name == "pathfinding":
            return {"info": {"name": "pathfinding", "summary": "A* grid lib",
                             "license": "MIT", "classifiers": []}}
        return None
    res = PypiSearcher(opener=opener)(CapabilityRequest(capability="pathfinding"))
    assert any(r["identifier"] == "pathfinding" for r in res)
    r = next(r for r in res if r["identifier"] == "pathfinding")
    assert r["registry"] == "pypi" and r["ecosystem"] == "pip" and r["license"] == "MIT"
    assert r["note"] == "A* grid lib"


def test_pypi_searcher_license_from_classifiers():
    def opener(name):
        return {"info": {"name": name, "license": "",
                         "classifiers": ["License :: OSI Approved :: MIT License"]}}
    res = PypiSearcher(opener=opener)(CapabilityRequest(capability="thing"))
    assert res and res[0]["license"] == "MIT License"


def test_pypi_searcher_absorbs_opener_exception():
    def boom(name):
        raise RuntimeError("net down")
    assert PypiSearcher(opener=boom)(CapabilityRequest(capability="x")) == []


def test_pypi_searcher_missing_package_skipped():
    res = PypiSearcher(opener=lambda n: None)(CapabilityRequest(capability="nonexistent-pkg-xyz"))
    assert res == []


def test_make_searcher_pypi_and_unknown():
    assert isinstance(make_searcher("pypi"), PypiSearcher)
    with pytest.raises(ValueError):
        make_searcher("npm-not-yet")


# ──────────────────────────── NpmSearcher (의미 검색, 주입 opener) ────────────────────────────


def test_npm_searcher_semantic_search_via_opener():
    """npm은 능력 *텍스트*를 query로 의미 검색(이름 추측 아님) → 관련도순 후보 + 메타데이터."""
    from haetae.capability_search import NpmSearcher
    seen = {}

    def opener(query):
        seen["q"] = query
        return {"objects": [
            {"package": {"name": "easystarjs", "description": "A* on a grid",
                         "keywords": ["astar", "pathfinding"], "license": "MIT"},
             "score": {"final": 0.9}},
            {"package": {"name": "pathfinding", "description": "pathfinding lib",
                         "keywords": ["astar"], "license": {"type": "ISC"}},
             "score": {"final": 0.8}},
        ]}

    res = NpmSearcher(opener=opener)(CapabilityRequest(capability="pathfinding for AI"))
    assert seen["q"] == "pathfinding for AI"           # query=능력 텍스트(슬러그 추측 아님)
    assert [r["identifier"] for r in res] == ["easystarjs", "pathfinding"]  # 관련도순
    assert res[0]["registry"] == "npm" and res[0]["ecosystem"] == "npm"
    assert res[0]["relevance"] == 0.9
    assert "pathfinding" in res[0]["keywords"]
    assert res[0]["note"] == "A* on a grid"
    assert res[1]["license"] == "ISC"                  # dict license({type}) 흡수


def test_npm_searcher_empty_query_and_bad_responses():
    from haetae.capability_search import NpmSearcher
    assert NpmSearcher(opener=lambda q: {"objects": []})(CapabilityRequest(capability="")) == []  # 빈 query
    assert NpmSearcher(opener=lambda q: None)(CapabilityRequest(capability="x")) == []
    assert NpmSearcher(opener=lambda q: {"objects": []})(CapabilityRequest(capability="x")) == []
    assert NpmSearcher(opener=lambda q: {"no_objects": 1})(CapabilityRequest(capability="x")) == []
    assert NpmSearcher(opener=lambda q: {"objects": [1, "x"]})(CapabilityRequest(capability="x")) == []


def test_npm_searcher_absorbs_opener_exception():
    from haetae.capability_search import NpmSearcher

    def boom(q):
        raise RuntimeError("net down")
    assert NpmSearcher(opener=boom)(CapabilityRequest(capability="x")) == []


def test_npm_searcher_bounded_by_size():
    from haetae.capability_search import NpmSearcher
    objs = [{"package": {"name": f"pkg{i}"}, "score": {"final": 0.5}} for i in range(20)]
    res = NpmSearcher(size=3, opener=lambda q: {"objects": objs})(CapabilityRequest(capability="x"))
    assert len(res) == 3  # bounded


# ──────────────────────────── 멀티 레지스트리 (composite·dedup) ────────────────────────────


def test_composite_merges_and_dedups():
    from haetae.capability_search import _CompositeSearcher
    npm = _remote_searcher([{"identifier": "easystar", "registry": "npm", "note": "npm one"}])
    pypi = _remote_searcher([
        {"identifier": "pathfinding", "registry": "pypi"},
        {"identifier": "easystar", "registry": "npm"},  # 중복(npm) → dedup
    ])
    res = _CompositeSearcher([npm, pypi])(CapabilityRequest(capability="x"))
    keys = {(r["identifier"], r["registry"]) for r in res}
    assert ("easystar", "npm") in keys and ("pathfinding", "pypi") in keys
    assert sum(1 for r in res if r["identifier"] == "easystar") == 1  # dedup


def test_composite_absorbs_one_searcher_failure():
    from haetae.capability_search import _CompositeSearcher

    def boom(_r):
        raise RuntimeError("registry down")
    ok = _remote_searcher([{"identifier": "good", "registry": "npm"}])
    res = _CompositeSearcher([boom, ok])(CapabilityRequest(capability="x"))
    assert [r["identifier"] for r in res] == ["good"]  # 한 레지스트리 실패가 다른 것 안 막음


def test_make_searcher_multi_and_whitespace_and_errors():
    from haetae.capability_search import NpmSearcher, _CompositeSearcher
    assert isinstance(make_searcher("npm"), NpmSearcher)
    assert isinstance(make_searcher("npm,pypi"), _CompositeSearcher)
    assert isinstance(make_searcher(" npm , pypi "), _CompositeSearcher)  # 공백 흡수
    with pytest.raises(ValueError):
        make_searcher("npm,bogus")   # 미지 하나라도 있으면 거부
    with pytest.raises(ValueError):
        make_searcher("")            # 빈 리스트


# ──────────────────────────── 메타데이터 → escalation (사람 판단 신호) ────────────────────────────


def test_npm_metadata_flows_to_escalation():
    """npm description/keywords/relevance가 escalation 후보까지 흘러 사람에게 노출된다."""
    items = [{
        "identifier": "easystarjs", "registry": "npm", "ecosystem": "npm", "license": "MIT",
        "note": "A* pathfinding on a 2D grid", "keywords": ["astar", "pathfinding"],
        "relevance": 0.83,
    }]
    out = governed_capability_preflight(
        [CapabilityRequest(capability="pathfinding", unit="u1")],
        registry_dir=None, allowlist=[], approved_at="t",
        searcher=_remote_searcher(items),
    )
    c = out.escalation["requests"][0]["candidates"][0]
    assert c["source"] == "remote:npm"
    assert c["note"] == "A* pathfinding on a 2D grid"
    assert "pathfinding" in c["keywords"]
    assert c["relevance"] == 0.83
    assert c["poc"]["ok"] is None              # 실행 0(무회귀)
    assert c["trust"] == "remote-unverified"


# ──────────────────────────── (선택) 실 pypi 통합 — opt-in ────────────────────────────


@pytest.mark.skipif(
    os.environ.get("HAETAE_CAP_SEARCH_IT") != "1",
    reason="실 네트워크 능력 검색 통합 테스트는 opt-in (HAETAE_CAP_SEARCH_IT=1)",
)
def test_real_pypi_discovery_smoke():
    res = make_searcher("pypi")(CapabilityRequest(capability="requests"))
    assert any(r["identifier"].lower() == "requests" for r in res)
    assert all(r["registry"] == "pypi" for r in res)


@pytest.mark.skipif(
    os.environ.get("HAETAE_CAP_SEARCH_IT") != "1",
    reason="실 네트워크 능력 검색 통합 테스트는 opt-in (HAETAE_CAP_SEARCH_IT=1)",
)
def test_real_npm_semantic_discovery_smoke():
    # npm 진짜 의미 검색: 능력 텍스트로 관련 패키지(이름이 텍스트와 안 같아도) 발견.
    res = make_searcher("npm")(CapabilityRequest(capability="a star pathfinding grid"))
    assert res and all(r["registry"] == "npm" for r in res)
    assert all("identifier" in r for r in res)
