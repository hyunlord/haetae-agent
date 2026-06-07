"""Executor 구현들.

이번 단계의 유일한 executor = HumanRelayExecutor: haetae가 work order를 만들어
사람에게 건네고(present), 사람이 CC로 실행한 결과를 받아(collect) 루프를 잇는다.
자율 코딩 executor(codex write 모드 + sandbox)는 위험도가 높아 별도 후속 WO.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable

from haetae.metering import Usage
from haetae.models import NextOrder
from haetae.providers.codex import CodexError, exec_codex_with_usage

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


class CodexExecutorError(RuntimeError):
    """CodexExecutor 실행 실패(비정상 종료/빈 출력/타임아웃 등)."""


# codex에게 work order 뒤에 붙이는 실행 지시. HumanRelay용 센티넬 안내는 무시되고
# 대신 "이 디렉토리에서 직접 구현하고 요약 보고하라"를 명시한다.
_EXEC_INSTRUCTION = (
    "\n\n"
    + "=" * 60
    + "\n"
    "위 work order를 **지금 이 작업 디렉토리에서 직접** 구현하라.\n"
    "- 필요한 파일을 만들고/수정하라 (이 디렉토리 밖은 건드리지 마라).\n"
    "- local_checks가 있으면 직접 돌려 검증하라.\n"
    "- 끝나면 무엇을 했는지(변경/생성한 파일, 검증 결과)를 한국어로 요약 보고하라.\n"
)


class CodexExecutor:
    """work order를 codex(쓰기 sandbox)에 직접 던져 `--workdir`에서 자율 구현시키는 Executor.

    HumanRelayExecutor를 대체해 run_loop에 끼울 수 있다(둘 다 Executor Protocol 충족).

    ⚠️ SAFETY: 이건 LLM이 만든 work order를 *쓰기 권한*으로 실행하는 위험 단계다.
      - sandbox는 가장 좁은 쓰기 모드(workspace-write)만. danger-full-access는
        exec_codex의 화이트리스트가 코드 레벨에서 막는다.
      - 실행 범위는 cwd=workdir로 한정된다(`-C`). workdir 밖은 codex가 못 건드린다.
      - 단, 지금은 버리는 scratch 폴더(예: ~/haetae-test/...) 용도다. 진짜 repo에
        쓰려면 컨테이너/VM 격리가 필요하며 그건 후속 hardening(이번 스코프 아님).

    model:   codex 모델 override. None이면 codex 설정 기본.
    workdir: codex 작업 루트(cwd). 실행이 이 폴더로 한정된다.
    timeout: subprocess 타임아웃(초). 자율 구현은 느릴 수 있어 넉넉히.
    sandbox: 쓰기 sandbox. 기본 workspace-write(가장 좁은 쓰기 모드).
    """

    def __init__(
        self,
        model: str | None = None,
        workdir: str | Path = ".",
        timeout: float = 1800.0,
        sandbox: str = "workspace-write",
    ):
        self.model = model
        self.workdir = Path(workdir)
        self.timeout = timeout
        self.sandbox = sandbox
        # 직전 실행의 token usage(WO#33). 미노출/파싱 실패면 None(날조 금지).
        self.last_usage: Usage | None = None

    # ── Executor 인터페이스 ────────────────────────────────────────────
    def run(self, order: NextOrder) -> str:
        prompt = format_work_order(order) + _EXEC_INSTRUCTION
        return self._run(prompt)

    # ── 테스트 seam: 실제 subprocess 실행은 공유 헬퍼로 격리 ────────────
    def _run(self, prompt: str) -> str:
        try:
            text, usage = exec_codex_with_usage(
                prompt,
                sandbox=self.sandbox,
                cwd=str(self.workdir),
                model=self.model,
                timeout=self.timeout,
            )
        except CodexError as e:
            raise CodexExecutorError(str(e)) from e
        self.last_usage = usage  # 읽기만 — sandbox 권한 불변
        return text
