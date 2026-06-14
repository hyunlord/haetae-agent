"""WO#84 — 하니스 종류 유도(가벼운 node 트레이스, 실브라우저 E2E 회피) 테스트 (mock).

#83 핀포인트: 빌더가 실/헤드리스 브라우저 하니스(e2e:trace·trace:browser)를 골라 게이트
오프라인 환경서 exit 1 반복 → 하니스가 전체 비용 66~89% 태우고 미수렴(검증 역전). md-editor만
가벼운 node 트레이스로 완주. 수정: (1) 합성기 프롬프트 유도 + (2) #32 verification-harness 스킬.
**빌더-측 유도만** — 게이트(#82-B)·적대 run-judge·바 불변.
"""

from pathlib import Path

from haetae.skills import inject_skills, load_skills, match_skills

REPO_ROOT = Path(__file__).resolve().parents[1]
SYNTH_PROMPT = REPO_ROOT / "prompts" / "synthesizer.md"
SKILLS_DIR = REPO_ROOT / "skills"


# ════════════════════ 1. 합성기 프롬프트 유도 ════════════════════


def test_synthesizer_steers_harness_to_node_trace():
    """합성기가 하니스를 *node 실행 가능한 가벼운 트레이스*로 유도(실브라우저 회피 명시)."""
    src = SYNTH_PROMPT.read_text(encoding="utf-8")
    # node로 실행 가능한 헤드리스 트레이스 유도
    assert "node로 실행 가능한" in src
    assert "import" in src and ("순수 JS" in src or "JSDOM" in src)
    # 실브라우저 E2E 금지 명시
    assert "실브라우저 E2E 금지" in src or "실브라우저 E2E" in src
    for forbidden in ("playwright", "puppeteer", "chromium"):
        assert forbidden in src, f"브라우저 바이너리 {forbidden} 회피 명시돼야"
    # 게이트 오프라인 환경 근거
    assert "오프라인" in src


def test_synthesizer_keeps_evidence_fields_and_bar():
    """무회귀: node 유도가 #82 evidence_fields 지시·바(run 기준 존재 이유)를 깨지 않는다."""
    src = SYNTH_PROMPT.read_text(encoding="utf-8")
    assert "evidence_fields" in src                       # #82-A 보존
    assert "run-judge가 한다" in src or "run-judge가 그런" in src  # 채점=독립 게이트(바 불변)


# ════════════════════ WO#86: stdout JSON 위생 유도 ════════════════════
# #85 핀포인트: node 하니스가 exit 0으로 깨끗이 *실행*되나 stdout에 노이즈(npm 배너·console.log)가
# 섞여 게이트의 JSON.parse 계약 체크가 실패(미수렴). md-editor는 깨끗한 JSON-only stdout으로 통과.


def test_synthesizer_steers_stdout_json_hygiene():
    """합성기가 하니스에 stdout=단일 JSON only·stderr=로그·배너 억제를 유도한다."""
    src = SYNTH_PROMPT.read_text(encoding="utf-8")
    assert "stdout 위생" in src
    assert "단일 유효 JSON" in src or "단일 JSON" in src
    assert "JSON.parse" in src                              # 게이트가 stdout을 parse
    assert "stderr" in src                                  # 로그는 stderr로
    assert "--silent" in src                                # npm 배너 억제
    assert "node" in src and "tsx" in src                   # 직접 node 실행 대안


def test_skill_teaches_stdout_json_hygiene():
    """verification-harness 스킬에 stdout-위생 패턴(JSON only·stderr 로그·배너 억제)이 있다."""
    skills = {s.name: s for s in load_skills(SKILLS_DIR)}
    body = skills["verification-harness"].body
    assert "stdout 위생" in body
    assert "JSON.parse" in body
    assert "console.error" in body or "process.stderr" in body  # 로그는 stderr
    assert "--silent" in body                                   # npm 배너 억제
    assert "단일 JSON" in body or "stdout = 단일 JSON" in body


