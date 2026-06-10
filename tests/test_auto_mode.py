"""제로-config auto 모드 테스트 (WO#65).

설계: `--auto`면 order만으로 *미설정* 운영 knob을 sensible 기본으로 채운다(tier 사다리·
critic-model·scaffold/skills·parallel/timeout). **명시 플래그가 auto를 오버라이드**.
**거버넌스(능력 채택·네트워크·bar)는 절대 자동 안 켬(사람 게이트 유지).** 해석 결과는
이벤트/state에 투명하게 노출.

codex 없이 — resolve_auto_config는 순수 함수, main() 배선은 run() 캡처로 검증.
"""

from pathlib import Path

import haetae.run as run_mod
from haetae.executors import Tier
from haetae.loop import STAGE_AUTO_CONFIG, MockExecutor, MockGate, run_loop
from haetae.models import State, Status, Verdict
from haetae.run import main, resolve_auto_config

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPT_DIR = REPO_ROOT / "prompts"


def _capture_main_run(monkeypatch):
    """main()이 run()에 넘기는 kwargs를 캡처(실제 루프 미실행)."""
    captured = {}

    def fake_run(order, **kwargs):
        captured["order"] = order
        captured.update(kwargs)
        return State(spec_ref="x", spec_version=1, status=Status.done)

    monkeypatch.setattr(run_mod, "run", fake_run)
    return captured


# config 읽기를 막아 codex config pre-fill이 테스트 머신마다 흔들리지 않게(결정성).
def _no_codex_config(monkeypatch):
    monkeypatch.setattr(run_mod, "read_codex_config", lambda *a, **k: {})


# ──────────────────────── resolve_auto_config (순수) ────────────────────────


class _Args:
    """argparse Namespace 흉내 — resolve_auto_config가 읽는 필드만."""

    def __init__(self, **kw):
        self.model = kw.get("model")
        self.reasoning_effort = kw.get("reasoning_effort")
        self.tier_ladder = kw.get("tier_ladder")
        self.critic_model = kw.get("critic_model")
        self.scaffold = kw.get("scaffold", True)
        self.skills = kw.get("skills", True)
        self.max_parallel = kw.get("max_parallel", 4)


def test_auto_fills_default_effort_ladder():
    """미설정이면 effort 사다리 medium→high→xhigh(빌더 model 고정)로 자동 ON."""
    cfg = resolve_auto_config(_Args(), config_path="/nonexistent")
    assert cfg.tier_ladder == [Tier(None, "medium"), Tier(None, "high"), Tier(None, "xhigh")]
    assert cfg.critic_on is True  # critic 절대 OFF 아님


def test_auto_critic_set_and_independence_warned_single_provider():
    """auto critic-model 설정(OFF 아님). 단일 model이면 빌더와 동일 → 독립성 경고."""
    cfg = resolve_auto_config(_Args(model="gpt-x"), config_path="/nonexistent")
    assert cfg.critic_model == "gpt-x"          # critic ON, 빌더 model로
    assert cfg.critic_independent is False       # 단일 provider → 분리 불가
    assert any("독립성" in w for w in cfg.warnings)
    assert "non-indep" in cfg.summary


def test_auto_critic_independent_when_explicit_different():
    """명시 --critic-model이 빌더와 다르면 독립(경고 없음)."""
    cfg = resolve_auto_config(
        _Args(model="gpt-x", critic_model="gpt-y"), config_path="/nonexistent"
    )
    assert cfg.critic_model == "gpt-y"
    assert cfg.critic_independent is True
    assert not cfg.warnings
    assert "indep" in cfg.summary and "non-indep" not in cfg.summary


def test_auto_explicit_effort_overrides_ladder():
    """명시 --reasoning-effort면 자동 effort 사다리 대신 그 effort 단일 tier(오버라이드)."""
    cfg = resolve_auto_config(_Args(reasoning_effort="high"), config_path="/nonexistent")
    assert cfg.tier_ladder == [Tier(None, "high")]


def test_auto_explicit_tier_ladder_wins():
    """명시 --tier-ladder가 자동 사다리를 이긴다."""
    cfg = resolve_auto_config(
        _Args(tier_ladder="m0:medium,m1:xhigh"), config_path="/nonexistent"
    )
    assert cfg.tier_ladder == [Tier("m0", "medium"), Tier("m1", "xhigh")]


def test_auto_summary_surfaces_knobs():
    """summary에 사다리·critic·scaffold·parallel·skills·governance가 한눈에."""
    cfg = resolve_auto_config(_Args(max_parallel=8), config_path="/nonexistent")
    s = cfg.summary
    assert "auto-config:" in s and "ladder=[" in s
    assert "critic=" in s and "scaffold=on" in s
    assert "parallel=8" in s and "skills=on" in s
    assert "governance=manual" in s  # 거버넌스 자동 미활성 표기


# ──────────────────────── main() 배선 (run() 캡처) ────────────────────────


