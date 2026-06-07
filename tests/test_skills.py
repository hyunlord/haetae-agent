"""스킬 레지스트리 + dispatch 주입 테스트 (WO#32) — mock/픽스처만(네트워크 없음).

커버: load_skills(파싱·best-effort) · match_skills(대소문자무시·캡) · inject_skills(섹션·캡)
     · dispatch 주입(순차·병렬) · **분리 가드**(judge/gate에 안 샘) · 시드 스킬 2개.
"""

import threading
from pathlib import Path

from haetae.llm import MockClient
from haetae.loop import MockExecutor, MockGate, run_loop
from haetae.models import GateResult, Status, Verdict
from haetae.skills import (
    SKILL_SECTION_HEADER,
    Skill,
    inject_skills,
    load_skills,
    match_skills,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPT_DIR = REPO_ROOT / "prompts"
SEED_SKILLS_DIR = REPO_ROOT / "skills"


def _write_skill(base: Path, name: str, triggers, body: str = "패턴 본문") -> None:
    sk = base / name
    sk.mkdir(parents=True, exist_ok=True)
    trig = ", ".join(triggers)
    (sk / "SKILL.md").write_text(
        f"---\ntriggers: [{trig}]\n---\n\n{body}\n", encoding="utf-8"
    )


# ──────────────────────────── load_skills ────────────────────────────


def test_load_skills_parses_triggers_and_body(tmp_path):
    _write_skill(tmp_path, "alpha", ["React", "vite"], body="# Alpha\n쓸 패턴")
    skills = load_skills(tmp_path)
    assert len(skills) == 1
    assert skills[0].name == "alpha"
    assert skills[0].triggers == ("react", "vite")  # 소문자 정규화
    assert "쓸 패턴" in skills[0].body


def test_load_skills_missing_dir_returns_empty():
    assert load_skills("/nonexistent/path/xyz-haetae") == []  # raise 없음


def test_load_skills_skips_broken_and_triggerless(tmp_path):
    _write_skill(tmp_path, "good", ["react"])
    nofm = tmp_path / "nofm"
    nofm.mkdir()
    (nofm / "SKILL.md").write_text("frontmatter 없음", encoding="utf-8")
    notrig = tmp_path / "notrig"
    notrig.mkdir()
    (notrig / "SKILL.md").write_text("---\nname: x\n---\n본문", encoding="utf-8")
    skills = load_skills(tmp_path)
    assert {s.name for s in skills} == {"good"}  # 깨진/무트리거는 스킵, raise 없음


def test_load_skills_deterministic_order(tmp_path):
    _write_skill(tmp_path, "b-skill", ["x"])
    _write_skill(tmp_path, "a-skill", ["x"])
    skills = load_skills(tmp_path)
    assert [s.name for s in skills] == ["a-skill", "b-skill"]  # 디렉토리명 정렬


# ──────────────────────────── match_skills ────────────────────────────


def test_match_skills_case_insensitive():
    sk = Skill("a", ("react", "canvas"), "b")
    assert match_skills([sk], "Build a REACT dashboard") == [sk]
    assert match_skills([sk], "a Canvas thing") == [sk]


def test_match_skills_no_match_returns_empty():
    sk = Skill("a", ("react",), "b")
    assert match_skills([sk], "rust cli tool") == []


def test_match_skills_caps_to_max():
    sks = [Skill(f"s{i}", ("x",), "b") for i in range(5)]
    assert len(match_skills(sks, "x x x", max_skills=2)) == 2


# ──────────────────────────── inject_skills ────────────────────────────


def test_inject_skills_appends_section():
    sk = Skill("alpha", ("x",), "패턴 내용")
    out = inject_skills("원래 작업지시", [sk])
    assert "원래 작업지시" in out
    assert SKILL_SECTION_HEADER in out
    assert "### alpha" in out
    assert "패턴 내용" in out


def test_inject_skills_no_match_returns_original():
    assert inject_skills("원본 그대로", []) == "원본 그대로"


def test_inject_skills_respects_body_cap():
    sk = Skill("big", ("x",), "a" * 10000)
    out = inject_skills("base", [sk], max_body_chars=100)
    assert "생략" in out
    assert len(out) < 1000


# ──────────────────────────── 시드 스킬 2개 ────────────────────────────


def test_seed_skills_load_and_match_as_intended():
    skills = load_skills(SEED_SKILLS_DIR)
    names = {s.name for s in skills}
    assert "frontend-build" in names
    assert "simulation-behavior" in names
    fe = match_skills(skills, "build a react canvas dashboard")
    assert any(s.name == "frontend-build" for s in fe)
    sim = match_skills(skills, "crowd simulation with queue and spawn logic")
    assert any(s.name == "simulation-behavior" for s in sim)


# ──────────────────── dispatch 주입 (순차) ────────────────────

SPEC_YAML = """\
spec_id: skill-001
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
non_goals: ["n1", "n2"]
done_when: "ac1"
decomposition:
  - { unit: u1, desc: a, deps: [] }
open_questions: []
"""


def _next_order(goal: str) -> str:
    return f"""\
verdict: pass
action: next_order
rationale: "build"
next_order:
  unit: u1
  goal: "{goal}"
  deliverable: "요약"
"""


def test_dispatch_injects_matched_skill_into_executor_order(tmp_path):
    _write_skill(tmp_path, "frontend-build", ["react"], body="vitest를 써라")
    client = MockClient([SPEC_YAML, _next_order("react 컴포넌트 구현")])
    executor = MockExecutor(["u1 done"])
    gate = MockGate([Verdict.done])
    state = run_loop(
        "x", client, executor=executor, gate=gate,
        prompt_dir=PROMPT_DIR, skills_dir=tmp_path,
    )
    assert state.status is Status.done
    injected = executor.calls[0].scope or ""
    assert SKILL_SECTION_HEADER in injected
    assert "vitest를 써라" in injected


def test_no_skills_dir_means_no_injection():
    client = MockClient([SPEC_YAML, _next_order("react 구현")])
    executor = MockExecutor(["u1 done"])
    gate = MockGate([Verdict.done])
    run_loop("x", client, executor=executor, gate=gate,
             prompt_dir=PROMPT_DIR, skills_dir=None)
    assert SKILL_SECTION_HEADER not in (executor.calls[0].scope or "")


def test_unmatched_skill_not_injected(tmp_path):
    _write_skill(tmp_path, "rust-skill", ["rust"], body="cargo")
    client = MockClient([SPEC_YAML, _next_order("react 구현")])  # rust 트리거 없음
    executor = MockExecutor(["u1 done"])
    gate = MockGate([Verdict.done])
    run_loop("x", client, executor=executor, gate=gate,
             prompt_dir=PROMPT_DIR, skills_dir=tmp_path)
    assert SKILL_SECTION_HEADER not in (executor.calls[0].scope or "")


# ──────────────────── 분리 가드: judge/gate에 안 샘 ────────────────────


def test_skill_not_leaked_to_gate_or_events(tmp_path):
    """스킬은 빌더 전용 — gate가 보는 입력·감사 이벤트 어디에도 스킬이 새지 않는다."""
    marker = "SKILL_MARKER_XYZ"
    _write_skill(tmp_path, "frontend-build", ["react"], body=marker)
    client = MockClient([SPEC_YAML, _next_order("react 구현")])
    executor = MockExecutor(["순수 executor 결과 텍스트"])
    gate = MockGate([Verdict.done])
    state = run_loop(
        "x", client, executor=executor, gate=gate,
        prompt_dir=PROMPT_DIR, skills_dir=tmp_path,
    )
    # gate가 본 모든 result에 스킬 마커/섹션 없음
    for seen in gate.calls:
        assert marker not in seen
        assert SKILL_SECTION_HEADER not in seen
    # 감사 이벤트(work_order_ref=goal, result=executor 출력)에도 스킬 없음
    for ev in state.events:
        assert marker not in (ev.work_order_ref or "")
        assert marker not in (ev.result or "")
    # executor에는 실제로 주입됐음(가드가 "주입 자체를 끈 것"이 아님을 확인)
    assert marker in (executor.calls[0].scope or "")


# ──────────────────── dispatch 주입 (병렬) ────────────────────

SPEC_PAR = """\
spec_id: skill-par-001
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
non_goals: ["n1", "n2"]
done_when: "ac1"
decomposition:
  - { unit: u1, desc: a, deps: [] }
open_questions: []
"""

DEC_REACT = """\
verdict: pass
action: next_order
rationale: "build"
next_order:
  unit: placeholder
  goal: "react 구현"
  deliverable: "요약"
"""


class _BrainClient:
    """call#1=synthesize(spec) / 이후=replan(dec). 병렬 harness용(main 스레드 직렬)."""

    def __init__(self, spec_yaml: str, dec_yaml: str):
        self.spec = spec_yaml
        self.dec = dec_yaml
        self.n = 0

    def complete(self, system: str, user: str, **opts) -> str:
        self.n += 1
        return self.spec if self.n == 1 else self.dec


class _PassGate:
    def __init__(self):
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def judge(self, result, spec, unit=None):
        with self._lock:
            self.calls.append(result)
        return GateResult(verdict=Verdict.pass_)


def test_parallel_dispatch_injects_skill_and_guards_gate(tmp_path):
    marker = "PARALLEL_SKILL_MARKER"
    skills_dir = tmp_path / "skills"
    _write_skill(skills_dir, "frontend-build", ["react"], body=marker)
    workdir = tmp_path / "repo"
    workdir.mkdir()

    seen_scopes: list[str] = []
    lock = threading.Lock()

    def make_ex(wt):
        class E:
            def run(self, order):
                with lock:
                    seen_scopes.append(order.scope or "")
                return f"{order.unit} done"

        return E()

    integration_gate = _PassGate()
    state = run_loop(
        "x", _BrainClient(SPEC_PAR, DEC_REACT), executor=None, gate=integration_gate,
        executor_factory=make_ex, gate_factory=lambda wt: _PassGate(),
        max_parallel=2, workdir=workdir, prompt_dir=PROMPT_DIR, skills_dir=skills_dir,
    )
    assert state.status is Status.done
    # executor가 받은 order.scope에 스킬 주입됨(병렬 경로)
    assert any(marker in s and SKILL_SECTION_HEADER in s for s in seen_scopes)
    # 통합 gate가 본 입력엔 스킬 없음(분리 보존)
    for seen in integration_gate.calls:
        assert marker not in seen
