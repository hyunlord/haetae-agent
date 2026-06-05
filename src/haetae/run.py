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
from haetae.models import State


def run(
    order: str,
    *,
    client: LLMClient,
    executor: Executor,
    gate: Gate,
    critic_client: LLMClient | None = None,
    max_iters: int = 30,
    state_path: str | Path | None = None,
    prompt_dir: str | Path | None = None,
    progress: Callable[[str], None] | None = None,
    max_parallel: int = 1,
    workdir: str | Path | None = None,
    executor_factory: Callable | None = None,
    gate_factory: Callable | None = None,
    unit_retries: int = 2,
) -> State:
    """주입된 brain/executor/gate로 루프를 한 번 완주하고 최종 State를 반환한다."""
    return run_loop(
        order,
        client,
        executor,
        gate,
        critic_client=critic_client,
        max_iters=max_iters,
        state_path=state_path,
        prompt_dir=prompt_dir,
        progress=progress,
        max_parallel=max_parallel,
        workdir=workdir,
        executor_factory=executor_factory,
        gate_factory=gate_factory,
        unit_retries=unit_retries,
    )


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
        "--max-parallel",
        type=int,
        default=4,
        help=(
            "동시에 굴릴 ready unit 수 (기본 4). 1이면 현행 순차 경로(worktree 미사용). "
            ">1이면 git worktree per unit 격리 + 결정적 DAG 스케줄링."
        ),
    )
    args = parser.parse_args(argv)

    client = CodexClient(model=args.model)
    # judge는 read-only CodexClient(executor와 다른 --judge-model 가능). judge 타입
    # 기준이 없는 spec(예: palindrome)이면 CompositeGate가 judge를 아예 안 부른다.
    gate = CompositeGate(
        workdir=args.workdir,
        judge_client=CodexClient(model=args.judge_model),
        run_timeout=args.run_timeout,
        install_deps=args.install_deps,
        install_timeout=args.install_timeout,
    )
    if args.executor == "codex":
        # 자율 쓰기 실행 — gate와 같은 --workdir로 범위 한정.
        executor: Executor = CodexExecutor(model=args.model, workdir=args.workdir)
    else:
        executor = HumanRelayExecutor()

    # 병렬 모드(>1): unit마다 worktree 경로에 묶인 executor/gate를 만든다.
    # 통합 gate(머지된 main 1회)는 위 gate(=main workdir)를 그대로 쓴다.
    executor_factory = None
    gate_factory = None
    if args.max_parallel > 1:
        if args.executor == "codex":
            executor_factory = lambda wt: CodexExecutor(model=args.model, workdir=wt)
        else:
            executor_factory = lambda wt: HumanRelayExecutor()
        gate_factory = lambda wt: CompositeGate(
            workdir=wt, judge_client=CodexClient(model=args.judge_model),
            run_timeout=args.run_timeout,
            install_deps=args.install_deps, install_timeout=args.install_timeout)

    # spec critic: --critic-model 줄 때만 ON(read-only, 합성기와 다른 모델 권장 = 독립성).
    # 없으면 None → critic OFF(추가 비용 0, 기존 동작 불변).
    critic_client = CodexClient(model=args.critic_model) if args.critic_model else None

    # 진행 표시: 느린 codex 호출이 "행"으로 안 보이게 stderr로 한 줄씩.
    def progress(msg: str) -> None:
        print(f"… {msg}", file=sys.stderr, flush=True)

    state = run(
        args.order,
        client=client,
        executor=executor,
        gate=gate,
        critic_client=critic_client,
        max_iters=args.max_iters,
        state_path=args.state_path,
        progress=progress,
        max_parallel=args.max_parallel,
        workdir=args.workdir,
        executor_factory=executor_factory,
        gate_factory=gate_factory,
        unit_retries=args.unit_retries,
    )
    print(format_summary(state))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
