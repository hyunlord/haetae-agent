"""분해 critic(WO#40, Phase C) 단위 테스트 — mock LLM만(네트워크/시크릿 없음).

critique_decomposition: progress/weak 판정 + best-effort 흡수(파싱 실패·client 예외).
+ 정규화 변종 흡수, is_weak, build_decomp_feedback.
"""

from pathlib import Path

import yaml

from haetae.decomp_critic import (
    build_decomp_feedback,
    critique_decomposition,
    is_weak,
)
from haetae.llm import MockClient
from haetae.models import (
    DecompCritique,
    NextOrder,
    PlanItem,
    PlanState,
    ProjectSpec,
    State,
    Status,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPT_DIR = REPO_ROOT / "prompts"
DECOMP_PROMPT = PROMPT_DIR / "decomp_critic.md"

_SPEC_DICT = {
    "spec_id": "dc-001",
    "version": 1,
    "order_raw": "리테일 흐름 시뮬레이션을 만들어라",
    "goal": "리테일 매장 고객 흐름 시뮬레이션",
    "task_type": "feature_impl",
    "verifiability": "objective",
    "mode": "normal",
    "constraints": [],
    "acceptance_criteria": [
        {"id": "ac1", "desc": "고객 에이전트 이동", "check": {"type": "test", "cmd": "pytest x"}}
    ],
    "assumptions": [],
    "non_goals": [],
    "done_when": "리테일 흐름이 실제로 시뮬레이션됨",
    "decomposition": [
        {"unit": "u1", "desc": "데이터 모델", "deps": []},
        {"unit": "u2", "desc": "렌더", "deps": ["u1"]},
    ],
}


def _spec() -> ProjectSpec:
    return ProjectSpec.model_validate(_SPEC_DICT)


def _state() -> State:
    return State(
        spec_ref="dc-001", spec_version=1, status=Status.running,
        plan=[
            PlanItem(unit="u1", state=PlanState.pending, deps=None),
            PlanItem(unit="u2", state=PlanState.pending, deps=["u1"]),
        ],
    )


def _order(unit: str = "u1", goal: str = "데이터 모델(스키마+CRUD)만 구현") -> NextOrder:
    return NextOrder(unit=unit, goal=goal, deliverable="요약")


_PROGRESS_YAML = "verdict: progress\nreason: \"전체 goal 중 데이터 모델 한 조각만 좁힘 — 진전.\"\n"
_WEAK_YAML = "verdict: weak\nreason: \"work order goal이 전체 done_when을 그대로 재진술 — 무진전.\"\n"


# ──────────────────────────── 판정 ────────────────────────────


def test_critique_progress():
    crit = critique_decomposition(
        _order(), _spec(), _state(), MockClient([_PROGRESS_YAML]),
        prompt_path=DECOMP_PROMPT,
    )
    assert crit.verdict == "progress"
    assert not is_weak(crit)
    assert crit.unit == "u1"


def test_critique_weak_on_restated_goal():
    # 전체 goal을 그대로 재진술한 work order → weak.
    crit = critique_decomposition(
        _order(goal="리테일 매장 고객 흐름 시뮬레이션을 만들어라"),
        _spec(), _state(), MockClient([_WEAK_YAML]), prompt_path=DECOMP_PROMPT,
    )
    assert crit.verdict == "weak"
    assert is_weak(crit)
    assert crit.reason


def test_critique_user_prompt_includes_order_and_progress():
    """critic user 프롬프트에 work order(goal) + spec goal/done_when + plan 진행이 들어간다."""
    client = MockClient([_PROGRESS_YAML])
    critique_decomposition(_order(), _spec(), _state(), client, prompt_path=DECOMP_PROMPT)
    user = client.calls[0]["user"]
    assert "데이터 모델(스키마+CRUD)만 구현" in user  # work order goal
    assert "리테일 흐름이 실제로 시뮬레이션됨" in user  # spec done_when
    assert "pending: u1, u2" in user  # plan 진행


# ──────────────────────────── best-effort 흡수 ────────────────────────────


def test_critique_parse_failure_degrades_to_progress():
    """깨진 출력 → progress로 흡수(진행 막지 않음), reason에 사유."""
    crit = critique_decomposition(
        _order(), _spec(), _state(), MockClient(["이건 YAML이 아니다 {{{"]),
        prompt_path=DECOMP_PROMPT,
    )
    assert crit.verdict == "progress"
    assert not is_weak(crit)
    assert "평가 불가" in (crit.reason or "")


def test_critique_client_exception_degrades_to_progress():
    """client.complete가 던져도 raise하지 않고 progress로 흡수."""

    class _Raise:
        def complete(self, system, user, **opts):
            raise RuntimeError("codex 다운")

    crit = critique_decomposition(
        _order(), _spec(), _state(), _Raise(), prompt_path=DECOMP_PROMPT
    )
    assert crit.verdict == "progress"
    assert "실행 실패" in (crit.reason or "")


def test_critique_unknown_verdict_is_progress():
    """미지 verdict는 보수적으로 progress(오버블록 금지)."""
    crit = critique_decomposition(
        _order(), _spec(), _state(), MockClient(["verdict: maybe\nreason: x\n"]),
        prompt_path=DECOMP_PROMPT,
    )
    assert crit.verdict == "progress"


def test_critique_normalizes_weak_aliases():
    """no-progress/restated 등 변종도 weak로 정규화."""
    for alias in ("no-progress", "restated", "stuck"):
        crit = critique_decomposition(
            _order(), _spec(), _state(),
            MockClient([f"verdict: {alias}\nrationale: 무진전\n"]),
            prompt_path=DECOMP_PROMPT,
        )
        assert crit.verdict == "weak", alias
        assert crit.reason == "무진전"  # rationale → reason 흡수


# ──────────────────────────── 헬퍼 ────────────────────────────


def test_build_decomp_feedback_includes_reason():
    fb = build_decomp_feedback(DecompCritique(verdict="weak", reason="전체 재진술", unit="u1"))
    assert "전체 재진술" in fb
    assert "더 작은" in fb  # 더 작은 진전 스텝으로 분해 지시
