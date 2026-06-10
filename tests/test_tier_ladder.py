"""반응형 tier 사다리 테스트 (WO#64).

설계: 유닛은 싼 tier로 시작 → gate가 판정(진짜 결과) → 막히면 *그 유닛만* 한 tier 상향.
첫 시도 자체가 probe(별도 throwaway probe 없음). 신호는 gate verdict.

여기서 검증:
  - 사다리 상향: gate 실패 재dispatch마다 팩토리가 한 칸 위 tier(model/effort)로 호출됨, top에서 cap.
  - 싼 경로: tier0 통과 → escalation 0(tier0만).
  - 시작 힌트: start_tier 있는 유닛은 base가 그 tier(probe부터 높게).
  - back-compat: 사다리 미지정 → 단일 tier·1-arg 팩토리 그대로(661 무회귀의 단위 근거).
  - 적대 분리: tier는 *빌더(executor) 팩토리에만*. gate_factory는 tier 미수신(1-arg).
  - anti-erosion: tier 상향해도 spec bar(done_when/acceptance_criteria) 불변.
  - bounded: tier가 사다리 top 초과 안 함. 하트비트에 tier 노출.

codex 없이 mock/실git. brain은 main 스레드 직렬 호출이라 결정적.
"""

import threading
from pathlib import Path

from haetae.executors import Tier, tier_label
from haetae.loop import (
    MockGate,
    _build_executor,
    _factory_accepts_tier,
    resolve_start_tier,
    run_loop,
)
from haetae.models import GateResult, Status, Verdict
from haetae.run import parse_tier_ladder

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPT_DIR = REPO_ROOT / "prompts"

LADDER3 = [Tier("m0", "medium"), Tier("m1", "high"), Tier("m2", "xhigh")]


def _spec(units: str) -> str:
    return f"""\
spec_id: tier-001
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
    check: {{ type: test, cmd: "true" }}
assumptions: []
non_goals: ["n"]
done_when: "ac1"
decomposition:
{units}
open_questions: []
"""


SPEC_SINGLE = _spec("  - { unit: u1, desc: a, deps: [] }")
SPEC_HINTED = _spec('  - { unit: u1, desc: a, deps: [], start_tier: "m1:high" }')

DEC = """\
verdict: pass
action: next_order
rationale: "build unit"
next_order:
  unit: placeholder
  goal: "unit 구현"
  deliverable: "요약"
"""


class BrainClient:
    """call#1=synthesize(spec) / 이후=replan(DEC). 재dispatch로 호출이 늘어도 안전."""

    def __init__(self, spec_yaml: str, dec_yaml: str = DEC):
        self.spec = spec_yaml
        self.dec = dec_yaml
        self.n = 0

    def complete(self, system: str, user: str, **opts) -> str:
        self.n += 1
        return self.spec if self.n == 1 else self.dec


class TierSpyExec:
    """tier-aware 팩토리가 만드는 executor — 받은 tier를 공유 리스트에 기록(스레드 안전)."""

    def __init__(self, tier: Tier, seen: list, lock: threading.Lock):
        with lock:
            seen.append(tier)

    def run(self, order):
        return "ran"


def _tier_factory(seen: list, lock: threading.Lock):
    return lambda wt, tier: TierSpyExec(tier, seen, lock)


# ──────────────────────── 순수 유닛(빠름, git 무관) ────────────────────────


def test_parse_tier_ladder_unspecified_is_single_tier():
    """미지정이면 [(--model, --reasoning-effort)] 단일 tier = 후방호환."""
    assert parse_tier_ladder(None, "gpt-x", "high") == [Tier("gpt-x", "high")]
    assert parse_tier_ladder("", None, None) == [Tier(None, None)]


def test_parse_tier_ladder_multi():
    assert parse_tier_ladder("m0:medium,m1:high,m2:xhigh", None, None) == LADDER3
    # effort 생략 → None. 빈 모델(":high") → None.
    assert parse_tier_ladder("m0,:high", "d", "e") == [Tier("m0", None), Tier(None, "high")]


