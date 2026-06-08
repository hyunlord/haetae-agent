"""WorktreeManager — git worktree per unit 격리 + 머지 + **보장된 뒷정리**.

병렬 executor의 핵심 난제 둘:
  1. 격리: 동시에 도는 executor가 같은 workdir을 건드리면 race → unit마다 git
     worktree(+브랜치)를 따로 떼어주고, gate pass면 main(통합 브랜치)에 머지한다.
  2. 뒷정리: 모든 종료 경로(done/escalate/예외/Ctrl-C)에서 worktree·브랜치·관리
     루트가 0이어야 한다. removal 프리미티브(_remove)는 멱등 — 두 번 불러도 안전.

git만 쓴다(LLM 없음). subprocess는 _git 한 곳으로 격리해 테스트/이식을 쉽게.
관리 루트: `<workdir>/.haetae-worktrees/`, 브랜치: `haetae/u-<unit_id>`.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

# 격리 worktree들을 모아두는 관리 루트 디렉토리 이름
ROOT_NAME = ".haetae-worktrees"
# unit 브랜치 접두사 — cleanup이 stray까지 싹 지울 수 있게 네임스페이스를 고정한다
BRANCH_PREFIX = "haetae/u-"
# git 커밋에 쓸 신원(테스트/CI에 user.* 설정이 없을 수 있어 커맨드마다 박는다)
_GIT_IDENT = ("-c", "user.email=haetae@local", "-c", "user.name=haetae")


class WorktreeError(RuntimeError):
    """worktree 작업 실패(git init/add 등 *치명적* 단계). merge 충돌은 예외가 아니라
    반환값("conflict")으로 다룬다 — 충돌은 정상적인 직렬화 트리거이기 때문."""


class WorktreeManager:
    """unit별 git worktree 생성/머지/폐기와 전수 뒷정리를 담당한다.

    workdir: 통합(main) 브랜치가 체크아웃된 작업 루트. 모든 git 작업의 기준.
    main_branch: ensure_repo가 감지/생성한 통합 브랜치 이름(머지 대상).
    """

    def __init__(self, workdir: str | Path):
        self.workdir = Path(workdir)
        self.root = self.workdir / ROOT_NAME
        self.main_branch = "main"
        self._created: set[str] = set()
        # 마지막 merge 충돌의 *겹친 파일* 목록(통합 적응 피드백용, WO#48).
        # merge는 main 스레드에서 직렬 실행되므로 인스턴스 보관이 안전(동시성 없음).
        self.last_conflict_files: list[str] = []
        # WO#52: 통합 백트래킹 — checkpoint()가 기록한 all-merged ref 집합. reset_main_to는
        # *이 집합의 ref로만* reset한다(임의 ref/HEAD~N reset 차단 — 안전 가드).
        self._checkpoints: set[str] = set()

    # ── git 실행 격리 (유일한 subprocess 지점) ────────────────────────────
    def _git(
        self, *args: str, cwd: str | Path | None = None, check: bool = True
    ) -> subprocess.CompletedProcess:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd or self.workdir),
            capture_output=True,
            text=True,
        )
        if check and proc.returncode != 0:
            raise WorktreeError(
                f"git {' '.join(args)} 실패 (exit {proc.returncode}): "
                f"{(proc.stderr or proc.stdout).strip()}"
            )
        return proc

    def _branch(self, unit_id: str) -> str:
        return f"{BRANCH_PREFIX}{unit_id}"

    # ── 1. workdir을 git repo로 (없으면 init + 초기 커밋) ──────────────────
    def ensure_repo(self) -> None:
        """workdir이 *자기 자신을 루트로 하는* git repo가 아니면 init + 초기 커밋.

        부모 repo 안의 하위 디렉토리를 가리켜도(toplevel이 다름) workdir에 독립
        repo를 새로 판다 — haetae worktree/브랜치가 부모 repo를 오염시키지 않도록.
        """
        self.workdir.mkdir(parents=True, exist_ok=True)
        top = self._git("rev-parse", "--show-toplevel", check=False)
        is_own_repo = (
            top.returncode == 0
            and Path(top.stdout.strip()).resolve() == self.workdir.resolve()
        )
        if not is_own_repo:
            self._git("init")
            self._commit_initial()
            # 통합 브랜치 이름을 main으로 정규화(git 기본이 master일 수 있음)
            self._git("branch", "-M", "main", check=False)
            self.main_branch = "main"
        else:
            cur = self._git("rev-parse", "--abbrev-ref", "HEAD", check=False)
            self.main_branch = (cur.stdout.strip() or "main")
            # worktree add는 커밋이 최소 하나 있어야 한다 — 없으면 만든다
            if self._git("rev-parse", "HEAD", check=False).returncode != 0:
                self._commit_initial()

    def _commit_initial(self) -> None:
        self._git(*_GIT_IDENT, "commit", "--allow-empty", "-m", "haetae: initial", check=False)

    # ── 2. create / merge / discard / cleanup ────────────────────────────
    def create(self, unit_id: str) -> Path:
        """main 기준으로 unit 전용 worktree(+브랜치)를 만들어 그 경로를 돌려준다."""
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / unit_id
        # 재dispatch 등으로 잔재가 있으면 먼저 멱등 제거(깨끗한 base에서 다시 분기)
        self._remove(unit_id)
        self._git(
            "worktree", "add", "-b", self._branch(unit_id),
            str(path), self.main_branch,
        )
        self._created.add(unit_id)
        return path

    def merge(self, unit_id: str) -> str:
        """worktree 변경을 커밋하고 그 브랜치를 main에 머지한다.

        반환: "ok" | "conflict". 충돌이면 main을 더럽히지 않게 merge --abort 해서
        깨끗한 상태로 되돌린다(상위가 직렬화 재dispatch로 처리).
        """
        path = self.root / unit_id
        branch = self._branch(unit_id)
        # worktree의 변경 전부 스테이징 + 커밋(--allow-empty: 무변경 unit도 머지 가능)
        self._git("add", "-A", cwd=path, check=False)
        self._git(*_GIT_IDENT, "commit", "--allow-empty", "-m",
                  f"haetae unit {unit_id}", cwd=path, check=False)
        proc = self._git(
            *_GIT_IDENT, "merge", "--no-ff", "-m", f"merge {branch}", branch, check=False
        )
        if proc.returncode != 0:
            # 충돌(또는 머지 거부) → 겹친 파일을 *abort 전에* 캡처(통합 적응 피드백용),
            # 그다음 main 원복 후 충돌 신호. (충돌 파일을 못 잡아도 best-effort — 빈 목록.)
            diff = self._git(
                "diff", "--name-only", "--diff-filter=U", check=False
            )
            self.last_conflict_files = [
                ln.strip() for ln in diff.stdout.splitlines() if ln.strip()
            ]
            self._git("merge", "--abort", check=False)
            return "conflict"
        self.last_conflict_files = []
        return "ok"

    def discard(self, unit_id: str) -> None:
        """머지 없이 버린다(gate 실패/포기/충돌). = 멱등 removal."""
        self._remove(unit_id)

    def cleanup(self, unit_id: str) -> None:
        """unit worktree+브랜치+디렉토리 제거. 머지 성공 후에도 호출(멱등)."""
        self._remove(unit_id)

    # ── 통합 백트래킹: 체크포인트 + 가드된 main reset (WO#52) ────────────────
    def checkpoint(self) -> str | None:
        """현재 main(통합 브랜치) HEAD 커밋 해시를 기록·반환(읽기 전용).

        통합 OR 백트래킹용: all-units-merged 깨끗 상태를 ref로 못박아, 대안 사이에
        reset_main_to(ref)로 *그 상태로만* 되감을 수 있게 한다(기록된 ref만 reset 허용).
        실패 → None(best-effort, 호출부가 #41 동작으로 폴백).
        """
        proc = self._git("rev-parse", "HEAD", check=False)
        ref = proc.stdout.strip()
        if proc.returncode != 0 or not ref:
            return None
        self._checkpoints.add(ref)
        return ref

    def reset_main_to(self, ref: str | None) -> bool:
        """run workdir 빌드 repo의 main을 *기록된 체크포인트* ref로 `git reset --hard`.

        안전 가드(둘 다 통과해야 reset 실행):
          ① ref가 checkpoint()로 *기록된* 것 — 임의 ref·HEAD~N·사용자 입력 ref 거부.
          ② workdir이 *자기 자신을 토플레벨로 하는* git repo — ensure_repo가 판 haetae
             빌드 repo만 해당. 부모/사용자 실 repo(toplevel≠workdir)는 거부 → 실 repo 보호.
        best-effort: 어떤 실패도 False 반환(raise 안 함) → 호출부가 #41 동작(현재 main 위
        재dispatch)으로 폴백. **haetae가 만든 throwaway 빌드 repo만, 기록된 ref로만** reset.
        """
        if not ref or ref not in self._checkpoints:
            return False  # 가드 ①: 기록 안 된 ref reset 거부(임의 ref/HEAD~N 차단)
        top = self._git("rev-parse", "--show-toplevel", check=False)
        if top.returncode != 0:
            return False
        try:
            is_own = Path(top.stdout.strip()).resolve() == self.workdir.resolve()
        except OSError:
            return False
        if not is_own:
            return False  # 가드 ②: 자기-소유 빌드 repo가 아니면 거부(사용자 실 repo 보호)
        return self._git("reset", "--hard", ref, check=False).returncode == 0

    def _remove(self, unit_id: str) -> None:
        """removal 프리미티브 — 멱등. worktree remove + branch -D + rmtree."""
        path = self.root / unit_id
        self._git("worktree", "remove", "--force", str(path), check=False)
        self._git("branch", "-D", self._branch(unit_id), check=False)
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
        self._created.discard(unit_id)

    # ── 3. 전수 뒷정리 — main만 남기고 흔적 0 ─────────────────────────────
    def cleanup_all(self) -> None:
        """모든 haetae worktree/브랜치/관리루트를 제거한다.

        반드시 모든 종료 경로(loop의 try/finally)에서 호출된다. 멱등하며 절대
        예외를 던지지 않는다(뒷정리가 또 다른 실패의 원인이 되면 안 됨).
        crash로 _created에 안 잡힌 stray까지 worktree list/for-each-ref로 훑어 지운다.
        """
        try:
            # 1) 추적 중인 unit 제거
            for unit in list(self._created):
                self._remove(unit)
            self._created.clear()

            # 2) stray worktree: 관리루트 아래를 가리키는 등록 worktree 강제 제거
            wl = self._git("worktree", "list", "--porcelain", check=False)
            root_resolved = self.root.resolve()
            for line in wl.stdout.splitlines():
                if not line.startswith("worktree "):
                    continue
                wt_path = Path(line[len("worktree "):].strip())
                try:
                    under_root = root_resolved in wt_path.resolve().parents
                except OSError:
                    under_root = str(wt_path).startswith(str(self.root))
                if under_root:
                    self._git("worktree", "remove", "--force", str(wt_path), check=False)

            # 3) stray 브랜치: haetae/* 전부 삭제
            br = self._git(
                "for-each-ref", "--format=%(refname:short)", "refs/heads/haetae",
                check=False,
            )
            for name in br.stdout.split():
                self._git("branch", "-D", name, check=False)

            # 4) 관리루트 디렉토리 제거 + prune
            if self.root.exists():
                shutil.rmtree(self.root, ignore_errors=True)
            self._git("worktree", "prune", check=False)
        except Exception:  # noqa: BLE001 — 뒷정리는 절대 run을 죽이지 않는다
            pass