# ════════════════════ 2. #32 verification-harness 스킬 ════════════════════


def test_verification_harness_skill_loads():
    """node-트레이스 패턴 스킬이 레지스트리에 로드된다."""
    names = {s.name for s in load_skills(SKILLS_DIR)}
    assert "verification-harness" in names


def test_skill_matches_behavior_harness_units():
    """행동 검증 하니스 유닛(trace/headless/e2e/browser)에 매칭된다 — #83 실패 work order 포함."""
    skills = load_skills(SKILLS_DIR)
    for wo in [
        "앱 화면 통합과 헤드리스 E2E 트레이스 진입점 구현",                # kanban u6
        "헤드리스 브라우저에서 실제 앱을 실행하고 JSON 트레이스를 방출",      # snake u4
        "라이브 프리뷰·레이아웃을 검증하는 헤드리스 트레이스",               # md u6
        "playwright e2e harness for the app",
    ]:
        matched = [s.name for s in match_skills(skills, wo)]
        assert "verification-harness" in matched, f"매칭 실패: {wo}"


def test_skill_does_not_match_non_harness_builder_unit():
    """과매칭 금지: 비하니스 빌더 유닛(엔진·UI 렌더)엔 안 붙는다."""
    skills = load_skills(SKILLS_DIR)
    for wo in [
        "연속 좌표 매장 layout과 벽 geometry 충돌 판정 강화",   # 엔진 유닛
        "계산대 FIFO 큐 서비스 시간 결제 완료 흐름 구현",       # 로직 유닛
    ]:
        matched = [s.name for s in match_skills(skills, wo)]
        assert "verification-harness" not in matched, f"과매칭: {wo}"


def test_skill_body_teaches_node_trace_and_browser_avoidance():
    """스킬 본문이 핵심 패턴(node 실행·로직-렌더 분리·브라우저 회피·JSON 증거)을 담는다."""
    skills = {s.name: s for s in load_skills(SKILLS_DIR)}
    body = skills["verification-harness"].body
    assert "node" in body
    assert "로직-렌더 분리" in body or "분리" in body
    for forbidden in ("playwright", "puppeteer", "chromium"):
        assert forbidden in body
    assert "JSDOM" in body                        # 브라우저 API 대체
    assert "evidence_fields" in body or "구조화 JSON" in body


# ════════════════════ 분리 / 빌더-측 전용 ════════════════════


def test_skill_injection_is_builder_side_section_only():
    """스킬 주입은 빌더 가이드 섹션으로만 — 검증 기준이 아님(분리; judge/gate 무관)."""
    skills = load_skills(SKILLS_DIR)
    matched = match_skills(skills, "헤드리스 트레이스 하니스")
    injected = inject_skills("원래 작업지시서", matched)
    assert "## 참고 패턴 (스킬)" in injected            # 빌더 가이드 섹션
    assert "검증 기준이 아니라 빌더 가이드" in injected   # 명시적 비-검증 라벨
    assert "원래 작업지시서" in injected                # 원본 보존


def test_skill_does_not_lower_bar():
    """바 불변: 스킬은 *하니스를 게이트서 돌게* 도울 뿐, 행동 검증을 약화하지 않는다."""
    skills = {s.name: s for s in load_skills(SKILLS_DIR)}
    body = skills["verification-harness"].body
    # 행동 판정은 여전히 독립 run-judge(자가채점 금지) — 바 약화 아님
    assert "run-judge" in body
    assert "자가채점" in body and ("금지" in body or "채점은 게이트 몫" in body)


