"""WO#99 — 하니스 탐지 정교화(준비/스캐폴드 유닛 과매칭 제외) 테스트 (mock, codex/네트워크 없음).

#87/#89가 드러낸 과매칭: 하니스 탐지가 키워드("trace"·"harness") 부분문자열 매칭이라, 트레이스를
*생산하지 않는* 준비/스캐폴드 유닛이 오탐됐다. 예: snake u0(desc "trace 스크립트 *이름 준비*")가
"trace" 키워드로 하니스 탐지 → evidence-contract 부착 → u0는 트레이스를 안 냄 → 계약 못 채워 fail.

수정: 하니스 탐지를 "키워드 언급"에서 **"실제로 트레이스를 *생산*하는가"**로 정교화 —
  (PRIMARY) 유닛에 태그된 기준이 evidence_fields/scenario_steps를 들거나,
  (SECONDARY) 트레이스 키워드 매칭 *그리고* 준비/스캐폴드/네이밍 유닛이 아님.
**탐지/분류만 조정** — 적대 run-judge·gate 판정 로직·실제 하니스의 #82-B 검사 내용은 불변.
"""

from pathlib import Path

from haetae.gate import CompositeGate, contract_for_unit
from haetae.intake import (
    extract_evidence_contracts,
    harness_scenario_steps,
    harness_units,
)
from haetae.models import ProjectSpec, Verdict

REPO_ROOT = Path(__file__).resolve().parents[1]
JUDGE_PROMPT = REPO_ROOT / "prompts" / "judge.md"
RUN_JUDGE_PROMPT = REPO_ROOT / "prompts" / "run_judge.md"

_RUN_FIELDS = ["overlap_pairs", "wall_crossings"]  # 정렬된 기대 계약


def _spec(acs: list[dict], decomp: list[dict]) -> ProjectSpec:
    return ProjectSpec.model_validate({
        "spec_id": "hd-001", "version": 1, "order_raw": "x", "goal": "g",
        "task_type": "feature_impl", "verifiability": "objective", "mode": "normal",
        "acceptance_criteria": acs, "non_goals": ["n"], "done_when": "전부 통과",
        "decomposition": decomp,
    })


def _gate(tmp_path, client=None) -> CompositeGate:
    return CompositeGate(
        workdir=tmp_path, judge_client=client,
        judge_prompt_path=JUDGE_PROMPT, run_judge_prompt_path=RUN_JUDGE_PROMPT,
        run_timeout=10, install_deps=False,
    )


# 트레이스 키워드를 든 *준비/스캐폴드* 유닛(비-생산). #89 snake u0 패턴.
_PREP_DESCS = [
    "trace 스크립트 이름 준비",            # #89 snake u0 정확 패턴
    "sim:trace 스크립트 이름/구조 스캐폴드",
    "headless trace harness scaffold (naming only)",
    "트레이스 모듈 뼈대 스텁",
]
# 트레이스를 *실제로 생산*하는 진짜 하니스 유닛 desc(키워드 + 하니스 성격).
_REAL_DESCS = [
    "헤드리스 sim:trace 하니스",
    "trace 하니스",
    "sim:trace 하니스",
    "헤드리스 trace CLI로 JSON 트레이스 방출",
]


# ════════════════════ 1. 준비/스캐폴드 과매칭 제외 ════════════════════


def test_prep_units_excluded_from_harness():
    """키워드를 든 준비/스캐폴드/네이밍 유닛(run 체크 없음·증거 없음) → 하니스 탐지 안 됨."""
    spec = _spec(
        [{"id": "ac1", "desc": "동선", "check": {"type": "run", "cmd": "sim", "pass": "wall_crossings==0, overlap_pairs==0"}}],
        [{"unit": f"u{i}", "desc": d} for i, d in enumerate(_PREP_DESCS)],
    )
    assert harness_units(spec) == set()  # 전부 준비 유닛 → 하니스 0


def test_real_harness_units_still_detected():
    """진짜 하니스 유닛(키워드 + 하니스 성격, 준비어 없음) → 여전히 하니스 탐지(SECONDARY)."""
    spec = _spec(
        [{"id": "ac1", "desc": "동선", "check": {"type": "run", "cmd": "sim", "pass": "wall_crossings==0, overlap_pairs==0"}}],
        [{"unit": f"u{i}", "desc": d} for i, d in enumerate(_REAL_DESCS)],
    )
    assert harness_units(spec) == {"u0", "u1", "u2", "u3"}  # 전부 진짜 하니스


