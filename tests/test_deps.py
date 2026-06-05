"""호스트-사이드 install 테스트 (WO#23) — 실제 네트워크 없이 runner 주입으로 검증.

감지 / 해시 캐시 / non-fatal / gate 진입 호출 순서 / node_modules gitignore(머지 정합) /
토글 / executor sandbox 불변.
"""

import subprocess
from pathlib import Path

from haetae.deps import HASH_SIDECAR, InstallResult, ensure_deps
from haetae.gate import CompositeGate
from haetae.models import ProjectSpec, Verdict


class RecRunner:
    """install runner 스파이 — 호출 기록 + 스크립트된 (rc, out) 반환. side로 부작용 주입."""

    def __init__(self, rc: int = 0, out: str = "", side=None):
        self.calls: list[tuple] = []
        self.rc = rc
        self.out = out
        self.side = side

    def __call__(self, cmd, cwd, timeout):
        self.calls.append((list(cmd), str(cwd), timeout))
        if self.side is not None:
            self.side(cmd, cwd, timeout)
        return (self.rc, self.out)


def _spec(acs: list[dict]) -> ProjectSpec:
    return ProjectSpec.model_validate(
        {
            "spec_id": "deps-001",
            "version": 1,
            "order_raw": "x",
            "goal": "g",
            "task_type": "feature_impl",
            "verifiability": "objective",
            "mode": "normal",
            "acceptance_criteria": acs,
            "non_goals": ["a", "b"],
            "done_when": "전부 통과",
        }
    )


# ──────────────────────────── 감지 ────────────────────────────


def test_detect_npm(tmp_path):
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    rec = RecRunner()
    res = ensure_deps(tmp_path, runner=rec)
    assert res.manager == "npm"
    assert res.installed is True
    assert rec.calls[0][0] == ["npm", "install"]


def test_detect_pip_requirements(tmp_path):
    (tmp_path / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    rec = RecRunner()
    res = ensure_deps(tmp_path, runner=rec)
    assert res.manager == "pip"
    assert rec.calls[0][0] == ["pip", "install", "-r", "requirements.txt"]


def test_detect_pip_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    rec = RecRunner()
    res = ensure_deps(tmp_path, runner=rec)
    assert res.manager == "pip"
    assert rec.calls[0][0] == ["pip", "install", "-e", "."]


def test_detect_none_is_noop(tmp_path):
    rec = RecRunner()
    res = ensure_deps(tmp_path, runner=rec)
    assert res.manager == "none"
    assert res.installed is False
    assert rec.calls == []  # 매니페스트 없으면 install 시도 0회


def test_npm_takes_priority_over_python(tmp_path):
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    res = ensure_deps(tmp_path, runner=RecRunner())
    assert res.manager == "npm"


# ──────────────────────────── 해시 캐시 ────────────────────────────


def test_hash_cache_skips_second_call(tmp_path):
    (tmp_path / "package.json").write_text('{"deps": 1}', encoding="utf-8")
    rec = RecRunner()
    first = ensure_deps(tmp_path, runner=rec)
    assert first.installed is True and len(rec.calls) == 1
    # 매니페스트 불변 → 두 번째는 runner 미호출(스킵)
    second = ensure_deps(tmp_path, runner=rec)
    assert second.skipped_cached is True
    assert second.installed is False
    assert len(rec.calls) == 1  # 재호출 없음


def test_hash_cache_reinstalls_on_manifest_change(tmp_path):
    mani = tmp_path / "package.json"
    mani.write_text('{"deps": 1}', encoding="utf-8")
    rec = RecRunner()
    ensure_deps(tmp_path, runner=rec)
    assert len(rec.calls) == 1
    mani.write_text('{"deps": 2}', encoding="utf-8")  # 변경
    res = ensure_deps(tmp_path, runner=rec)
    assert res.installed is True
    assert len(rec.calls) == 2  # 재설치


def test_hash_cache_includes_lockfile(tmp_path):
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / "package-lock.json").write_text('{"v": 1}', encoding="utf-8")
    rec = RecRunner()
    ensure_deps(tmp_path, runner=rec)
    # lock만 바뀌어도 재설치
    (tmp_path / "package-lock.json").write_text('{"v": 2}', encoding="utf-8")
    ensure_deps(tmp_path, runner=rec)
    assert len(rec.calls) == 2


# ──────────────────────────── non-fatal ────────────────────────────


def test_install_failure_is_non_fatal(tmp_path):
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    rec = RecRunner(rc=1, out="network blocked")
    res = ensure_deps(tmp_path, runner=rec)  # raise 없음
    assert res.ok is False
    assert res.installed is False
    assert "network blocked" in res.reason
    # 실패는 해시를 기록하지 않는다 → 다음에 재시도
    assert not (tmp_path / HASH_SIDECAR).exists()
    ensure_deps(tmp_path, runner=rec)
    assert len(rec.calls) == 2