# ════════════════════ WO#114: 밀도 시나리오 커버리지 유도 (#98 확장·#112 교훈) ════════════════════
# #112 핀포인트: crowd-sim이 *저밀도*(동시 ~12체, overlap onset ~15 바로 아래)서만 돌아 overlap=0이
# 저밀도 아티팩트였다 → 적대 run-judge가 분리 붕괴를 못 잡음. 시나리오 커버리지 = gate 엄밀성의 상한.
# 수정: sim/crowd 류 scenario_steps가 현실/스트레스 밀도를 구동하도록 (1) 합성기 프롬프트 + (2) 스킬.
# **빌더-측 유도만**, *강화*(더 어려운 조건 요구)이지 바 완화 아님 — 비-sim 기준 무영향.


def test_synthesizer_steers_sim_density_coverage():
    """합성기가 sim/crowd 류 scenario_steps에 *현실/혼잡 밀도* 구동을 유도(저밀도 happy-path 금지)."""
    src = SYNTH_PROMPT.read_text(encoding="utf-8")
    assert "밀도 커버리지" in src
    assert "저밀도 happy-path 금지" in src
    assert "혼잡" in src and "큐" in src                  # 큐/경합이 실제 형성될 밀도
    assert "overlap" in src and ("min_separation" in src or "최소 분리" in src or "최소분리" in src)
    assert "#112" in src                                  # 근거 명시
    # 스코프: sim/crowd/agent 류로 한정(전체 기준 강제 아님 — 비-sim 무영향)
    assert "sim" in src and ("crowd" in src or "navigation" in src or "에이전트" in src)


def test_density_steering_is_strict_not_lenient():
    """밀도 유도는 *강화*(더 어려운 조건)이지 완화 아님 — 판정은 여전히 run-judge(바 불변)."""
    src = SYNTH_PROMPT.read_text(encoding="utf-8")
    assert "완화가 아니라 강화" in src or ("완화" in src and "강화" in src)
    assert "run-judge가 한다" in src                      # 행동 판정은 여전히 독립 게이트
    # 비-sim 무영향 명시
    assert "비-sim" in src or "비-sim 기준엔 무관" in src


def test_skill_teaches_density_coverage():
    """verification-harness 스킬에 '밀도 커버리지' 섹션(혼잡 밀도 구동·밀도-하 overlap/separation 측정)."""
    skills = {s.name: s for s in load_skills(SKILLS_DIR)}
    body = skills["verification-harness"].body
    assert "밀도 커버리지" in body
    assert "저밀도 happy-path" in body
    assert "overlap" in body and ("min_separation" in body or "분리" in body)
    assert "#112" in body
    # 강화이지 완화 아님 — 판정은 run-judge(자가채점 금지)
    assert "강화" in body and "run-judge" in body


# ════════════════════ WO#88: test·빌드 cmd 위생 유도 ════════════════════
# #87 핀포인트: vitest 스캐폴드에 Jest 전용 플래그(--runInBand)를 붙여 러너 크래시 → 미수렴.


def test_synthesizer_steers_test_cmd_hygiene():
    """합성기가 test/빌드 cmd를 스캐폴드 러너에 맞추고 임의 플래그를 금지하도록 유도한다."""
    src = SYNTH_PROMPT.read_text(encoding="utf-8")
    # 러너-특정 플래그 금지 명시
    assert "플래그를 임의로 더하지 마라" in src
    # 실제 크래시 플래그 사례 언급
    assert "--runInBand" in src
    # 스캐폴드 러너 참조 (vitest)
    assert "vitest" in src
    # package.json devDeps 확인 지시
    assert "devDeps" in src


def test_skill_teaches_test_cmd_hygiene():
    """verification-harness 스킬에 test·빌드 cmd 위생(스택-맞는 러너, 임의 플래그 금지)이 있다."""
    skills = {s.name: s for s in load_skills(SKILLS_DIR)}
    body = skills["verification-harness"].body
    # 섹션 헤더
    assert "test·빌드 cmd 위생" in body
    # 실제 크래시 플래그 사례 언급
    assert "--runInBand" in body
    # 스캐폴드 러너 참조 (vitest)
    assert "vitest" in body