def test_prep_and_real_coexist_only_real_gets_contract():
    """준비 유닛 + 진짜 하니스 공존: 계약은 진짜 하니스에만 부착(준비 유닛 미부착 → 거짓 fail 0)."""
    spec = _spec(
        [{"id": "ac1", "desc": "동선", "check": {"type": "run", "cmd": "sim",
          "pass": "wall_crossings==0, overlap_pairs==0"}}],
        [
            {"unit": "u0", "desc": "trace 스크립트 이름 준비"},      # 준비(키워드+준비어)
            {"unit": "u1", "desc": "물리 엔진 구현"},               # 비하니스(키워드 없음)
            {"unit": "u2", "desc": "헤드리스 sim:trace 하니스"},     # 진짜 하니스
        ],
    )
    out = extract_evidence_contracts(spec)
    by = {u.unit: u.evidence_contract for u in out.decomposition}
    assert by["u2"] == _RUN_FIELDS  # 진짜 하니스 → 계약
    assert by["u0"] == []           # 준비 유닛 → 무계약(오탐 제거)
    assert by["u1"] == []           # 비하니스 → 무계약


def test_prep_unit_excluded_from_scenario_steps_too():
    """시나리오 흐름(#98)도 같은 게이트 — 준비 유닛엔 scenario_steps 미부착."""
    spec = _spec(
        [{"id": "ac1", "desc": "DnD", "check": {"type": "run", "cmd": "trace"},
          "scenario_steps": ["카드 생성", "*같은 카드*를 이동"]}],
        [
            {"unit": "u0", "desc": "trace 스크립트 이름 준비"},   # 준비
            {"unit": "u1", "desc": "헤드리스 trace 하니스"},      # 진짜 하니스
        ],
    )
    mapping = harness_scenario_steps(spec)
    assert "u0" not in mapping           # 준비 유닛 제외
    assert mapping["u1"] == ["카드 생성", "*같은 카드*를 이동"]


# ════════════════════ 2. PRIMARY 게이트 — 증거-생산이 주, 키워드는 보조 ════════════════════


def test_unit_owning_evidence_fields_is_harness_without_keyword():
    """PRIMARY: 유닛에 태그된 기준이 evidence_fields를 들면 키워드 없어도 하니스(증거-생산 확정)."""
    spec = _spec(
        [{"id": "ac1", "desc": "behavior 검증", "unit": "u1",
          "check": {"type": "run", "cmd": "node runner.js"},
          "evidence_fields": ["wall_crossings", "overlap_pairs"]}],
        [{"unit": "u1", "desc": "behavior simulation runner"}],  # 키워드 'trace' 없음
    )
    assert harness_units(spec) == {"u1"}  # 증거 부착 → 하니스(PRIMARY)


def test_unit_owning_scenario_steps_is_harness_without_keyword():
    """PRIMARY: scenario_steps(#98)를 든 유닛도 키워드 없이 하니스."""
    spec = _spec(
        [{"id": "ac1", "desc": "흐름 검증", "unit": "u1",
          "check": {"type": "run", "cmd": "node runner.js"},
          "scenario_steps": ["생성", "조작", "검증"]}],
        [{"unit": "u1", "desc": "app behavior runner"}],  # 키워드 없음
    )
    assert harness_units(spec) == {"u1"}


def test_primary_production_overrides_prep_wording():
    """증거-생산(PRIMARY)이 준비어 표현을 이긴다 — 증거 부착 유닛은 desc가 '준비'여도 하니스."""
    spec = _spec(
        [{"id": "ac1", "desc": "trace", "unit": "u1",
          "check": {"type": "run", "cmd": "node t.js"},
          "evidence_fields": ["wall_crossings"]}],
        [{"unit": "u1", "desc": "trace 스크립트 이름 준비"}],  # 준비어지만 증거를 *소유*
    )
    assert harness_units(spec) == {"u1"}  # 증거-생산 확정 → 하니스


def test_bare_keyword_alone_insufficient_for_prep():
    """키워드 단독은 불충분 — 준비 유닛(키워드만, 증거 미소유)은 하니스 아님."""
    spec = _spec(
        [{"id": "ac1", "desc": "x", "check": {"type": "run", "cmd": "sim", "pass": "wall_crossings==0"}}],
        [{"unit": "u1", "desc": "trace 스크립트 이름 준비"}],  # 키워드만, 증거 미소유
    )
    assert harness_units(spec) == set()


