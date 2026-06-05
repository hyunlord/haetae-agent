"""선제 스캐폴드 테스트 (WO#27) — 실제 네트워크/codex 없이 mock client/runner로 검증.

생성(dep 스택 판단) / best-effort 흡수 / 파일 쓰기 경로안전 / main 커밋(symlink-safe
gitignore) / worktree node_modules 준비 / 루프 배선 순서(executor 전) / scaffold=None no-op
후방호환 / executor sandbox 불변(안전 가드).
"""

import subprocess
import threading
from pathlib import Path

from haetae.llm import MockClient
from haetae.loop import MockExecutor, MockGate, run_loop
from haetae.models import ProjectSpec, Status, Verdict
from haetae.scaffold import (
    Scaffold,
    commit_scaffold,
    generate_scaffold,
    prepare_worktree_deps,
    write_scaffold,
)
from haetae.worktree import ROOT_NAME

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPT_DIR = REPO_ROOT / "prompts"


def _spec(decomp: str = "  - { unit: u1, desc: a, deps: [] }") -> ProjectSpec:
    return ProjectSpec.model_validate(
        {
            "spec_id": "scaf-001",
            "version": 1,
            "order_raw": "React + TS + Vite로 대시보드",
            "goal": "React 대시보드",
            "task_type": "feature_impl",
            "verifiability": "objective",
            "mode": "normal",
            "acceptance_criteria": [
                {"id": "ac1", "desc": "d", "check": {"type": "build", "cmd": "npm run build"}}
            ],
            "non_goals": ["a", "b"],
            "done_when": "ac1",
            "decomposition": [{"unit": "u1", "desc": "a", "deps": []}],
        }
    )


# dep 스택 필요 → package.json + entry stub + install:true
SCAFFOLD_YAML = """\
files:
  package.json: |
    {"name":"app","private":true,"scripts":{"build":"echo built","test":"echo tested"},"dependencies":{"react":"^18.0.0"}}
  src/main.tsx: |
    // executor가 채운다 — 골격만
install: true
"""


class _RaisingClient:
    """complete()에서 예외를 던지는 mock — codex 다운/인증/잘못된 모델 시뮬."""

    def __init__(self, exc: Exception):
        self._exc = exc
        self.calls = 0

    def complete(self, system: str, user: str, **opts) -> str:
        self.calls += 1
        raise self._exc


class SpyRunner:
    """ensure_deps runner 스파이 — 호출 기록 + node_modules 마커 생성(설치 성공 시뮬)."""

    def __init__(self, marker: str = ".installed"):
        self.calls: list[tuple] = []
        self.marker = marker
        self._lock = threading.Lock()

    def __call__(self, cmd, cwd, timeout):
        with self._lock:
            self.calls.append((list(cmd), str(cwd), timeout))
        nm = Path(cwd) / "node_modules"
        nm.mkdir(parents=True, exist_ok=True)
        (nm / self.marker).write_text("x", encoding="utf-8")
        return (0, "")


# ──────────────────────────── 생성 (LLM 판단) ────────────────────────────


def test_generate_scaffold_dep_stack_returns_scaffold():
    sc = generate_scaffold(_spec(), MockClient([SCAFFOLD_YAML]), prompt_path=PROMPT_DIR / "scaffold.md")
    assert isinstance(sc, Scaffold)
    assert "package.json" in sc.files
    assert "react" in sc.files["package.json"]
    assert sc.install is True


def test_generate_scaffold_empty_files_is_none():
    """스택 불필요 → 빈 files → None(스킵)."""
    assert generate_scaffold(_spec(), MockClient(["files: {}\ninstall: false\n"]),
                             prompt_path=PROMPT_DIR / "scaffold.md") is None


def test_generate_scaffold_null_output_is_none():
    assert generate_scaffold(_spec(), MockClient(["null"]),
                             prompt_path=PROMPT_DIR / "scaffold.md") is None


def test_generate_scaffold_non_mapping_is_none():
    """매핑이 아닌 출력(그냥 문장) → None(흡수, raise 안 함)."""
    assert generate_scaffold(_spec(), MockClient(["그냥 설명 문장입니다"]),
                             prompt_path=PROMPT_DIR / "scaffold.md") is None


