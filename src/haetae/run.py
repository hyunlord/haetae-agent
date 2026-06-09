"""CLI 엔트리 — `python -m haetae.run --order "..."`.

첫 실제 end-to-end: brain=CodexClient, gate=CheckRunner, executor=HumanRelayExecutor.
배선 로직은 run()으로 빼서 테스트 가능하게 두고, __main__은 인자 파싱 + 호출만 한다.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable

from haetae.executors import CodexExecutor, HumanRelayExecutor
from haetae.gate import CompositeGate
from haetae.llm import CodexClient
from haetae.loop import Executor, Gate, run_loop
from haetae.llm import LLMClient
from haetae.metering import MeteredClient
from haetae.models import State

# 기본 스킬 디렉토리 = 이 repo의 skills/ (src/haetae/run.py → parents[2] = repo 루트).
_DEFAULT_SKILLS_DIR = str(Path(__file__).resolve().parents[2] / "skills")
# 큐레이션 능력 레지스트리(WO#53 F.1) = 이 repo의 capabilities/. opt-in(--capabilities)일 때만 사용.
_DEFAULT_CAPABILITIES_DIR = str(Path(__file__).resolve().parents[2] / "capabilities")


def run(
    order: str,
    *,
    client: LLMClient,
    executor: Executor,
    gate: Gate,
    critic_client: LLMClient | None = None,
    max_iters: int = 30,
    decomp_critic: bool = True,
    decomp_retries: int = 1,
    or_alternatives: int = 1,
    state_path: str | Path | None = None,
    prompt_dir: str | Path | None = None,
    progress: Callable[[str], None] | None = None,
    max_parallel: int = 1,
    workdir: str | Path | None = None,
    executor_factory: Callable | None = None,
    gate_factory: Callable | None = None,
    unit_retries: int = 2,
    scaffold_client: LLMClient | None = None,
    install_deps: bool = True,
    skills_dir: str | Path | None = None,
    pricing: dict | None = None,
    capabilities_on: bool = False,
    capability_registry_path: str | Path | None = None,
    capability_allowlist: list[str] | None = None,
) -> State:
    """주입된 brain/executor/gate로 루프를 한 번 완주하고 최종 State를 반환한다."""
    return run_loop(
        order,
        client,
        executor,
        gate,
        critic_client=critic_client,
        capabilities_on=capabilities_on,
        capability_registry_path=capability_registry_path,
        capability_allowlist=capability_allowlist,
        max_iters=max_iters,
        decomp_critic=decomp_critic,
        decomp_retries=decomp_retries,
        or_alternatives=or_alternatives,
        state_path=state_path,
        prompt_dir=prompt_dir,
        progress=progress,
        max_parallel=max_parallel,
        workdir=workdir,
        executor_factory=executor_factory,
        gate_factory=gate_factory,
        unit_retries=unit_retries,
        scaffold_client=scaffold_client,
        install_deps=install_deps,
        skills_dir=skills_dir,
        pricing=pricing,
    )


def _load_pricing(path: str | None) -> dict | None:
    """가격표 JSON을 best-effort 로드: {model: [input_per_mtok, output_per_mtok]}.

    경로 없음 → None(usd 미계산). 로드/형식 실패도 None(계측이 run을 막지 않는다).
    값은 (input, output) 튜플로 정규화한다.
    """
    if not path:
        return None
    try:
        import json

        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        out: dict[str, tuple[float, float]] = {}
        for model, rate in raw.items():
            if isinstance(rate, (list, tuple)) and len(rate) == 2:
                out[model] = (float(rate[0]), float(rate[1]))
        return out or None
    except Exception as e:  # noqa: BLE001 — 가격표 로드 실패는 계측만 비활성(run 진행)
        print(f"… 가격표 로드 실패({path}): {e} — usd 미계산으로 진행", file=sys.stderr)
        return None


def format_summary(state: State) -> str:
    """사람이 읽기 좋은 최종 State 요약."""
    lines = [
        "── haetae 루프 종료 ──",
        f"status            : {state.status.value}",
        f"events            : {len(state.events)}",
        "plan              : "
        + (", ".join(f"{p.unit}={p.state.value}" for p in state.plan) or "(없음)"),
    ]
    if state.pending_escalations:
        lines.append(f"pending_escalations: {len(state.pending_escalations)}")
        for e in state.pending_escalations:
            lines.append(f"  - {e}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="haetae.run",
        description="order 한 줄에서 시작해 haetae 루프를 돈다 (사람이 executor).",
    )
    parser.add_argument("--order", required=True, help="주문 원문")
    parser.add_argument(
        "--workdir", default=".", help="check 실행 + codex executor의 cwd (gate/executor 공유, 기본: .)"
    )
    parser.add_argument("--model", default=None, help="codex 모델 override (기본: codex 설정)")
    parser.add_argument(
        "--reasoning-effort",
        choices=["minimal", "low", "medium", "high", "xhigh"],
        default=None,
        help=(
            "codex executor 추론 강도(minimal..xhigh). 미설정(기본)이면 플래그 미부착 → "
            "codex 기본(medium) 그대로(기존 동작 불변). xhigh=거친 동선 frontier 레버."
        ),
    )
    parser.add_argument(
        "--judge-model",
        default=None,
        help="judge 전용 codex 모델 (executor --model과 다르게 줘 독립성 확보; 기본: codex 설정)",
    )
    parser.add_argument(
        "--critic-model",
        default=None,
        help=(
            "spec critic 전용 codex 모델 (주면 적대적 spec 비평 ON — 다른 모델 권장 = 독립성). "
            "없으면 critic OFF(추가 비용 0, 기존 동작 불변)"
        ),
    )
    parser.add_argument(
        "--decomp-critic",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "분해 critic at replan (기본 on, Phase C = LEAP LLM 리뷰어). replan이 낸 매 work "
            "order의 *진전성*을 독립 critic(critic-model)이 판정 → 무진전/재진술이면 reject·재계획. "
            "--no-decomp-critic으로 끔. **--critic-model이 있어야 동작**(없으면 자동 OFF). "
            "verifier-side·soft·best-effort(실패 시 진행)."
        ),
    )
    parser.add_argument(
        "--decomp-retries",
        type=int,
        default=1,
        help=(
            "분해 critic이 weak(무진전) 판정 시 재계획하는 최대 횟수 (기본 1). "
            "소진 후에도 weak면 *진행*(데드락 금지)하고 critique를 state에 기록."
        ),
    )
    parser.add_argument(
        "--or-alternatives",
        type=int,
        default=1,
        help=(
            "OR-node 대안(기본 1, Phase D = LEAP AND-OR DAG). gate(유닛/통합)가 정상 재시도까지 "
            "소진하고도 실패하면 *같은 criteria를 둔 채* 근본적으로 다른 접근으로 갈아타 백트래킹·재시도. "
            "대안 소진 시 escalate(시도한 접근 첨부). 0이면 기존 동작(즉시 escalate, 후방호환). 병렬(>1) 경로."
        ),
    )
    parser.add_argument(
        "--executor",
        choices=["human", "codex"],
        default="human",
        help="실행자 (기본: human=사람 릴레이). codex=자율 쓰기 실행(opt-in)",
    )
    parser.add_argument("--state-path", default=None, help="최종 State를 저장할 YAML 경로")
    parser.add_argument("--max-iters", type=int, default=30, help="최대 루프 횟수 (기본 30)")
    parser.add_argument(
        "--unit-retries",
        type=int,
        default=2,
        help=(
            "병렬 경로: 유닛 gate 실패/머지 충돌 시 그 유닛 재dispatch 최대 횟수 (기본 2). "
            "소진 후 escalate. (LLM 출력 재시도 replan_retries와는 별개.)"
        ),
    )
    parser.add_argument(
        "--run-timeout",
        type=float,
        default=120.0,
        help="run 체크(산출물 실행)의 타임아웃 초 (기본 120). 호스트에서 실행, 바운드 필수.",
    )
    parser.add_argument(
        "--codex-idle-timeout",
        type=float,
        default=300.0,
        help=(
            "codex 호출의 *무진행(idle) timeout* 초 (기본 300). `--json` 이벤트가 이 시간 동안 "
            "끊기면(무음=멈춤) 멈춘 프로세스를 정리하고 라우팅한다(필수=재시도/escalate, "
            "best-effort=degrade). **총 시간 cap이 아니라 침묵만 잼** — 진행 중인 긴 호출은 "
            "안 죽인다. 추론 갭보단 길고 무한 hang보단 짧게."
        ),
    )
    parser.add_argument(
        "--codex-max-duration",
        type=float,
        default=None,
        help=(
            "codex 호출의 *선택적* 절대 backstop 초 (기본 없음=off). 진행 중이어도 pathological"
            "하게 길면 차단. 주 메커니즘은 idle — 이건 안전망일 뿐."
        ),
    )
    parser.add_argument(
        "--install-deps",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "gate 체크 전에 호스트(네트워크 O)가 npm/pip install을 대신 수행 (기본 on). "
            "--no-install-deps로 끈다. executor sandbox는 그대로 offline."
        ),
    )
    parser.add_argument(
        "--install-timeout",
        type=int,
        default=300,
        help="호스트 install 타임아웃 초 (기본 300). non-fatal — 초과해도 run은 진행.",
    )
    parser.add_argument(
        "--scaffold",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "executor dispatch 전에 director(host=네트워크 O)가 진짜 스택 스캐폴드를 깔고 "
            "deps 설치 (기본 on=auto: dep 스택 필요할 때만, 아니면 자동 스킵). "
            "--no-scaffold로 끈다(기존 동작 그대로). executor sandbox는 그대로 offline."
        ),
    )
    parser.add_argument(
        "--max-parallel",
        type=int,
        default=4,
        help=(
            "동시에 굴릴 ready unit 수 (기본 4). 1이면 현행 순차 경로(worktree 미사용). "
            ">1이면 git worktree per unit 격리 + 결정적 DAG 스케줄링."
        ),
    )
    parser.add_argument(
        "--skills-dir",
        default=_DEFAULT_SKILLS_DIR,
        help=(
            "읽기전용 패턴 스킬(skills/<name>/SKILL.md) 디렉토리 (기본: 이 repo의 skills/). "
            "매칭된 스킬을 유닛 work order에 빌더 가이드로 주입한다(judge/gate엔 안 들어감)."
        ),
    )
    parser.add_argument(
        "--skills",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="스킬 주입 on/off (기본 on). --no-skills로 끄면 주입 없음(기존 동작 불변).",
    )
    parser.add_argument(
        "--pricing",
        default=None,
        help=(
            "코스트 계측용 가격표 JSON 경로 (model → [input_$/Mtok, output_$/Mtok]). "
            "주면 usd를 계산, 없으면 모델 미상 → usd=null(토큰만 기록). best-effort 로드."
        ),
    )
    # ── 능력 획득 거버넌스(WO#53 F.1) — opt-in. 기본 OFF면 완전 no-op(기존 동작 불변) ──
    parser.add_argument(
        "--capabilities",
        action="store_true",
        default=False,
        help=(
            "능력 획득 거버넌스 ON(기본 OFF). spec이 요청한 *없는 능력*을 큐레이션 레지스트리에서 "
            "발견→POC→사람 승인(escalate)→채택(provenance). **자동 채택 없음**·executor sandbox 불변."
        ),
    )
    parser.add_argument(
        "--capability-registry",
        default=_DEFAULT_CAPABILITIES_DIR,
        help="큐레이션 능력 레지스트리 디렉토리(capabilities/*.yaml). --capabilities일 때만 사용.",
    )
    parser.add_argument(
        "--capability-allowlist",
        default="",
        help=(
            "사람이 *out-of-band로 승인*한 능력 식별자(쉼표 구분). 승인된 것만 채택(provenance) — "
            "미승인은 escalate(검토 후 여기 추가하고 재실행). 비면 전부 미승인(자동 채택 없음)."
        ),
    )
    args = parser.parse_args(argv)

    pricing = _load_pricing(args.pricing)

    # WO#54: idle(무진행) timeout을 모든 codex 클라이언트에 건다. brain(합성/replan/scaffold)과
    #   executor(빌드)는 *필수* → stall_retries=1(bounded 재시도 후 escalate). judge/critic은
    #   *best-effort* → stall_retries=0(degrade 빠르게). 멈춰도 무한 hang 없이 종료.
    idle_to = args.codex_idle_timeout
    max_dur = args.codex_max_duration

    client = CodexClient(
        model=args.model, idle_timeout=idle_to, max_duration=max_dur, stall_retries=1
    )

    # judge LLM 비용 계측(WO#34): judge client를 passthrough MeteredClient로 감싼다.
    # complete 결과를 그대로 통과시키므로 gate 검증 *행동*은 불변 — 비용만 기록된다.
    # gate가 호출마다 이 client를 드레인해 GateResult.judge_cost로 노출하고, 루프가
    # event.cost/budget에 합산한다. judge_model이 없으면 codex 기본(모델 미상→usd=null).
    def make_judge_client() -> LLMClient:
        return MeteredClient(
            CodexClient(
                model=args.judge_model, idle_timeout=idle_to, max_duration=max_dur,
                stall_retries=0,  # best-effort: 멈추면 degrade(skipped→ambiguous, 가짜 pass 금지)
            ),
            source="judge", pricing=pricing,
        )

    # judge는 read-only CodexClient(executor와 다른 --judge-model 가능). judge 타입
    # 기준이 없는 spec(예: palindrome)이면 CompositeGate가 judge를 아예 안 부른다.
    gate = CompositeGate(
        workdir=args.workdir,
        judge_client=make_judge_client(),
        run_timeout=args.run_timeout,
        install_deps=args.install_deps,
        install_timeout=args.install_timeout,
    )
    if args.executor == "codex":
        # 자율 쓰기 실행 — gate와 같은 --workdir로 범위 한정.
        # reasoning_effort: 미설정(None)이면 codex 기본(medium) 그대로(후방호환).
        executor: Executor = CodexExecutor(
            model=args.model, workdir=args.workdir,
            reasoning_effort=args.reasoning_effort,
            idle_timeout=idle_to, max_duration=max_dur, stall_retries=1,
        )
    else:
        executor = HumanRelayExecutor()

    # 병렬 모드(>1): unit마다 worktree 경로에 묶인 executor/gate를 만든다.
    # 통합 gate(머지된 main 1회)는 위 gate(=main workdir)를 그대로 쓴다.
    executor_factory = None
    gate_factory = None
    if args.max_parallel > 1:
        if args.executor == "codex":
            executor_factory = lambda wt: CodexExecutor(
                model=args.model, workdir=wt, reasoning_effort=args.reasoning_effort,
                idle_timeout=idle_to, max_duration=max_dur, stall_retries=1,
            )
        else:
            executor_factory = lambda wt: HumanRelayExecutor()
        # per-worktree gate도 metered judge client(유닛마다 새 인스턴스 → 스레드 안전).
        gate_factory = lambda wt: CompositeGate(
            workdir=wt, judge_client=make_judge_client(),
            run_timeout=args.run_timeout,
            install_deps=args.install_deps, install_timeout=args.install_timeout)

    # spec critic: --critic-model 줄 때만 ON(read-only, 합성기와 다른 모델 권장 = 독립성).
    # 없으면 None → critic OFF(추가 비용 0, 기존 동작 불변).
    critic_client = (
        CodexClient(
            model=args.critic_model, idle_timeout=idle_to, max_duration=max_dur,
            stall_retries=0,  # best-effort: 멈추면 진행(critic은 advisory)
        )
        if args.critic_model else None
    )

    # 선제 스캐폴드(WO#27): --scaffold(기본 on)면 brain client를 scaffold 생성에 재사용.
    # --no-scaffold면 None → 스캐폴드 OFF(기존 동작 그대로). 생성기는 dep 스택 필요할 때만
    # 골격을 내고 아니면 자동 스킵(auto). 호스트 install은 --install-deps 토글을 공유한다.
    scaffold_client = client if args.scaffold else None

    # 스킬 주입(빌더 전용): --skills(기본 on)면 --skills-dir에서 로드. --no-skills면 None.
    skills_dir = args.skills_dir if args.skills else None

    # 진행 표시: 느린 codex 호출이 "행"으로 안 보이게 stderr로 한 줄씩.
    def progress(msg: str) -> None:
        print(f"… {msg}", file=sys.stderr, flush=True)

    # graceful stop(WO#43): 웹 stop(#37)은 이 프로세스에 SIGINT를 보낸다. run_loop이
    # 이미 정리·저장·클린 마무리하지만(KeyboardInterrupt를 잡아 State 반환), 그 바깥
    # (배선/예외 흐름)에서 인터럽트가 와도 raw traceback 없이 클린 종료한다.
    # 종료코드 0 = stopped로 해석(대시보드가 "failed"로 오해하지 않게 정합).
    try:
        state = run(
            args.order,
            client=client,
            executor=executor,
            gate=gate,
            critic_client=critic_client,
            max_iters=args.max_iters,
            decomp_critic=args.decomp_critic,
            decomp_retries=args.decomp_retries,
            or_alternatives=args.or_alternatives,
            state_path=args.state_path,
            progress=progress,
            max_parallel=args.max_parallel,
            workdir=args.workdir,
            executor_factory=executor_factory,
            gate_factory=gate_factory,
            unit_retries=args.unit_retries,
            scaffold_client=scaffold_client,
            install_deps=args.install_deps,
            skills_dir=skills_dir,
            pricing=pricing,
            capabilities_on=args.capabilities,
            # opt-in: --capabilities일 때만 레지스트리/allowlist를 넘긴다(OFF면 no-op).
            capability_registry_path=(args.capability_registry if args.capabilities else None),
            capability_allowlist=(
                [s.strip() for s in args.capability_allowlist.split(",") if s.strip()]
                if args.capabilities else None
            ),
        )
    except KeyboardInterrupt:
        print("중단됨 (사용자 stop/SIGINT)", file=sys.stderr, flush=True)
        return 0
    print(format_summary(state))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
