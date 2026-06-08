"""WO#45 — provider-declared launch-options 디스크립터 테스트.

핵심: provider-agnostic 디스크립터(엔진-free 리프) + codex effort/model 기본 + config
pre-fill(best-effort) + 파싱 실패 흡수. 실행 동작/sandbox와 무관(메타데이터만).
"""

from __future__ import annotations

import ast
from pathlib import Path

from haetae.providers.launch_options import (
    DEFAULT_REASONING_EFFORT,
    REASONING_EFFORT_LEVELS,
    all_launch_options,
    codex_launch_options,
    launch_options_for,
    read_codex_config,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
LO = REPO_ROOT / "src" / "haetae" / "providers" / "launch_options.py"


def _by_name(opts) -> dict:
    return {o.name: o for o in opts}


# ──────────────────── 정적 기본(config 없음) ────────────────────


def test_effort_levels_and_default_medium():
    """effort 레벨은 codex 화이트리스트와 일치하고 기본은 medium."""
    assert REASONING_EFFORT_LEVELS == ("minimal", "low", "medium", "high", "xhigh")
    assert DEFAULT_REASONING_EFFORT == "medium"


def test_codex_descriptor_static_defaults(tmp_path: Path):
    """config 부재 → 정적 기본: effort=medium, model 비움(=자동), 둘 다 optional."""
    missing = tmp_path / "nope.toml"
    opts = _by_name(codex_launch_options(config_path=missing))
    assert opts["reasoning_effort"].kind == "select"
    assert opts["reasoning_effort"].default == "medium"
    assert opts["reasoning_effort"].choices == REASONING_EFFORT_LEVELS
    assert opts["reasoning_effort"].optional is True
    assert opts["reasoning_effort"].hint  # 권장/비용 힌트 존재
    assert opts["model"].kind == "text"
    assert opts["model"].default == ""  # 비움 = codex 기본(자동)
    assert opts["model"].optional is True
    assert opts["model"].placeholder  # "비우면 자동·최신" 안내
    assert opts["model"].hint


def test_codex_descriptor_config_prefill(tmp_path: Path):
    """config 있으면 model/effort를 pre-fill(세팅 추적)."""
    cfg = tmp_path / "config.toml"
    cfg.write_text('model = "gpt-5.5"\nmodel_reasoning_effort = "xhigh"\n', encoding="utf-8")
    opts = _by_name(codex_launch_options(config_path=cfg))
    assert opts["model"].default == "gpt-5.5"
    assert opts["reasoning_effort"].default == "xhigh"


def test_config_prefill_partial(tmp_path: Path):
    """config에 일부만 있어도 그 부분만 pre-fill, 나머지는 정적 기본."""
    cfg = tmp_path / "config.toml"
    cfg.write_text('model_reasoning_effort = "high"\n', encoding="utf-8")
    opts = _by_name(codex_launch_options(config_path=cfg))
    assert opts["reasoning_effort"].default == "high"
    assert opts["model"].default == ""  # model 없음 → 정적(자동)


def test_config_bad_effort_falls_back_to_static(tmp_path: Path):
    """config의 effort가 화이트리스트 밖이면 무시하고 medium 폴백."""
    cfg = tmp_path / "config.toml"
    cfg.write_text('model_reasoning_effort = "ultra"\n', encoding="utf-8")
    opts = _by_name(codex_launch_options(config_path=cfg))
    assert opts["reasoning_effort"].default == "medium"


def test_read_config_parse_failure_absorbed(tmp_path: Path):
    """깨진 TOML → 빈 dict(무크래시)."""
    cfg = tmp_path / "config.toml"
    cfg.write_text("this is = = not valid toml [[[", encoding="utf-8")
    assert read_codex_config(config_path=cfg) == {}


def test_read_config_missing_absorbed(tmp_path: Path):
    assert read_codex_config(config_path=tmp_path / "absent.toml") == {}


# ──────────────────── 레지스트리(provider-agnostic) ────────────────────


def test_launch_options_for_codex_and_human(tmp_path: Path):
    missing = tmp_path / "nope.toml"
    assert {o.name for o in launch_options_for("codex", config_path=missing)} == {
        "reasoning_effort", "model"
    }
    assert launch_options_for("human", config_path=missing) == []  # 사람 릴레이는 옵션 없음


def test_launch_options_for_unknown_executor_is_empty(tmp_path: Path):
    assert launch_options_for("bogus", config_path=tmp_path / "x.toml") == []


def test_all_launch_options_json_shape(tmp_path: Path):
    """엔드포인트용 JSON dict — executor별 옵션 dict 리스트(렌더 필드 전부 포함)."""
    out = all_launch_options(config_path=tmp_path / "nope.toml")
    assert set(out.keys()) >= {"codex", "human"}
    codex = {o["name"]: o for o in out["codex"]}
    eff = codex["reasoning_effort"]
    assert eff["kind"] == "select" and eff["default"] == "medium"
    assert "minimal" in eff["choices"] and "xhigh" in eff["choices"]
    assert codex["model"]["kind"] == "text" and codex["model"]["placeholder"]


# ──────────────────── 엔진-free 리프 가드(격리 유지) ────────────────────


def test_launch_options_is_engine_free():
    """디스크립터 리프는 haetae 엔진/실행 코드를 일절 import하지 않는다(순수 메타데이터).

    이게 대시보드(read-only 뷰어)가 안전하게 import할 수 있는 근거다 — 미러링 없이 provider
    선언을 그대로 공유하되, 엔진 격리 불변식은 유지.
    """
    tree = ast.parse(LO.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            for a in node.names:
                imported.add(a.name)
    assert not any(m.startswith("haetae") for m in imported), f"haetae import 발견: {imported}"
