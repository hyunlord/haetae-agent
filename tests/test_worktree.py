"""WorktreeManager 테스트 — 실제 git(tmp_path)으로 격리/머지/충돌/뒷정리 검증.

mock 없이 진짜 git을 쓴다(격리·머지·cleanup은 git 동작에 의존하므로). codex/LLM은 없다.
"""

import subprocess
from pathlib import Path

from haetae.worktree import BRANCH_PREFIX, ROOT_NAME, WorktreeManager


def _git(workdir, *args):
    return subprocess.run(["git", *args], cwd=workdir, capture_output=True, text=True)


def _worktree_count(workdir) -> int:
    out = _git(workdir, "worktree", "list").stdout
    return len([ln for ln in out.splitlines() if ln.strip()])


def _haetae_branches(workdir) -> list[str]:
    out = _git(workdir, "branch", "--list", f"{BRANCH_PREFIX}*").stdout
    # git은 현재 브랜치에 '* ', worktree 체크아웃 브랜치에 '+ ' 마커를 붙인다 → 벗긴다
    return [ln.strip().lstrip("*+ ").strip() for ln in out.splitlines() if ln.strip()]


def assert_clean(workdir):
    """main worktree만, haetae 브랜치 0, 관리루트 디렉토리 사라짐 — 흔적 0."""
    assert _worktree_count(workdir) == 1, "main worktree만 남아야 한다"
    assert _haetae_branches(workdir) == [], "haetae/u-* 브랜치가 0이어야 한다"
    assert not (Path(workdir) / ROOT_NAME).exists(), "관리루트가 사라져야 한다"


# ──────────────────────────── ensure_repo ────────────────────────────


def test_ensure_repo_inits_non_repo(tmp_path):
    wm = WorktreeManager(tmp_path)
    wm.ensure_repo()
    assert (tmp_path / ".git").exists()
    assert wm.main_branch == "main"
    # 커밋이 최소 하나 존재(worktree add 가능 상태)
    assert _git(tmp_path, "rev-parse", "HEAD").returncode == 0


def test_ensure_repo_idempotent(tmp_path):
    wm = WorktreeManager(tmp_path)
    wm.ensure_repo()
    wm.ensure_repo()  # 두 번 불러도 안전
    assert _worktree_count(tmp_path) == 1


# ──────────────────────────── create / merge ────────────────────────────


def test_create_makes_worktree_and_branch(tmp_path):
    wm = WorktreeManager(tmp_path)
    wm.ensure_repo()
    path = wm.create("u1")
    assert path.exists()
    assert path == tmp_path / ROOT_NAME / "u1"
    assert f"{BRANCH_PREFIX}u1" in _haetae_branches(tmp_path)
    wm.cleanup_all()


def test_merge_brings_unit_changes_into_main(tmp_path):
    wm = WorktreeManager(tmp_path)
    wm.ensure_repo()
    p = wm.create("u1")
    (p / "feature.txt").write_text("hello\n")
    assert wm.merge("u1") == "ok"
    # main 워킹트리에 머지 반영
    assert (tmp_path / "feature.txt").read_text() == "hello\n"
    wm.cleanup_all()


def test_merge_empty_unit_is_ok(tmp_path):
    # 변경이 없어도(--allow-empty) 머지는 ok — 무변경 unit이 run을 막지 않는다.
    wm = WorktreeManager(tmp_path)
    wm.ensure_repo()
    wm.create("u1")
    assert wm.merge("u1") == "ok"
    wm.cleanup_all()


def test_conflicting_merge_returns_conflict_and_keeps_main_clean(tmp_path):
    wm = WorktreeManager(tmp_path)
    wm.ensure_repo()
    p1 = wm.create("u1")
    p2 = wm.create("u2")  # 둘 다 초기 main 기준(파일 없음)
    (p1 / "shared.txt").write_text("u1\n")
    (p2 / "shared.txt").write_text("u2\n")
    assert wm.merge("u1") == "ok"
    # u2는 옛 main 기준이라 add/add 충돌
    assert wm.merge("u2") == "conflict"
    # 충돌 후 main은 깨끗(merge --abort) — u1 내용만 있고 머지 진행 흔적 없음
    assert (tmp_path / "shared.txt").read_text() == "u1\n"
    assert _git(tmp_path, "rev-parse", "MERGE_HEAD").returncode != 0  # 진행중 머지 없음
    wm.cleanup_all()


def test_merge_captures_conflict_files_for_integration_feedback(tmp_path):
    """WO#48: 충돌 시 겹친 파일을 last_conflict_files에 캡처(통합 피드백용). ok면 비운다."""
    wm = WorktreeManager(tmp_path)
    wm.ensure_repo()
    p1 = wm.create("u1")
    p2 = wm.create("u2")
    (p1 / "shared.txt").write_text("u1\n")
    (p2 / "shared.txt").write_text("u2\n")
    assert wm.merge("u1") == "ok"
    assert wm.last_conflict_files == []  # 깨끗한 머지는 빈 목록
    assert wm.merge("u2") == "conflict"
    # 겹친(충돌) 파일을 abort 전에 캡처
    assert "shared.txt" in wm.last_conflict_files
    wm.cleanup_all()


def test_conflict_resolved_by_redispatch_on_updated_main(tmp_path):
    # 충돌 → discard → 갱신된 main에서 재create → 재머지 ok (직렬화 해소).
    wm = WorktreeManager(tmp_path)
    wm.ensure_repo()
    p1 = wm.create("u1")
    p2 = wm.create("u2")
    (p1 / "shared.txt").write_text("u1\n")
    (p2 / "shared.txt").write_text("u2\n")
    assert wm.merge("u1") == "ok"
    wm.cleanup("u1")
    assert wm.merge("u2") == "conflict"
    wm.discard("u2")
    # 재dispatch: 갱신된 main(=u1 반영) 위에 새 worktree
    p2b = wm.create("u2")
    (p2b / "shared.txt").write_text("u2\n")
    assert wm.merge("u2") == "ok"
    wm.cleanup_all()


# ──────────────────────────── 뒷정리 (핵심) ────────────────────────────


def test_cleanup_all_removes_all_traces(tmp_path):
    wm = WorktreeManager(tmp_path)
    wm.ensure_repo()
    for u in ("u1", "u2", "u3"):
        wm.create(u)
    wm.cleanup_all()
    assert_clean(tmp_path)


def test_cleanup_all_idempotent(tmp_path):
    wm = WorktreeManager(tmp_path)
    wm.ensure_repo()
    wm.create("u1")
    wm.cleanup_all()
    wm.cleanup_all()  # 두 번 불러도 안전(예외 없음)
    assert_clean(tmp_path)


def test_cleanup_all_sweeps_stray_branches(tmp_path):
    # _created에 안 잡힌 stray 브랜치/worktree(crash 시뮬)도 싹 지운다.
    wm = WorktreeManager(tmp_path)
    wm.ensure_repo()
    wm.create("u1")
    wm._created.clear()  # bookkeeping 유실(crash 흉내)
    wm.cleanup_all()
    assert_clean(tmp_path)
