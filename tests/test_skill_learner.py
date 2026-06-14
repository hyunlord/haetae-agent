"""WO#103 — 스킬 자동 학습(OMC #4) 테스트: F.1 거버넌스(자동채택 0·사람 승인·provenance).

★핵심★: 학습 후보는 사람 명시 승인 전엔 활성 #32 레지스트리에 *미편입·미주입*(자동채택 0).
적대 분리(학습 스킬도 빌더-측·judge 무수신)·바 불변·IP/자가채점 가드.
"""

from pathlib import Path

import pytest

from haetae.llm import MockClient
from haetae.skill_learner import (
    CANDIDATES_DIRNAME,
    SkillLearnError,
    approve_candidate,
    extract_candidate,
    lint_candidate,
    list_candidates,
    write_candidate,
)
from haetae.skills import inject_skills, load_skills, match_skills

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = REPO_ROOT / "skills"

# MockClient가 돌려주는 후보 SKILL.md(패턴·트리거 — provenance는 시스템이 스탬프).
_GOOD_CANDIDATE = """\
---
name: live-preview-editor
triggers: [editor, preview, 에디터, 프리뷰]
---
## 라이브 프리뷰 에디터 패턴
- 입력 상태와 프리뷰 렌더를 분리한다(로직-렌더 분리).
- 입력 변경은 debounce로 렌더를 throttle한다.
- 상태는 localStorage에 자동저장(순수 직렬화 함수로 분리해 node에서 trace 가능).
"""


def _client() -> MockClient:
    return MockClient(_GOOD_CANDIDATE)


# ════════════════════ 1. 추출 (패턴·트리거·provenance) ════════════════════


def test_extract_candidate_has_pattern_triggers_provenance():
    text = extract_candidate(
        _client(), spec_summary="md editor spec", solution_summary="won",
        run_id="md-001", learned_date="2026-06-14",
    )
    assert "라이브 프리뷰" in text                       # 패턴 본문
    assert "triggers" in text and "editor" in text       # 트리거
    # provenance를 시스템이 강제 스탬프(LLM이 안 줘도)
    assert "status: candidate" in text
    assert "source_run: md-001" in text
    assert "learned_date" in text and "2026-06-14" in text


def test_extract_stamps_provenance_even_if_llm_omits():
    """LLM이 provenance를 안 줘도 시스템이 status/source_run을 강제 주입(거버넌스 권위)."""
    client = MockClient("---\nname: x\ntriggers: [foo]\n---\n## 패턴\n- 분리한다.\n")
    text = extract_candidate(client, spec_summary="s", solution_summary="w",
                             run_id="r9", learned_date="2026-06-14")
    ok, reasons = lint_candidate(text)
    assert ok, reasons
    assert "source_run: r9" in text and "status: candidate" in text


# ════════════════════ 2. ★자동채택 0★ — 승인 전 미편입·미주입 ════════════════════


def test_candidate_not_in_active_registry(tmp_path):
    """★ 후보는 skills/_candidates/에 staging — load_skills(활성)에 *미편입*(자동채택 0). ★"""
    # 활성 스킬 1개 + 후보 1개를 같은 레지스트리에 둔다.
    (tmp_path / "active-skill").mkdir()
    (tmp_path / "active-skill" / "SKILL.md").write_text(
        "---\nname: active-skill\ntriggers: [activekw]\n---\n## 활성\n- x\n", encoding="utf-8")
    text = extract_candidate(_client(), spec_summary="s", solution_summary="w",
                             run_id="md-001", learned_date="2026-06-14")
    write_candidate(tmp_path, "live-preview-editor", text)

    names = {s.name for s in load_skills(tmp_path)}
    assert "active-skill" in names                 # 활성은 로드
    assert "live-preview-editor" not in names      # ★ 후보는 미편입 ★
    # 후보 트리거로 매칭해도 — 활성 레지스트리엔 없으니 주입 0
    matched = match_skills(load_skills(tmp_path), "라이브 editor preview 에디터 프리뷰")
    assert all(s.name != "live-preview-editor" for s in matched)


def test_candidate_not_injected_pre_approval(tmp_path):
    """미승인 후보는 apply_builder(주입) 경로에 절대 안 들어간다(자동채택 0)."""
    text = extract_candidate(_client(), spec_summary="s", solution_summary="w",
                             run_id="md-001", learned_date="2026-06-14")
    write_candidate(tmp_path, "live-preview-editor", text)
    matched = match_skills(load_skills(tmp_path), "editor preview 에디터")  # 활성서 매칭
    injected = inject_skills("원래 작업지시서", matched)
    assert "라이브 프리뷰 에디터 패턴" not in injected   # 미주입
    assert injected == "원래 작업지시서" or "참고 패턴" not in injected