def test_runner_exception_is_absorbed(tmp_path):
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")

    def boom(cmd, cwd, timeout):
        raise RuntimeError("runner 폭발")

    res = ensure_deps(tmp_path, runner=boom)  # raise 없음
    assert res.ok is False
    assert "예외" in res.reason


def test_returns_install_result_type(tmp_path):
    assert isinstance(ensure_deps(tmp_path, runner=RecRunner()), InstallResult)


# ──────────────────────────── gate 진입 호출 ────────────────────────────


def test_gate_installs_before_checks(tmp_path):
    """gate가 체크 실행 *전에* ensure_deps를 부른다 — 마커 파일로 순서 증명."""
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    rec = RecRunner(side=lambda cmd, cwd, t: (Path(cwd) / ".installed").write_text("x"))
    # 체크는 마커가 있어야 통과 → install이 먼저 돌았다는 증거
    spec = _spec([{"id": "ac1", "desc": "d", "check": {"type": "test", "cmd": "test -f .installed"}}])
    gate = CompositeGate(workdir=tmp_path, judge_client=None, deps_runner=rec, install_deps=True)
    gr = gate.judge("결과", spec)
    assert rec.calls  # install 시도됨
    assert gr.checks[0].status == "pass"  # 마커 존재 → 체크 전에 install 실행됨
    assert gr.verdict is Verdict.pass_


def test_gate_proceeds_when_install_fails(tmp_path):
    """install 실패해도 gate는 진행(non-fatal) — 체크가 자연히 평가된다."""
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    rec = RecRunner(rc=1, out="blocked")
    spec = _spec([{"id": "ac1", "desc": "d", "check": {"type": "test", "cmd": "true"}}])
    gate = CompositeGate(workdir=tmp_path, judge_client=None, deps_runner=rec)
    gr = gate.judge("결과", spec)  # raise 없음
    assert rec.calls  # 시도는 함
    assert gr.verdict is Verdict.pass_  # 체크는 그대로 평가


def test_gate_no_install_when_disabled(tmp_path):
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    rec = RecRunner()
    spec = _spec([{"id": "ac1", "desc": "d", "check": {"type": "test", "cmd": "true"}}])
    gate = CompositeGate(workdir=tmp_path, judge_client=None, deps_runner=rec, install_deps=False)
    gate.judge("결과", spec)
    assert rec.calls == []  # 토글 off → ensure_deps no-op


def test_gate_no_manifest_no_install(tmp_path):
    rec = RecRunner()
    spec = _spec([{"id": "ac1", "desc": "d", "check": {"type": "test", "cmd": "true"}}])
    gate = CompositeGate(workdir=tmp_path, judge_client=None, deps_runner=rec)
    gate.judge("결과", spec)
    assert rec.calls == []  # 매니페스트 없음 → install 안 함(비용 불변)


# ──────────────── node_modules gitignore (worktree 머지 정합) ────────────────


def test_install_artifacts_not_staged_in_git_workdir(tmp_path):
    """git workdir에서 install 후 node_modules가 staging되지 않는다(머지 정합 보장)."""
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, text=True)
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")

    def make_node_modules(cmd, cwd, t):
        nm = Path(cwd) / "node_modules" / "pkg"
        nm.mkdir(parents=True, exist_ok=True)
        (nm / "index.js").write_text("module.exports={}", encoding="utf-8")

    ensure_deps(tmp_path, runner=RecRunner(side=make_node_modules))

    gi = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert "node_modules/" in gi
    assert HASH_SIDECAR in gi

    # git add -A 후 staged 목록에 node_modules가 없어야 한다(merge가 깨끗하게 유지됨)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True, text=True)
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=tmp_path, capture_output=True, text=True,
    ).stdout
    assert "node_modules" not in staged
    assert HASH_SIDECAR not in staged
    # ignored라 status에도 안 뜬다
    porcelain = subprocess.run(
        ["git", "status", "--porcelain"], cwd=tmp_path, capture_output=True, text=True
    ).stdout
    assert "node_modules" not in porcelain


def test_gitignore_preserves_existing_entries(tmp_path):
    (tmp_path / ".gitignore").write_text(".haetae-worktrees/\n", encoding="utf-8")
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    ensure_deps(tmp_path, runner=RecRunner())
    gi = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert ".haetae-worktrees/" in gi  # 기존 항목 보존
    assert "node_modules/" in gi  # 신규 추가


# ──────────────── executor sandbox 불변 (안전 가드) ────────────────


def test_executor_sandbox_allowlist_unchanged():
    """WO#23은 executor sandbox를 절대 건드리지 않는다 — offline·danger 금지 불변."""
    from haetae.providers.codex import ALLOWED_SANDBOXES

    assert ALLOWED_SANDBOXES == ("read-only", "workspace-write")
    assert not any("danger" in s for s in ALLOWED_SANDBOXES)