def test_resolve_start_tier_matches_label_or_model():
    assert resolve_start_tier("m1:high", LADDER3) == 1
    assert resolve_start_tier("m1/high", LADDER3) == 1
    assert resolve_start_tier("m2", LADDER3) == 2
    # 미매칭/빈 → 0. 단일 사다리 → 항상 0.
    assert resolve_start_tier("nope", LADDER3) == 0
    assert resolve_start_tier("", LADDER3) == 0
    assert resolve_start_tier("m2", [Tier("m0", "medium")]) == 0


def test_build_executor_back_compat_one_arg_ignores_tier():
    """1-arg 팩토리(기존 전부)는 tier가 있어도 wt만 받는다 → 661 무회귀의 근거."""
    assert not _factory_accepts_tier(lambda wt: object())
    assert _factory_accepts_tier(lambda wt, tier: object())
    seen = []
    _build_executor(lambda wt: seen.append("1arg") or object(), Path("."), Tier("x", "high"))
    assert seen == ["1arg"]  # 호출됨, tier 미전달(예외 없음)
    got = []
    _build_executor(lambda wt, tier: got.append(tier) or object(), Path("."), Tier("x", "high"))
    assert got == [Tier("x", "high")]


def test_tier_label():
    assert tier_label(Tier("m1", "high")) == "m1/high"
    assert tier_label(Tier(None, None)) == "-/-"


# ──────────────────────── 사다리 상향 / cap (실git) ────────────────────────


def test_ladder_escalates_one_tier_per_gate_fail(tmp_path):
    """gate 실패 재dispatch마다 팩토리가 한 칸 위 tier로 호출된다 (t0→t1→t2)."""
    seen: list[Tier] = []
    lock = threading.Lock()
    state = run_loop(
        "x", BrainClient(SPEC_SINGLE), executor=None, gate=PassGateOk(),
        executor_factory=_tier_factory(seen, lock),
        gate_factory=lambda wt: GateFail(),
        max_parallel=2, workdir=tmp_path, prompt_dir=PROMPT_DIR,
        unit_retries=2, or_alternatives=0, tier_ladder=LADDER3,
    )
    assert state.status is Status.escalated
    # 최초 1 + 재시도 2 = 3 시도, 각각 한 칸 위 tier.
    assert seen == [LADDER3[0], LADDER3[1], LADDER3[2]]


def test_ladder_caps_at_top(tmp_path):
    """재시도가 사다리보다 많아도 tier는 top에서 cap(더 안 올라감)."""
    seen: list[Tier] = []
    lock = threading.Lock()
    run_loop(
        "x", BrainClient(SPEC_SINGLE), executor=None, gate=PassGateOk(),
        executor_factory=_tier_factory(seen, lock),
        gate_factory=lambda wt: GateFail(),
        max_parallel=2, workdir=tmp_path, prompt_dir=PROMPT_DIR,
        unit_retries=4, or_alternatives=0, tier_ladder=LADDER3,
    )
    # 5 시도(1+4): t0,t1,t2, 그 다음은 top(t2)에서 cap.
    assert seen == [LADDER3[0], LADDER3[1], LADDER3[2], LADDER3[2], LADDER3[2]]


def test_cheap_path_no_escalation(tmp_path):
    """tier0에서 통과하면 escalation 0 — 팩토리는 tier0으로 단 한 번만 호출(비용 최소)."""
    seen: list[Tier] = []
    lock = threading.Lock()
    state = run_loop(
        "x", BrainClient(SPEC_SINGLE), executor=None, gate=PassGateOk(),
        executor_factory=_tier_factory(seen, lock),
        gate_factory=lambda wt: PassGateOk(),
        max_parallel=2, workdir=tmp_path, prompt_dir=PROMPT_DIR,
        unit_retries=2, tier_ladder=LADDER3,
    )
    assert state.status is Status.done  # 통과 → 통합 gate도 pass → done
    assert seen == [LADDER3[0]]  # 싼 tier만


def test_start_tier_hint_probes_higher(tmp_path):
    """start_tier 힌트가 있는 유닛은 base가 그 tier — 첫 probe부터 높게."""
    seen: list[Tier] = []
    lock = threading.Lock()
    run_loop(
        "x", BrainClient(SPEC_HINTED), executor=None, gate=PassGateOk(),
        executor_factory=_tier_factory(seen, lock),
        gate_factory=lambda wt: PassGateOk(),
        max_parallel=2, workdir=tmp_path, prompt_dir=PROMPT_DIR,
        unit_retries=2, tier_ladder=LADDER3,
    )
    assert seen == [LADDER3[1]]  # start_tier="m1:high" → base=1


