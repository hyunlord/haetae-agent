"""선제 스캐폴드 (WO#27) — offline executor가 *진짜* 스택을 보게 만든다.

배경(캡스톤이 드러낸 뿌리): executor sandbox는 offline이라 React/Vite/TS 같은 dep 스택을
못 깔고 → 그 스택을 *통째로 회피*해 plain Node `.mjs`로 빌드한다(스택 치환). #23 호스트-설치는
*선언된* deps만 까는데 executor가 React를 *선언조차 안 하니* 깔 게 없다.

근본 수정: executor 시작 *전에*, 네트워크 있는 director(host)가 진짜 스택 스캐폴드
(package.json + 최소 config/entry stub)를 깔고 deps를 설치 → executor가 React를 *실재하는
것으로* 본다. 본체 구현은 executor 몫, 골격만 host가 깐다.

안전 불변: executor sandbox는 계속 offline(`providers/codex.py` 불변). 스캐폴드 생성·설치는
전부 host(director)에서. executor는 *이미 채워진* workspace(package.json + node_modules +
config)를 받을 뿐 — 네트워크를 *주지 않는다*.

best-effort: LLM 호출/파싱/검증 실패는 전부 `None`으로 흡수한다(스캐폴드 없이 진행, raise 금지).
`generate_scaffold`가 `None`이면 호출 루프의 모든 신규 경로가 no-op → 기존 동작 불변.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable

import yaml
from pydantic import BaseModel, ValidationError

from haetae.llm import LLMClient
from haetae.models import ProjectSpec
from haetae.parsing import strip_code_fence

DEFAULT_SCAFFOLD_PROMPT_PATH = "prompts/scaffold.md"

# 커밋 신원 — worktree.py와 동일. user.* 미설정 환경(CI/테스트)에서도 커밋되게 박는다.
_GIT_IDENT = ("-c", "user.email=haetae@local", "-c", "user.name=haetae")

# symlink된 node_modules가 머지에 안 잡히게 보장할 *bare* 항목.
# deps.py가 쓰는 `node_modules/`(디렉토리 한정)는 symlink를 무시하지 못한다(검증됨) →
# main에 bare `node_modules`를 커밋해 worktree들이 상속하게 한다(머지 정합).
_BARE_NODE_MODULES = "node_modules"

# git 실행 결과를 받는 runner 시그니처: (args, cwd) -> (returncode, output). 테스트 주입용.
GitRunner = Callable[[list[str], str], "tuple[int, str]"]


class Scaffold(BaseModel):
    """director가 spec으로부터 생성한 *최소 실행 가능 골격*.

    files: 경로(workdir 상대) → 내용. package.json(실제 deps) + 최소 config/entry stub.
           본체 로직은 비워두고 executor가 채운다.
    install: 호스트에서 deps install(ensure_deps)을 돌릴지. dep-bearing 스택이면 True.
    """

    files: dict[str, str]
    install: bool = True


# ──────────────────────────── 생성 (LLM) ────────────────────────────


def _dump_spec(spec: ProjectSpec) -> str:
    return yaml.safe_dump(
        spec.model_dump(by_alias=True, mode="json"),
        allow_unicode=True,
        sort_keys=False,
    )


def generate_scaffold(
    spec: ProjectSpec,
    client: LLMClient,
    prompt_path: str | Path = DEFAULT_SCAFFOLD_PROMPT_PATH,
) -> Scaffold | None:
    """spec을 보고 dep 스택이 필요하면 최소 골격 `Scaffold`를, 아니면 `None`을 반환한다.

    완전 best-effort: LLM 호출 실패·YAML 파싱 실패·스키마 검증 실패·빈 files를 전부
    `None`으로 흡수한다(스캐폴드 없이 진행 — raise 금지). `None`이면 호출 루프의 모든
    신규 경로가 no-op이 되어 기존 동작이 불변으로 보존된다.

    LLM이 스택 불필요라고 판단하면 빈/null/none을 내도록 프롬프트가 유도한다 → `None`.
    """
    try:
        system = Path(prompt_path).read_text(encoding="utf-8")
        user = f"# 합성된 spec\n```yaml\n{_dump_spec(spec)}```"
        raw = client.complete(system, user)
    except Exception:  # noqa: BLE001 — best-effort: 어떤 클라이언트/IO 실패도 run을 죽이면 안 됨
        return None

    try:
        data = yaml.safe_load(strip_code_fence(raw))
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None  # null/none/스칼라 → 스택 불필요 = 스킵

    files = data.get("files")
    if not isinstance(files, dict) or not files:
        return None  # 빈/없음 → 스킵 (no-op)

    # 값 정규화(None 항목 제거 + str 강제) 후 검증. 깨지면 None으로 흡수.
    norm = {str(k): str(v) for k, v in files.items() if v is not None}
    if not norm:
        return None
    try:
        return Scaffold.model_validate(
            {"files": norm, "install": bool(data.get("install", True))}
        )
    except ValidationError:
        return None


# ──────────────────────────── 파일 쓰기 ────────────────────────────


def write_scaffold(scaffold: Scaffold, workdir: str | Path) -> list[str]:
    """scaffold 파일들을 workdir에 쓴다(부모 디렉토리 생성). 쓴 상대경로 목록 반환.

    경로 안전: workdir 밖으로 나가는 항목(절대경로·`..` 탈출)은 건너뛴다(사고 방지).
    """
    wd = Path(workdir).resolve()
    written: list[str] = []
    for rel, content in scaffold.files.items():
        target = (wd / rel).resolve()
        try:
            target.relative_to(wd)
        except ValueError:
            continue  # workdir 탈출 시도 → 무시
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        written.append(rel)
    return written


# ──────────────────────────── main 커밋 (병렬 상속) ────────────────────────────


def _default_git(args: list[str], cwd: str) -> tuple[int, str]:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    return proc.returncode, (proc.stderr or proc.stdout or "")


def _ensure_symlink_safe_gitignore(workdir: Path) -> None:
    """main .gitignore에 *bare* `node_modules`를 보장(symlink 머지 누수 방지). non-fatal.

    deps.ensure_deps는 `node_modules/`(디렉토리 한정)만 쓴다 → symlink된 node_modules는
    그 패턴으로 무시되지 않아 worktree 머지 `git add -A`에 잡힌다(검증됨). bare 항목을
    main에 박아 worktree들이 상속하게 하면 symlink가 머지에서 안전히 빠진다.
    """
    gi = workdir / ".gitignore"
    try:
        existing = gi.read_text(encoding="utf-8") if gi.exists() else ""
        if _BARE_NODE_MODULES in {ln.strip() for ln in existing.splitlines()}:
            return
        prefix = existing if (existing == "" or existing.endswith("\n")) else existing + "\n"
        gi.write_text(prefix + _BARE_NODE_MODULES + "\n", encoding="utf-8")
    except OSError:
        pass


def commit_scaffold(
    workdir: str | Path,
    message: str = "haetae: scaffold (director stack)",
    *,
    git: GitRunner | None = None,
) -> bool:
    """workdir의 scaffold 변경을 main에 커밋한다(worktree들이 분기 시 상속하도록). best-effort.

    node_modules 등 install 산출물은 .gitignore(ensure_deps + bare 보강)로 staging에서
    빠진다. 커밋 실패는 non-fatal(False) — 파일은 이미 디스크에 있어 worktree 분기 시 보인다.
    호출 전 ensure_deps로 node_modules가 이미 생성돼 있어도 무방(gitignore로 제외).
    """
    git = git or _default_git
    _ensure_symlink_safe_gitignore(Path(workdir))
    wd = str(workdir)
    try:
        git(["add", "-A"], wd)
        rc, _out = git([*_GIT_IDENT, "commit", "-m", message], wd)
        return rc == 0
    except Exception:  # noqa: BLE001 — 커밋 실패는 run을 죽이지 않는다(파일은 이미 디스크에)
        return False


# ──────────────────────────── worktree node_modules 준비 ────────────────────────────


def prepare_worktree_deps(
    main_workdir: str | Path,
    worktree_path: str | Path,
    *,
    ensure_deps_fn: Callable[[Path], object] | None = None,
) -> str:
    """worktree에 node_modules를 준비한다. 반환: "symlink" | "copy" | "install" | "none".

    node_modules는 gitignore라 worktree가 git으로 *상속하지 못한다* → executor dispatch
    *전에* 여기서 따로 채운다. main의 node_modules를 symlink(기본: 빠름·디스크 0)하고,
    symlink 불가면 copytree, 그래도 안 되면 per-worktree host-install(ensure_deps)로 폴백.

    offline executor는 node_modules를 *읽기만* 하면 되므로 공유 symlink로 충분하다.
    bare `node_modules` gitignore(commit_scaffold가 main에 보강)가 상속돼 symlink가
    머지에서 안전히 빠진다.
    """
    main = Path(main_workdir)
    wt = Path(worktree_path)
    src = main / "node_modules"
    dst = wt / "node_modules"

    if src.is_dir() and not dst.exists():
        try:
            os.symlink(src.resolve(), dst, target_is_directory=True)
            return "symlink"
        except OSError:
            try:
                shutil.copytree(src, dst, symlinks=True)
                return "copy"
            except OSError:
                pass  # 폴백으로

    # 폴백: worktree에 매니페스트가 있으면 거기서 직접 host-install(상속된 package.json).
    if ensure_deps_fn is not None and not dst.exists():
        res = ensure_deps_fn(wt)
        return "install" if getattr(res, "installed", False) else "none"
    return "none"