def test_generate_scaffold_broken_yaml_is_none():
    assert generate_scaffold(_spec(), MockClient(["files: {unterminated"]),
                             prompt_path=PROMPT_DIR / "scaffold.md") is None


def test_generate_scaffold_client_exception_is_absorbed():
    """client.complete가 던져도 None(best-effort) — run을 죽이지 않는다."""
    from haetae.llm import CodexError

    rc = _RaisingClient(CodexError("모델 없음"))
    assert generate_scaffold(_spec(), rc, prompt_path=PROMPT_DIR / "scaffold.md") is None
    assert rc.calls == 1  # 호출은 일어남(그리고 실패 흡수)


def test_generate_scaffold_strips_code_fence():
    fenced = "```yaml\n" + SCAFFOLD_YAML + "```"
    sc = generate_scaffold(_spec(), MockClient([fenced]), prompt_path=PROMPT_DIR / "scaffold.md")
    assert isinstance(sc, Scaffold) and "package.json" in sc.files


# ──────────────────────────── 파일 쓰기 (경로 안전) ────────────────────────────


def test_write_scaffold_writes_nested_files(tmp_path):
    sc = Scaffold(files={"package.json": "{}", "src/main.tsx": "// stub"}, install=True)
    written = write_scaffold(sc, tmp_path)
    assert set(written) == {"package.json", "src/main.tsx"}
    assert (tmp_path / "package.json").read_text() == "{}"
    assert (tmp_path / "src" / "main.tsx").read_text() == "// stub"


def test_write_scaffold_skips_path_escape(tmp_path):
    """workdir 밖으로 나가는 항목(절대경로/.. 탈출)은 건너뛴다."""
    sc = Scaffold(files={"../evil.txt": "x", "/etc/evil": "y", "ok.txt": "z"}, install=False)
    written = write_scaffold(sc, tmp_path)
    assert written == ["ok.txt"]
    assert not (tmp_path.parent / "evil.txt").exists()


# ──────────────────────────── main 커밋 (symlink-safe gitignore) ────────────────────────────


def test_commit_scaffold_adds_bare_node_modules_and_commits(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path)
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("node_modules/\n", encoding="utf-8")  # deps.py 식(디렉토리 한정)
    ok = commit_scaffold(tmp_path)
    assert ok is True
    gi = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    # bare node_modules가 추가됨(symlink 머지 누수 방지)
    assert "node_modules" in {ln.strip() for ln in gi.splitlines()}
    tracked = subprocess.run(["git", "ls-files"], cwd=tmp_path, capture_output=True, text=True).stdout
    assert "package.json" in tracked


def test_commit_scaffold_non_fatal_on_git_failure(tmp_path):
    """git 실패해도 raise 안 하고 False 반환(파일은 이미 디스크에)."""
    def boom_git(args, cwd):
        raise RuntimeError("git 폭발")

    assert commit_scaffold(tmp_path, git=boom_git) is False


# ──────────────────────────── worktree node_modules 준비 ────────────────────────────


def test_prepare_worktree_deps_symlinks_node_modules(tmp_path):
    main = tmp_path / "main"
    wt = tmp_path / "wt"
    (main / "node_modules" / "pkg").mkdir(parents=True)
    (main / "node_modules" / "pkg" / "i.js").write_text("x")
    wt.mkdir()
    how = prepare_worktree_deps(main, wt)
    assert how == "symlink"
    assert (wt / "node_modules").is_symlink()
    assert (wt / "node_modules" / "pkg" / "i.js").read_text() == "x"


def test_prepare_worktree_deps_install_fallback_when_no_main_nm(tmp_path):
    """main에 node_modules가 없으면 worktree에서 직접 host-install로 폴백."""
    main = tmp_path / "main"
    wt = tmp_path / "wt"
    main.mkdir()
    wt.mkdir()
    (wt / "package.json").write_text("{}", encoding="utf-8")
    rec = SpyRunner()
    from haetae.deps import ensure_deps

    how = prepare_worktree_deps(main, wt, ensure_deps_fn=lambda p: ensure_deps(p, runner=rec))
    assert how == "install"
    assert rec.calls  # worktree에서 install 시도
    assert (wt / "node_modules" / ".installed").exists()