def test_main_auto_fills_ladder_and_critic(monkeypatch):
    """--auto + order만 → tier_ladder 자동(3칸)·critic_client ON·auto_config_note 전달."""
    _no_codex_config(monkeypatch)
    captured = _capture_main_run(monkeypatch)
    rc = main(["--order", "x", "--auto"])
    assert rc == 0
    assert captured["tier_ladder"] == [Tier(None, "medium"), Tier(None, "high"), Tier(None, "xhigh")]
    assert captured["critic_client"] is not None        # critic 강제 ON(auto)
    assert captured["auto_config_note"] and "auto-config:" in captured["auto_config_note"]


def test_main_auto_explicit_overrides(monkeypatch):
    """--auto --max-parallel 2 --reasoning-effort high → 명시값이 auto 기본을 이김."""
    _no_codex_config(monkeypatch)
    captured = _capture_main_run(monkeypatch)
    main(["--order", "x", "--auto", "--max-parallel", "2", "--reasoning-effort", "high"])
    assert captured["max_parallel"] == 2                 # 명시 parallel
    assert captured["tier_ladder"] == [Tier(None, "high")]  # 명시 effort = 단일 tier(오버라이드)


def test_main_auto_governance_not_auto(monkeypatch):
    """--auto만으론 거버넌스 미활성: capabilities OFF·allowlist 미확장·searcher None."""
    _no_codex_config(monkeypatch)
    captured = _capture_main_run(monkeypatch)
    main(["--order", "x", "--auto"])
    assert captured["capabilities_on"] is False          # 능력 채택 자동 안 켬
    assert captured["capability_allowlist"] is None       # allowlist 미확장(capabilities OFF)
    assert captured["capability_searcher"] is None        # capability-search(네트워크) OFF


def test_main_auto_executor_stays_default_human(monkeypatch):
    """auto는 executor 타입을 안 바꾼다 — 기본 human(자율 쓰기는 명시 opt-in)."""
    from haetae.executors import HumanRelayExecutor
    _no_codex_config(monkeypatch)
    captured = _capture_main_run(monkeypatch)
    main(["--order", "x", "--auto"])
    assert isinstance(captured["executor"], HumanRelayExecutor)


def test_main_no_auto_is_back_compat(monkeypatch):
    """--auto 없으면 기존 경로: 단일 tier·critic 게이트(critic-model 없으면 OFF)·note 없음."""
    _no_codex_config(monkeypatch)
    captured = _capture_main_run(monkeypatch)
    main(["--order", "x"])
    assert captured["tier_ladder"] == [Tier(None, None)]  # 단일 tier(기존 동작)
    assert captured["critic_client"] is None              # --critic-model 없음 → OFF(기존)
    assert captured["auto_config_note"] is None


# ──────────────────────── 투명성: state/이벤트 기록 ────────────────────────

SPEC_YAML = """\
spec_id: auto-001
version: 1
order_raw: "x"
goal: "g"
task_type: feature_impl
verifiability: objective
mode: normal
constraints: []
acceptance_criteria:
  - id: ac1
    desc: "d"
    check: { type: test, cmd: "true" }
assumptions: []
non_goals: ["n"]
done_when: "ac1"
decomposition:
  - { unit: u1, desc: a, deps: [] }
open_questions: []
"""

_NEXT = """\
verdict: pass
action: next_order
rationale: "u1"
next_order: { unit: u1, goal: "u1 구현", deliverable: "요약" }
"""
_STOP = "verdict: done\naction: stop\nrationale: done\n"


def test_auto_config_note_recorded_in_state_and_events(monkeypatch):
    """auto_config_note가 emit(이벤트) + state.transitions(STAGE_AUTO_CONFIG)에 기록된다."""
    from haetae.llm import MockClient

    seen: list[str] = []
    state = run_loop(
        order="x", client=MockClient([SPEC_YAML, _NEXT, _STOP]),
        executor=MockExecutor("ok"), gate=MockGate([Verdict.pass_, Verdict.done]),
        prompt_dir=PROMPT_DIR, progress=seen.append,
        auto_config_note="auto-config: ladder=[m/medium] · critic=x(non-indep)",
    )
    # 이벤트(progress)로 노출.
    assert any("auto-config:" in m for m in seen)
    # state transition으로 기록.
    assert any(t.stage == STAGE_AUTO_CONFIG for t in state.transitions)


def test_no_auto_note_no_transition(monkeypatch):
    """auto_config_note 없으면 STAGE_AUTO_CONFIG transition 없음(back-compat)."""
    from haetae.llm import MockClient

    state = run_loop(
        order="x", client=MockClient([SPEC_YAML, _NEXT, _STOP]),
        executor=MockExecutor("ok"), gate=MockGate([Verdict.pass_, Verdict.done]),
        prompt_dir=PROMPT_DIR,
    )
    assert not any(t.stage == STAGE_AUTO_CONFIG for t in state.transitions)
