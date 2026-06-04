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
from haetae.gate import CheckRunner
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
    max_iters: int = 20,
    state_path: str | Path | None = None,
    prompt_dir: str | Path | None = None,
    progress: Callable[[str], None] | None = None,
) -> State:
    """주입된 brain/executor/gate로 루프를 한 번 완주하고 최종 State를 반환한다."""
    return run_loop(
        order,
        client,
        executor,
        gate,
        max_iters=max_iters,
        state_path=state_path,
        prompt_dir=prompt_dir,
        progress=progress,
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
        "--executor",
        choices=["human", "codex"],
        default="human",
        help="실행자 (기본: human=사람 릴레이). codex=자율 쓰기 실행(opt-in)",
    )
    parser.add_argument("--state-path", default=None, help="최종 State를 저장할 YAML 경로")
    parser.add_argument("--max-iters", type=int, default=20, help="최대 루프 횟수 (기본 20)")
    args = parser.parse_args(argv)

    client = CodexClient(model=args.model)
    gate = CheckRunner(workdir=args.workdir)
    if args.executor == "codex":
        # 자율 쓰기 실행 — gate와 같은 --workdir로 범위 한정.
        executor: Executor = CodexExecutor(model=args.model, workdir=args.workdir)
    else:
        executor = HumanRelayExecutor()

    # 진행 표시: 느린 codex 호출이 "행"으로 안 보이게 stderr로 한 줄씩.
    def progress(msg: str) -> None:
        print(f"… {msg}", file=sys.stderr, flush=True)

    state = run(
        args.order,
        client=client,
        executor=executor,
        gate=gate,
        max_iters=args.max_iters,
        state_path=args.state_path,
        progress=progress,
    )
    print(format_summary(state))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