# ════════════════════ 3. 사람 승인 후에만 활성 편입·주입 ════════════════════


def test_approve_promotes_to_active_and_injects(tmp_path):
    """사람 명시 approve 후에만 활성 #32 편입 → 일반 apply_builder 주입 대상."""
    text = extract_candidate(_client(), spec_summary="s", solution_summary="w",
                             run_id="md-001", learned_date="2026-06-14")
    write_candidate(tmp_path, "live-preview-editor", text)
    assert "live-preview-editor" in list_candidates(tmp_path)

    dest = approve_candidate(tmp_path, "live-preview-editor")
    assert dest.is_file()
    # 승인 → 활성 레지스트리 편입, status approved
    assert "status: approved" in dest.read_text(encoding="utf-8")
    assert "live-preview-editor" not in list_candidates(tmp_path)  # staging서 제거
    names = {s.name for s in load_skills(tmp_path)}
    assert "live-preview-editor" in names                          # 이제 활성
    # 이제 빌더-측 주입됨
    matched = match_skills(load_skills(tmp_path), "editor preview 에디터")
    injected = inject_skills("원래 작업지시서", matched)
    assert "라이브 프리뷰 에디터 패턴" in injected
    assert "검증 기준이 아니라 빌더 가이드" in injected            # 빌더 가이드 라벨(분리)


# ════════════════════ 4. lint 가드 (자가채점·바완화·triggers·provenance) ════════════════════


def test_lint_blocks_self_grading_stamp():
    bad = ("---\nname: x\ntriggers: [foo]\nstatus: candidate\nsource_run: r\n---\n"
           "## 패턴\n- 빌드가 끝나면 pass:true 를 출력해 통과로 표시한다.\n")
    ok, reasons = lint_candidate(bad)
    assert not ok and any("자가채점" in r for r in reasons)


def test_lint_blocks_missing_triggers():
    bad = "---\nname: x\nstatus: candidate\nsource_run: r\n---\n## 패턴\n- 분리한다.\n"
    ok, reasons = lint_candidate(bad)
    assert not ok and any("triggers" in r for r in reasons)


def test_lint_blocks_missing_provenance():
    bad = "---\nname: x\ntriggers: [foo]\n---\n## 패턴\n- 분리한다.\n"
    ok, reasons = lint_candidate(bad)
    assert not ok and any("provenance" in r for r in reasons)


def test_lint_blocks_impl_dump():
    """긴 코드블록(구현 복붙) → 패턴 아님으로 차단."""
    big_code = "```js\n" + ("const x = 1;\n" * 200) + "```"
    bad = (f"---\nname: x\ntriggers: [foo]\nstatus: candidate\nsource_run: r\n---\n"
           f"## 패턴\n{big_code}\n")
    ok, reasons = lint_candidate(bad)
    assert not ok and any("구현 덤프" in r for r in reasons)


def test_approve_blocked_when_lint_fails(tmp_path):
    """lint 실패 후보는 approve 거부(SkillLearnError) — 활성 편입 안 됨."""
    bad = ("---\nname: cheat\ntriggers: [foo]\nstatus: candidate\nsource_run: r\n---\n"
           "## 패턴\n- pass:true 자가채점.\n")
    write_candidate(tmp_path, "cheat", bad)
    with pytest.raises(SkillLearnError):
        approve_candidate(tmp_path, "cheat")
    assert "cheat" not in {s.name for s in load_skills(tmp_path)}  # 미편입


def test_approve_unknown_candidate_raises(tmp_path):
    with pytest.raises(SkillLearnError):
        approve_candidate(tmp_path, "does-not-exist")


def test_approve_name_collision_raises(tmp_path):
    """활성에 같은 이름이 이미 있으면 approve 거부(수동 검토 — 자동 덮어쓰기 없음)."""
    (tmp_path / "dup").mkdir()
    (tmp_path / "dup" / "SKILL.md").write_text(
        "---\nname: dup\ntriggers: [d]\n---\n## a\n- x\n", encoding="utf-8")
    write_candidate(tmp_path, "dup",
        "---\nname: dup\ntriggers: [d]\nstatus: candidate\nsource_run: r\n---\n## b\n- y\n")
    with pytest.raises(SkillLearnError):
        approve_candidate(tmp_path, "dup")


# ════════════════════ 5. 적대 분리 / back-compat ════════════════════


