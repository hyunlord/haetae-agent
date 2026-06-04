"""intake 정규화 안전망 테스트 — codex 없이 결정적.

WO#10에서 관측된 3종 변종(객체형 constraints/non_goals, decomposition.id,
check.command)이 정규화 후 유효 ProjectSpec으로 검증되는지 확인한다.
"""

from pathlib import Path

import pytest

from haetae.intake import SynthesisError, _normalize_spec_dict, synthesize
from haetae.llm import MockClient
from haetae.models import ProjectSpec

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPT_PATH = REPO_ROOT / "prompts" / "synthesizer.md"

# WO#10 주문1 raw를 본뜬, 3종 변종이 모두 든 응답(필수 필드는 채움).
VARIANT_YAML = """\
spec_id: todo-cli-001
version: 1
order_raw: "todo CLI를 Python으로"
goal: "최소 todo CLI 구현"
task_type: feature_impl
verifiability: objective
mode: normal
constraints:
  - { id: C1, desc: "구현 언어는 Python이다." }
acceptance_criteria:
  - id: ac1
    desc: "add 동작"
    check: { type: test, command: "python -m pytest" }
non_goals:
  - { id: NG1, desc: "웹 UI 없음" }
  - { id: NG2, desc: "동기화 없음" }
done_when: "모든 ac 통과"
decomposition:
  - { id: U1, desc: "조사" }
  - { id: U2, desc: "구현" }
open_questions: []
"""


# ──────────────────────────── 단위: _normalize_spec_dict ────────────────────────────


def test_normalize_object_constraints_and_non_goals():
    d = _normalize_spec_dict(
        {
            "constraints": [{"id": "C1", "desc": "Python만"}],
            "non_goals": [{"id": "NG1", "desc": "웹 없음"}, "이미 문자열"],
        }
    )
    assert d["constraints"] == ["Python만"]
    assert d["non_goals"] == ["웹 없음", "이미 문자열"]


def test_normalize_decomposition_id_to_unit():
    d = _normalize_spec_dict(
        {"decomposition": [{"id": "U1", "desc": "x"}, {"unit": "U2", "desc": "y"}]}
    )
    assert d["decomposition"][0]["unit"] == "U1"  # id → unit
    assert d["decomposition"][1]["unit"] == "U2"  # 이미 unit이면 그대로


def test_normalize_check_command_to_cmd():
    d = _normalize_spec_dict(
        {
            "acceptance_criteria": [
                {"id": "ac1", "desc": "d", "check": {"type": "test", "command": "pytest"}}
            ]
        }
    )
    chk = d["acceptance_criteria"][0]["check"]
    assert chk["cmd"] == "pytest"
    assert "command" not in chk


def test_normalize_leaves_valid_dict_intact():
    valid = {
        "constraints": ["a"],
        "non_goals": ["b", "c"],
        "decomposition": [{"unit": "u1", "desc": "x", "deps": []}],
        "acceptance_criteria": [
            {"id": "ac1", "desc": "d", "check": {"type": "test", "cmd": "true"}}
        ],
    }
    assert _normalize_spec_dict(valid) == valid


# ──────────────────────────── end-to-end: synthesize ────────────────────────────


def test_synthesize_absorbs_variants():
    spec = synthesize("x", MockClient(VARIANT_YAML), prompt_path=PROMPT_PATH)
    assert isinstance(spec, ProjectSpec)
    assert spec.constraints == ["구현 언어는 Python이다."]
    assert spec.non_goals == ["웹 UI 없음", "동기화 없음"]
    assert spec.acceptance_criteria[0].check.cmd == "python -m pytest"
    assert [u.unit for u in spec.decomposition] == ["U1", "U2"]


def test_synthesize_still_rejects_missing_required():
    """정규화는 만능이 아님 — 필수 필드 누락은 여전히 SynthesisError."""
    bad = VARIANT_YAML.replace("spec_id: todo-cli-001\n", "")  # spec_id 제거
    with pytest.raises(SynthesisError):
        synthesize("x", MockClient(bad), prompt_path=PROMPT_PATH)
