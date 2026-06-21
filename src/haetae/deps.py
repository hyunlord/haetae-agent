"""호스트-사이드 의존성 설치 (WO#23) — 네트워크 차단 우회.

executor sandbox(workspace-write)는 네트워크를 막는다(안전 불변) → 빌드 중
`npm install`/`pip install`이 실패해 앱이 안 돌고, 그러면 gate 기계 체크·run-harness가
무력해진다. 해법: **executor sandbox는 그대로 offline 두고, haetae(호스트=네트워크 O)가
install을 대신**한다. 그래야 gate 체크와 run-harness가 설치된 deps로 동작한다.

원칙(loop/save/critic/run-harness resilience와 동일): **ensure_deps는 절대 raise하지 않는다.**
실패/타임아웃은 InstallResult(ok=False, reason)로 흡수 → 호출자는 그냥 진행(그럼 gate가
자연히 fail로 잡는다). 매니페스트 해시 캐시로 안 바뀌면 install을 스킵한다.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

# 해시 사이드카: 설치 성공 시 매니페스트 해시를 기록 → 다음 호출에서 불변이면 스킵.
HASH_SIDECAR = ".haetae-deps-hash"

# 호스트 install 산출물이 worktree 머지를 깨지 않도록 보장할 .gitignore 항목.
# (node_modules가 staged되면 #21 worktree 머지가 그걸 커밋·머지하려다 깨진다.)
GITIGNORE_ENTRIES = (
    "node_modules/",
    HASH_SIDECAR,
    "__pycache__/",
    "*.pyc",
    ".haetae-worktrees/",
    # WO#75: 로컬 oh-my-codex 툴링 산출물(.omx/logs·.omx/state)이 worktree에 흘러
    # `git add -A` 머지에 잡혀 충돌을 일으키던 노이즈 — 커밋 대상서 제외(머지 위생).
    ".omx/",
)

# install 실행 결과를 받는 runner 시그니처: (cmd, cwd, timeout) -> (returncode, output)
Runner = Callable[[list[str], str, float], "tuple[int, str]"]


@dataclass
class InstallResult:
    """ensure_deps 결과(감사/진행 판단용). State에 영속되지 않는 일회성 값."""

    manager: str  # "npm" | "pip" | "none"
    installed: bool = False
    skipped_cached: bool = False
    ok: bool = True
    reason: str | None = None
    duration_s: float = 0.0


# ──────────────────────────── 감지 ────────────────────────────

# (감지 매니페스트, manager, install cmd, 해시 대상 파일들). 우선순위 순.
# pnpm/yarn/uv 전용 경로는 후속 — 지금은 npm + pip만.
def _detect(wd: Path) -> tuple[str, list[str] | None, list[str]]:
    if (wd / "package.json").is_file():
        return ("npm", ["npm", "install"],
                ["package.json", "package-lock.json", "npm-shrinkwrap.json"])
    # WO#144 wart#1: 맨 `pip`(uv venv엔 pip 바이너리가 없어 #138 exit-127·#143 스퓨리어스
    # (install) 실패의 근본원인) 대신 *현재 인터프리터의* `python -m pip`을 쓴다(venv-일관).
    if (wd / "requirements.txt").is_file():
        return ("pip", [sys.executable, "-m", "pip", "install", "-r", "requirements.txt"],
                ["requirements.txt"])
    if (wd / "pyproject.toml").is_file():
        return ("pip", [sys.executable, "-m", "pip", "install", "-e", "."],
                ["pyproject.toml", "uv.lock", "poetry.lock"])
    return ("none", None, [])


def _manifest_hash(wd: Path, hash_files: list[str]) -> str:
    """매니페스트(+lock) 내용 해시. 존재하는 파일만, 경로+내용을 안정 순서로 섞는다."""
    h = hashlib.sha256()
    for name in hash_files:
        p = wd / name
        if p.is_file():
            try:
                h.update(name.encode("utf-8"))
                h.update(b"\0")
                h.update(p.read_bytes())
                h.update(b"\0")
            except OSError:
                pass
    return h.hexdigest()


def _ensure_gitignore(wd: Path) -> None:
    """install 산출물이 git에 안 잡히게 .gitignore에 필요한 항목을 보장(멱등).

    worktree에 .gitignore가 있으면(추적 여부 무관) git이 읽어 node_modules 등을 무시 →
    `git add -A`가 그것들을 staging하지 않아 worktree 머지가 깨끗하게 유지된다. non-fatal.
    """
    gi = wd / ".gitignore"
    try:
        existing = gi.read_text(encoding="utf-8") if gi.exists() else ""
        present = {ln.strip() for ln in existing.splitlines()}
        missing = [e for e in GITIGNORE_ENTRIES if e not in present]
        if not missing:
            return
        prefix = existing if (existing == "" or existing.endswith("\n")) else existing + "\n"
        gi.write_text(prefix + "\n".join(missing) + "\n", encoding="utf-8")
    except OSError:
        pass  # gitignore 쓰기 실패도 run을 죽이지 않는다


# ──────────────────────────── 기본 runner ────────────────────────────


def _default_runner(cmd: list[str], cwd: str, timeout: float) -> tuple[int, str]:
    """실제 subprocess install. 타임아웃/미설치/실행불가를 returncode+메시지로 흡수."""
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout
        )
        return proc.returncode, (proc.stderr or proc.stdout or "")
    except subprocess.TimeoutExpired:
        return 124, f"timeout (>{timeout}s)"
    except FileNotFoundError as e:  # npm/pip 자체가 호스트에 없음
        return 127, f"실행 파일 없음: {e}"
    except OSError as e:
        return 1, f"실행 실패: {e}"


def _tail(s: str, n: int = 400) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else "…" + s[-n:]


# ──────────────────────────── 공개 API ────────────────────────────


def ensure_deps(
    workdir: str | Path, *, timeout: int = 300, runner: Runner | None = None
) -> InstallResult:
    """workdir의 매니페스트를 감지해 호스트에서 deps를 설치한다. **절대 raise 안 함.**

    - 감지: package.json→npm / requirements.txt→pip / pyproject.toml→pip(-e .) / 없음→no-op.
    - 해시 캐시: 매니페스트(+lock) 불변이면 스킵(설치 안 함). 변경 시에만 install.
    - non-fatal: 실패/타임아웃 → InstallResult(ok=False, reason). 호출자는 진행.
    - .gitignore에 node_modules 등을 보장해 worktree 머지 정합을 지킨다.
    """
    runner = runner or _default_runner
    wd = Path(workdir)
    manager, cmd, hash_files = _detect(wd)
    if manager == "none" or cmd is None:
        return InstallResult(manager="none")

    # install 산출물이 머지를 깨지 않게 먼저 .gitignore 보장(설치/스킵 무관).
    _ensure_gitignore(wd)

    # 해시 캐시 — 매니페스트 불변이면 설치 스킵.
    digest = _manifest_hash(wd, hash_files)
    sidecar = wd / HASH_SIDECAR
    try:
        prev = sidecar.read_text(encoding="utf-8").strip() if sidecar.is_file() else None
    except OSError:
        prev = None
    if prev is not None and prev == digest:
        return InstallResult(manager=manager, skipped_cached=True, ok=True)

    # 설치(호스트, 네트워크 O). runner 호출까지 try로 감싸 never-raise 보장.
    start = time.monotonic()
    try:
        rc, out = runner(cmd, str(wd), float(timeout))
    except Exception as e:  # noqa: BLE001 — runner 버그도 흡수
        return InstallResult(
            manager=manager, installed=False, ok=False,
            reason=f"install 예외: {e}", duration_s=time.monotonic() - start,
        )
    dur = time.monotonic() - start

    if rc == 0:
        # 성공한 경우에만 해시 기록 → 실패하면 다음에 재시도된다.
        try:
            sidecar.write_text(digest, encoding="utf-8")
        except OSError:
            pass
        return InstallResult(manager=manager, installed=True, ok=True, duration_s=dur)

    return InstallResult(
        manager=manager, installed=False, ok=False,
        reason=f"install 실패 (exit {rc}): {_tail(out)}", duration_s=dur,
    )
