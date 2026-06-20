"""WO#128-B — server-less 게임플레이 검증 스티어링 테스트(#126 "플랫포머 onset", 안전 강화).

#126서 플랫포머 브라우저-하니스가 127.0.0.1 서버(headless Chrome / vite --host)를 띄워 관측하려다
샌드박스 loopback listen EPERM 차단 → trace:browser-render 통합 실패. 수정(빌더-측 스티어링):
게임플레이 검증의 *행동 권위* = in-sandbox engine-trace(서버 불요·전체 행동 증거), browser-render는
best-effort, 127.0.0.1/loopback listen 호스팅 금지(샌드박스 EPERM·안전 불변 강화). 검증 깊이는 유지.

**빌더-측만**(합성기 프롬프트 + verification-harness 스킬) — 적대 run-judge·gate·바·codex·
ALLOWED_SANDBOXES 불변. 본 테스트는 텍스트 스티어링 존재 + ALLOWED_SANDBOXES 불변(강화)만 단언한다.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SYNTH = (REPO_ROOT / "prompts" / "synthesizer.md").read_text(encoding="utf-8")
SKILL = (REPO_ROOT / "skills" / "verification-harness" / "SKILL.md").read_text(encoding="utf-8")
CODEX_SRC = (REPO_ROOT / "src" / "haetae" / "providers" / "codex.py").read_text(encoding="utf-8")


# ════════════════════ 합성기 프롬프트 — server-less 게임플레이 ════════════════════


def test_synthesizer_mandates_serverless_gameplay():
    """합성기: 게임플레이 검증 = server-less·engine-trace 행동 권위·loopback 서버 금지."""
    low = SYNTH.lower()
    assert "server-less" in low                       # server-less 명시
    assert "engine-trace" in low                      # engine-trace = 행동 권위
    assert "127.0.0.1" in SYNTH                        # loopback 서버 금지 대상 명시
    assert "loopback" in low
    assert "eperm" in low                             # 샌드박스 차단 근거
    assert "best-effort" in low                       # browser-render best-effort
    assert "file://" in SYNTH or "data-uri" in low    # 서버리스 렌더 로드 경로


def test_synthesizer_forbids_loopback_server_and_integration_fail():
    """합성기: loopback 서버 호스팅 금지 + browser-render가 통합 실패 유발 금지(best-effort)."""
    assert "서버를 띄우지 마라" in SYNTH               # 127.0.0.1/loopback 호스팅 금지
    assert "통합 실패를 유발하면 안" in SYNTH          # browser-render best-effort(통합 fail 금지)


# ════════════════════ verification-harness 스킬 — server-less 섹션 ════════════════════


def test_skill_has_serverless_gameplay_section():
    """스킬: server-less 게임플레이 검증 섹션(engine-trace 권위·loopback 금지·EPERM 근거)."""
    low = SKILL.lower()
    assert "server-less" in low
    assert "127.0.0.1" in SKILL
    assert "eperm" in low
    assert "engine-trace" in low
    assert "best-effort" in low
    assert "file://" in SKILL or "data-uri" in low


def test_skill_preserves_verification_depth_not_hollow():
    """B는 *검증 깊이 유지* — engine-trace가 전체 행동 권위(hollow 아님), 서버만 뺀다(얕아지지 않음)."""
    assert "행동 권위" in SKILL
    assert "hollow 아님" in SKILL                      # 깊이 유지 명시(서버 제거 ≠ 검증 약화)


# ════════════════════ 안전 불변 — ALLOWED_SANDBOXES 불변(강화) ════════════════════


def test_allowed_sandboxes_unchanged_read_only_workspace_write():
    """안전 불변: ALLOWED_SANDBOXES는 read-only/workspace-write만 — #128이 *건드리지 않음*(강화).

    B는 ALLOWED_SANDBOXES를 완화(loopback/network 허용)하는 게 아니라 *server-less*로 우회하므로,
    이 화이트리스트는 정확히 그대로여야 한다(#84 강화 — loopback 호스팅을 *빌더-측*에서 금지).
    """
    assert 'ALLOWED_SANDBOXES = ("read-only", "workspace-write")' in CODEX_SRC
    # danger-full-access(loopback/네트워크 포함 가능)는 여전히 거부됨(완화 0).
    assert "danger-full-access 금지" in CODEX_SRC


def test_serverless_steering_is_builder_side_only():
    """빌더-측만: server-less 스티어링은 합성기 프롬프트 + 스킬에만 — run-judge/gate 프롬프트 무주입."""
    run_judge = (REPO_ROOT / "prompts" / "run_judge.md").read_text(encoding="utf-8")
    judge = (REPO_ROOT / "prompts" / "judge.md").read_text(encoding="utf-8")
    # 적대 판정 프롬프트엔 server-less/loopback 스티어링이 새로 들어가지 않는다(판정층 불변).
    assert "server-less" not in run_judge.lower()
    assert "server-less" not in judge.lower()