def test_prepare_worktree_deps_none_when_nothing(tmp_path):
    main = tmp_path / "main"
    wt = tmp_path / "wt"
    main.mkdir()
    wt.mkdir()
    assert prepare_worktree_deps(main, wt) == "none"


# ──────────────────── 루프 배선: 순차(N=1) — executor 전 scaffold+install ────────────────────

SPEC_YAML = """\
spec_id: scaf-loop-001
version: 1
order_raw: "React 대시보드"
goal: "g"
task_type: feature_impl
verifiability: objective
mode: normal
constraints: []
acceptance_criteria:
  - id: ac1
    desc: "d"
    check: { type: build, cmd: "npm run build" }
assumptions: []
non_goals: ["a", "b"]
done_when: "ac1"
decomposition:
  - { unit: u1, desc: a, deps: [] }
open_questions: []
"""

_NEXT_ORDER = """\
verdict: pass
action: next_order
rationale: "u1"
next_order:
  unit: u1
  goal: "u1 구현"
  deliverable: "요약"
"""


class _SeqCheckExec:
    """run() 시점에 scaffold 파일 + install 마커 존재를 기록(순서 증명)."""

    def __init__(self, workdir):
        self.workdir = Path(workdir)
        self.saw: dict = {}

    def run(self, order):
        self.saw = {
            "package.json": (self.workdir / "package.json").exists(),
            "installed": (self.workdir / "node_modules" / ".installed").exists(),
        }
        return "u1 done"


def test_sequential_scaffold_applied_before_executor(tmp_path):
    """순차: scaffold 파일 쓰기 + host-install이 executor.run *전에* 끝나 있다(마커로 증명)."""
    ex = _SeqCheckExec(tmp_path)
    rec = SpyRunner()
    state = run_loop(
        "x",
        MockClient([SPEC_YAML, _NEXT_ORDER]),
        executor=ex,
        gate=MockGate(Verdict.done),
        scaffold_client=MockClient([SCAFFOLD_YAML]),
        deps_runner=rec,
        workdir=tmp_path,
        prompt_dir=PROMPT_DIR,
        max_parallel=1,
    )
    assert state.status is Status.done
    # executor가 도는 시점에 scaffold 파일 + 설치 마커가 *이미* 있었다 → 순서 증명
    assert ex.saw == {"package.json": True, "installed": True}
    assert rec.calls and rec.calls[0][0] == ["npm", "install"]
    # 순차는 git을 건드리지 않는다(현행 경로 보존)
    assert not (tmp_path / ".git").exists()


def test_sequential_scaffold_none_is_noop(tmp_path):
    """scaffold_client가 None 내는 spec(스택 불필요) → 신규 경로 전부 no-op."""
    ex = _SeqCheckExec(tmp_path)
    rec = SpyRunner()
    state = run_loop(
        "x",
        MockClient([SPEC_YAML, _NEXT_ORDER]),
        executor=ex,
        gate=MockGate(Verdict.done),
        scaffold_client=MockClient(["null"]),  # 스택 불필요 → None
        deps_runner=rec,
        workdir=tmp_path,
        prompt_dir=PROMPT_DIR,
        max_parallel=1,
    )
    assert state.status is Status.done
    assert ex.saw == {"package.json": False, "installed": False}  # 아무것도 안 깔림
    assert rec.calls == []  # install 시도 0


def test_sequential_no_scaffold_client_is_backcompat(tmp_path):
    """scaffold_client 미주입 → 기존 동작 그대로(scaffold 경로 자체가 죽어 있음)."""
    ex = _SeqCheckExec(tmp_path)
    rec = SpyRunner()
    state = run_loop(
        "x",
        MockClient([SPEC_YAML, _NEXT_ORDER]),
        executor=ex,
        gate=MockGate(Verdict.done),
        deps_runner=rec,
        workdir=tmp_path,
        prompt_dir=PROMPT_DIR,
        max_parallel=1,
    )
    assert state.status is Status.done
    assert ex.saw == {"package.json": False, "installed": False}
    assert rec.calls == []