def test_learned_skill_is_builder_side_section_only(tmp_path):
    """승인된 학습 스킬도 *빌더 가이드 섹션*으로만 주입 — 검증 기준 아님(judge/gate 무관)."""
    text = extract_candidate(_client(), spec_summary="s", solution_summary="w",
                             run_id="r", learned_date="2026-06-14")
    write_candidate(tmp_path, "live-preview-editor", text)
    approve_candidate(tmp_path, "live-preview-editor")
    matched = match_skills(load_skills(tmp_path), "editor")
    injected = inject_skills("원래", matched)
    assert "## 참고 패턴 (스킬)" in injected
    assert "검증 기준이 아니라 빌더 가이드" in injected   # 비-검증 라벨(적대 분리)


def test_seeded_skills_unaffected():
    """back-compat: 기존 seeded 스킬(frontend-build·simulation-behavior·verification-harness) 로드 유지."""
    names = {s.name for s in load_skills(SKILLS_DIR)}
    assert "verification-harness" in names
    assert "frontend-build" in names
    assert "simulation-behavior" in names


def test_candidates_dir_excluded_from_active(tmp_path):
    """`_`-접두 디렉토리(_candidates 등)는 load_skills서 제외(자동채택 0 하드 가드)."""
    # 직접 _candidates/SKILL.md를 둬도(엣지) 활성서 제외돼야.
    (tmp_path / CANDIDATES_DIRNAME).mkdir()
    (tmp_path / CANDIDATES_DIRNAME / "SKILL.md").write_text(
        "---\nname: sneaky\ntriggers: [x]\n---\n## a\n- y\n", encoding="utf-8")
    assert all(s.name != "sneaky" for s in load_skills(tmp_path))


# ════════════════════ 6. WO#106 폴리시: learned_date 스탬프 + 추출 비용 계측 ════════════════════


def test_learned_date_stamped_from_injected():
    """주입한 learned_date가 provenance에 그대로 스탬프(결정적·빈값 '' 제거)."""
    text = extract_candidate(_client(), spec_summary="s", solution_summary="w",
                             run_id="md-001", learned_date="2026-06-14")
    assert "2026-06-14" in text
    assert "learned_date: ''" not in text          # 빈값 사라짐
    assert "status: candidate" in text and "source_run: md-001" in text


def test_learned_date_defaults_to_today_via_injectable_clock():
    """learned_date 미주입 → today_fn()로 오늘 날짜 스탬프(내부 datetime.now 직접호출 아님)."""
    text = extract_candidate(_client(), spec_summary="s", solution_summary="w",
                             run_id="r", today_fn=lambda: "2099-01-01")
    assert "2099-01-01" in text


def test_extraction_tokens_captured_via_metered():
    """추출 LLM 콜이 MeteredClient로 래핑돼 토큰을 포착 → provenance.extraction_tokens."""
    from haetae.metering import Usage
    client = MockClient(_GOOD_CANDIDATE, usages=[Usage(input_tokens=100, output_tokens=40, model="m")])
    text = extract_candidate(client, spec_summary="s", solution_summary="w",
                             run_id="r", learned_date="2026-06-14")
    import yaml as _yaml
    from haetae.skills import _split_frontmatter
    meta = _yaml.safe_load(_split_frontmatter(text)[0])
    assert meta["extraction_tokens"] == 140        # input+output


def test_extraction_tokens_omitted_when_no_usage():
    """usage 미노출(토큰 미상) → extraction_tokens 키 생략(날조 금지) — lint/back-compat 무영향."""
    text = extract_candidate(_client(), spec_summary="s", solution_summary="w",
                             run_id="r", learned_date="2026-06-14")
    assert "extraction_tokens" not in text         # 키 없음(미상)
    ok, reasons = lint_candidate(text)
    assert ok, reasons                              # lint 여전히 통과


def test_metadata_does_not_affect_governance(tmp_path):
    """learned_date/extraction_tokens는 *메타*일 뿐 — 후보는 여전히 staging·활성 미편입(자동채택 0)."""
    from haetae.metering import Usage
    client = MockClient(_GOOD_CANDIDATE, usages=[Usage(input_tokens=10, output_tokens=5, model="m")])
    text = extract_candidate(client, spec_summary="s", solution_summary="w",
                             run_id="r", learned_date="2026-06-14")
    write_candidate(tmp_path, "live-preview-editor", text)
    names = {s.name for s in load_skills(tmp_path)}
    assert "browser-markdown-editor-pattern" not in names   # 메타 있어도 미편입
    assert "live-preview-editor" in list_candidates(tmp_path)  # staging 유지