# ════════════════════ 3. gate self-check 트리거(#82-B) — 준비 유닛 미트리거 ════════════════════


def test_gate_no_self_check_for_prep_unit(tmp_path):
    """준비 유닛(계약 미부착) → gate is_harness=False → #82-B self-check 미실행(거짓 fail 0)."""
    spec = _spec(
        [{"id": "ac1", "desc": "동선", "check": {"type": "run", "cmd": "sim",
          "pass": "wall_crossings==0, overlap_pairs==0"}}],
        [{"unit": "u0", "desc": "trace 스크립트 이름 준비"}],
    )
    spec = extract_evidence_contracts(spec)
    assert contract_for_unit(spec, "u0") == []  # 계약 없음
    gr = _gate(tmp_path, client=None).judge("결과", spec, unit="u0")
    # self-check(harness-run/harness-evidence-contract) 미추가 → executor-ok pass(거짓 fail 0).
    assert not [c for c in gr.checks if c.ac_id.startswith("(harness-")]
    assert gr.verdict is Verdict.pass_


def test_gate_self_check_still_runs_for_real_harness(tmp_path):
    """진짜 하니스(계약 부착) → #82-B self-check *여전히* 실행(검증 깊이 유지).

    WO#108-A: per-unit은 *대표 시나리오 구조 smoke*(≥1 계약 어휘 + 깨끗한 JSON)로 right-size됨 —
    계약 어휘를 *하나도* 안 내는 트레이스(다른 필드로 대체)는 여전히 per-unit fail(잘못된 증거 차단).
    전-필드 union 강제는 통합 게이트가 한다(test_loop_robustness).
    """
    spec = _spec(
        [{"id": "ac1", "desc": "동선 trace", "unit": "u1",
          "check": {"type": "run", "cmd": "echo '{\"my_own_count\":42}'",  # 계약 어휘 0개
                    "pass": "wall_crossings==0, overlap_pairs==0"}}],
        [{"unit": "u1", "desc": "헤드리스 sim:trace 하니스"}],
    )
    spec = extract_evidence_contracts(spec)
    assert contract_for_unit(spec, "u1") == _RUN_FIELDS  # 계약 부착됨(깊이 유지)
    gr = _gate(tmp_path, client=None).judge("결과", spec, unit="u1")
    ec = [c for c in gr.checks if c.ac_id == "(harness-evidence-contract)"]
    assert ec and ec[0].status == "fail"  # 계약 어휘 0개 → 구조 smoke fail(잘못된 증거 차단)
    assert gr.verdict is Verdict.fail_recoverable


# ════════════════════ 4. back-compat / 분리 ════════════════════


def test_back_compat_existing_harness_descs_detected():
    """무회귀: 기존 하니스 desc 패턴(md/snake/crowd-sim)이 모두 탐지 유지."""
    spec = _spec(
        [{"id": "ac1", "desc": "x", "check": {"type": "run", "cmd": "sim",
          "pass": "wall_crossings==0, overlap_pairs==0"}}],
        [{"unit": f"u{i}", "desc": d} for i, d in enumerate(_REAL_DESCS)],
    )
    out = extract_evidence_contracts(spec)
    for u in out.decomposition:
        assert u.evidence_contract == _RUN_FIELDS  # 전부 계약 부착(기존 동작)


def test_detection_change_does_not_alter_bar():
    """탐지 분류만 — 계약은 criteria서 파생(바 불변), done_when/goal/criteria 무변경."""
    spec = _spec(
        [{"id": "ac1", "desc": "동선", "check": {"type": "run", "cmd": "sim",
          "pass": "wall_crossings==0, overlap_pairs==0"}}],
        [{"unit": "u0", "desc": "trace 스크립트 이름 준비"},
         {"unit": "u1", "desc": "헤드리스 sim:trace 하니스"}],
    )
    out = extract_evidence_contracts(spec)
    assert out.acceptance_criteria == spec.acceptance_criteria  # 바 불변
    assert out.done_when == spec.done_when and out.goal == spec.goal


def test_harness_units_pure_no_mutation():
    """harness_units는 순수 — spec을 안 바꾼다(분류만)."""
    spec = _spec(
        [{"id": "ac1", "desc": "x", "check": {"type": "run", "cmd": "sim", "pass": "wall_crossings==0"}}],
        [{"unit": "u1", "desc": "헤드리스 sim:trace 하니스"}],
    )
    before = spec.model_dump()
    harness_units(spec)
    assert spec.model_dump() == before