# ──────────────────── 루프 배선: 병렬 — worktree 상속 + node_modules 준비 ────────────────────


class BrainClient:
    """call#1=synthesize(spec) / 이후=replan(DEC). main 스레드 직렬 호출(결정적)."""

    def __init__(self, spec_yaml: str, dec_yaml: str):
        self.spec = spec_yaml
        self.dec = dec_yaml
        self.n = 0

    def complete(self, system: str, user: str, **opts) -> str:
        self.n += 1
        return self.spec if self.n == 1 else self.dec


class PassGate:
    def judge(self, result, spec, unit=None):
        from haetae.models import GateResult

        return GateResult(verdict=Verdict.pass_)


def test_parallel_scaffold_inherited_and_node_modules_prepared(tmp_path):
    """병렬: worktree가 scaffold package.json을 git 상속 + node_modules가 dispatch 전 준비됨."""
    seen: dict = {}
    lock = threading.Lock()

    def make_ex(wt):
        class E:
            def run(self, order):
                with lock:
                    seen[order.unit] = {
                        "pkg": (Path(wt) / "package.json").exists(),  # git 상속
                        "nm": (Path(wt) / "node_modules").exists(),  # 준비됨(symlink)
                    }
                return f"{order.unit} done"

        return E()

    rec = SpyRunner(marker="pkg-marker")
    state = run_loop(
        "x",
        BrainClient(SPEC_YAML, _NEXT_ORDER),
        executor=None,
        gate=PassGate(),
        executor_factory=make_ex,
        gate_factory=lambda wt: PassGate(),
        scaffold_client=MockClient([SCAFFOLD_YAML]),
        deps_runner=rec,
        max_parallel=2,
        workdir=tmp_path,
        prompt_dir=PROMPT_DIR,
    )
    assert state.status is Status.done
    assert seen["u1"] == {"pkg": True, "nm": True}  # 둘 다 executor 전에 준비됨
    # main install이 1회 일어났다(host에서)
    assert any(c[0] == ["npm", "install"] for c in rec.calls)
    # 머지 정합: node_modules는 추적되지 않고 package.json은 커밋됨
    tracked = subprocess.run(["git", "ls-files"], cwd=tmp_path, capture_output=True, text=True).stdout
    assert "package.json" in tracked
    assert "node_modules" not in tracked
    # 뒷정리: worktree/브랜치/루트 0
    assert not (tmp_path / ROOT_NAME).exists()


def test_parallel_scaffold_none_is_noop(tmp_path):
    """병렬에서 scaffold=None(스택 불필요)이면 신규 경로 전부 no-op → 커밋/설치 0."""
    rec = SpyRunner()
    state = run_loop(
        "x",
        BrainClient(SPEC_YAML, _NEXT_ORDER),
        executor=None,
        gate=PassGate(),
        executor_factory=lambda wt: MockExecutor("done"),
        gate_factory=lambda wt: PassGate(),
        scaffold_client=MockClient(["null"]),  # 스택 불필요
        deps_runner=rec,
        max_parallel=2,
        workdir=tmp_path,
        prompt_dir=PROMPT_DIR,
    )
    assert state.status is Status.done
    assert rec.calls == []  # install 0
    # scaffold 미적용 → package.json 추적 없음(초기 빈 커밋만)
    tracked = subprocess.run(["git", "ls-files"], cwd=tmp_path, capture_output=True, text=True).stdout
    assert "package.json" not in tracked


# ──────────────────── 안전 가드: executor sandbox 불변 ────────────────────


def test_executor_sandbox_allowlist_unchanged():
    """WO#27은 executor sandbox를 절대 건드리지 않는다 — offline·danger 금지 불변."""
    from haetae.providers.codex import ALLOWED_SANDBOXES

    assert ALLOWED_SANDBOXES == ("read-only", "workspace-write")
    assert not any("danger" in s for s in ALLOWED_SANDBOXES)
