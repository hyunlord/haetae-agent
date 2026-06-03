"""ProjectSpec / State 모델 검증 테스트.

- positive: repo의 예시 스키마 YAML 두 개가 각 모델로 로드·검증된다.
- negative: 필수 필드 누락 / 잘못된 enum 값은 ValidationError로 거부된다.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from haetae.models import (
    Mode,
    ProjectSpec,
    State,
    Status,
    TaskType,
    Verifiability,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC_DIR = REPO_ROOT / "spec"


# ──────────────────────────── positive ────────────────────────────


def test_projectspec_example_yaml_loads():
    spec = ProjectSpec.from_yaml(SPEC_DIR / "projectspec.schema.yaml")
    assert spec.spec_id == "ws-combat-001"
    assert spec.task_type is TaskType.feature_impl
    assert spec.verifiability is Verifiability.objective
    assert spec.mode is Mode.normal
    # acceptance_criteria의 check.type이 enum으로 파싱됐는지
    assert spec.acceptance_criteria[0].check.type.value == "bench"
    assert spec.acceptance_criteria[0].check.pass_ == ">=10"
    # non_goals 최소 2개
    assert len(spec.non_goals) >= 2


def test_state_example_yaml_loads():
    state = State.from_yaml(SPEC_DIR / "state.schema.yaml")
    assert state.spec_ref == "ws-needs-expand-001"
    assert state.status is Status.running
    assert state.plan[0].state.value == "done"
    assert state.events[0].seq == 1
    assert state.events[0].checks[0].type.value == "bench"
    assert state.events[0].checks[0].pass_ is True
    assert state.budget.cap.usd == 5.00


# ──────────────────────────── negative ────────────────────────────


def test_projectspec_missing_required_field_rejected():
    """goal 누락 → ValidationError."""
    bad = {
        "spec_id": "x",
        "version": 1,
        "order_raw": "주문",
        # goal 누락
        "task_type": "feature_impl",
        "verifiability": "objective",
        "mode": "normal",
        "acceptance_criteria": [
            {"id": "ac1", "desc": "d", "check": {"type": "test"}}
        ],
        "non_goals": ["a", "b"],
        "done_when": "끝",
    }
    with pytest.raises(ValidationError):
        ProjectSpec.model_validate(bad)


def test_projectspec_invalid_enum_rejected():
    """task_type에 정의되지 않은 값 → ValidationError."""
    bad = {
        "spec_id": "x",
        "version": 1,
        "order_raw": "주문",
        "goal": "목표",
        "task_type": "totally_made_up",  # 잘못된 enum
        "verifiability": "objective",
        "mode": "normal",
        "acceptance_criteria": [
            {"id": "ac1", "desc": "d", "check": {"type": "test"}}
        ],
        "non_goals": ["a", "b"],
        "done_when": "끝",
    }
    with pytest.raises(ValidationError):
        ProjectSpec.model_validate(bad)


def test_state_invalid_status_rejected():
    """status에 정의되지 않은 값 → ValidationError."""
    bad = {
        "spec_ref": "x",
        "spec_version": 1,
        "status": "vibing",  # 잘못된 enum
    }
    with pytest.raises(ValidationError):
        State.model_validate(bad)


def test_state_invalid_plan_state_rejected():
    """plan[].state에 잘못된 enum → ValidationError."""
    bad = {
        "spec_ref": "x",
        "spec_version": 1,
        "status": "running",
        "plan": [{"unit": "u1", "state": "almost_done"}],  # 잘못된 enum
    }
    with pytest.raises(ValidationError):
        State.model_validate(bad)
