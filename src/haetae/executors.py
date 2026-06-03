"""Executor 구현들.

이번 단계의 유일한 executor = HumanRelayExecutor: haetae가 work order를 만들어
사람에게 건네고(present), 사람이 CC로 실행한 결과를 받아(collect) 루프를 잇는다.
자율 코딩 executor(codex write 모드 + sandbox)는 위험도가 높아 별도 후속 WO.
"""

from __future__ import annotations

import sys
from typing import Callable

from haetae.models import NextOrder

# 사람이 결과 입력을 끝낼 때 쓰는 센티넬 라인
SENTINEL = "---END---"


def _stdin_collect() -> str:
    """stdin에서 센티넬 라인 또는 EOF까지 읽어 결과 텍스트로 반환한다."""
    lines: list[str] = []
    for line in sys.stdin:
        if line.rstrip("\n") == SENTINEL:
            break
        lines.append(line)
    return "".join(lines).strip()


def format_work_order(order: NextOrder) -> str:
    """NextOrder를 사람이 CC에 붙여넣기 좋은 work order 텍스트로 포맷한다."""
    out: list[str] = []
    bar = "=" * 60
    out.append(bar)
    out.append(f"# WORK ORDER — unit {order.unit}")
    out.append("")
    out.append("## goal")
    out.append(order.goal)
    if order.scope:
        out.append("\n## scope")
        out.append(order.scope)
    if order.context_refs:
        out.append("\n## context_refs")
        out.extend(f"- {c}" for c in order.context_refs)
    if order.local_checks:
        out.append("\n## local_checks")
        for c in order.local_checks:
            extra = f"  (기대값 pass: {c.pass_})" if c.pass_ else ""
            out.append(f"- [{c.type.value}] {c.cmd or '(cmd 없음)'}{extra}")
    if order.deliverable:
        out.append("\n## deliverable")
        out.append(order.deliverable)
    out.append("")
    out.append(f"실행 후 결과를 붙여넣고 '{SENTINEL}' 한 줄로 끝내세요 (또는 EOF).")
    out.append(bar)
    return "\n".join(out)


class HumanRelayExecutor:
    """work order를 사람에게 제시하고 사람이 돌린 결과를 받아오는 Executor.

    present: work order 텍스트를 사람에게 보여주는 함수(기본 print).
    collect: 사람의 결과 텍스트를 수집하는 함수(기본 stdin 센티넬/EOF 수집).
    둘 다 주입 가능 → 테스트에서 캡처/캔된 값으로 대체.
    """

    def __init__(
        self,
        present: Callable[[str], None] = print,
        collect: Callable[[], str] = _stdin_collect,
    ):
        self.present = present
        self.collect = collect

    def run(self, order: NextOrder) -> str:
        self.present(format_work_order(order))
        return self.collect()