# ──────────────────────── 적대 분리 / anti-erosion / 하트비트 ────────────────────────


def test_gate_factory_never_receives_tier(tmp_path):
    """적대 분리: tier는 *빌더 팩토리에만*. gate_factory는 1-arg(wt)로만 호출됨(judge 독립)."""
    gate_arities: list[int] = []
    lock = threading.Lock()

    def gate_factory(*args):
        with lock:
            gate_arities.append(len(args))
        return GateFail()

    seen: list[Tier] = []
    run_loop(
        "x", BrainClient(SPEC_SINGLE), executor=None, gate=PassGateOk(),
        executor_factory=_tier_factory(seen, lock),
        gate_factory=gate_factory,
        max_parallel=2, workdir=tmp_path, prompt_dir=PROMPT_DIR,
        unit_retries=2, or_alternatives=0, tier_ladder=LADDER3,
    )
    # 빌더는 tier 받음(상향 확인), gate는 매 호출 wt 하나만(tier 안 샘).
    assert seen[:2] == [LADDER3[0], LADDER3[1]]
    assert gate_arities and all(n == 1 for n in gate_arities)


def test_bar_unchanged_across_tier_escalation(tmp_path):
    """anti-erosion: tier 상향해도 gate가 보는 spec bar(done_when/criteria) 불변."""
    bars: list[tuple] = []
    lock = threading.Lock()

    class RecordingGate:
        def judge(self, result, spec, unit=None):
            with lock:
                bars.append((spec.done_when, tuple(ac.id for ac in spec.acceptance_criteria)))
            return GateResult(verdict=Verdict.fail_recoverable)

    seen: list[Tier] = []
    run_loop(
        "x", BrainClient(SPEC_SINGLE), executor=None, gate=PassGateOk(),
        executor_factory=_tier_factory(seen, lock),
        gate_factory=lambda wt: RecordingGate(),
        max_parallel=2, workdir=tmp_path, prompt_dir=PROMPT_DIR,
        unit_retries=2, or_alternatives=0, tier_ladder=LADDER3,
    )
    # tier가 t0→t1→t2로 올라가도 spec bar는 매 judge에서 동일.
    assert len(seen) == 3 and seen[0] != seen[2]  # tier는 변함
    assert len(set(bars)) == 1  # bar는 불변(같은 done_when/criteria)
    assert bars[0] == ("ac1", ("ac1",))


def test_heartbeat_surfaces_tier(tmp_path):
    """하트비트 build_kind에 현재 tier가 라이브로 실린다(다중 tier일 때)."""
    contexts: list[tuple] = []
    lock = threading.Lock()

    class SpyHeartbeat:
        def set_context(self, kind, unit):
            with lock:
                contexts.append((kind, unit))
        def get_context(self): return (None, None)
        def start(self, *a, **k): return 0
        def beat(self, *a, **k): pass
        def finish(self, *a, **k): pass

    seen: list[Tier] = []
    run_loop(
        "x", BrainClient(SPEC_SINGLE), executor=None, gate=PassGateOk(),
        executor_factory=_tier_factory(seen, lock),
        gate_factory=lambda wt: GateFail(),
        max_parallel=2, workdir=tmp_path, prompt_dir=PROMPT_DIR,
        unit_retries=1, or_alternatives=0, tier_ladder=LADDER3, heartbeat=SpyHeartbeat(),
    )
    build_kinds = [k for (k, _u) in contexts if k and "빌드" in k]
    # 빌드 컨텍스트에 tier 라벨이 노출(예: "빌드(재시도 0 · tier=m0/medium)").
    assert any("tier=m0/medium" in k for k in build_kinds)
    assert any("tier=m1/high" in k for k in build_kinds)


# ──────────────────────── 상태 없는 mock gate/executor ────────────────────────


class PassGateOk:
    def judge(self, result, spec, unit=None):
        return GateResult(verdict=Verdict.pass_)


class GateFail:
    def judge(self, result, spec, unit=None):
        return GateResult(verdict=Verdict.fail_recoverable)
