"""HumanRelayExecutor 테스트 — 실제 codex/stdin 없이 present/collect 주입."""

from haetae.executors import SENTINEL, HumanRelayExecutor, format_work_order, _stdin_collect
from haetae.loop import Executor
from haetae.models import Check, CheckType, NextOrder


def _order() -> NextOrder:
    return NextOrder(
        unit="u1",
        goal="로그인 폼 구현",
        scope="폼만. 소셜 로그인 제외",
        context_refs=["spec.ac1", "state.u0"],
        local_checks=[Check(type=CheckType.test, cmd="pytest test_login")],
        executor="codex",
        deliverable="변경 파일 목록 + 요약",
    )


def test_run_presents_order_and_returns_collected():
    captured: list[str] = []
    ex = HumanRelayExecutor(present=captured.append, collect=lambda: "사람이 돌린 결과")
    result = ex.run(_order())
    assert result == "사람이 돌린 결과"
    # 제시된 텍스트에 핵심 필드가 포함됐는지
    assert len(captured) == 1
    text = captured[0]
    assert "로그인 폼 구현" in text
    assert "pytest test_login" in text
    assert "폼만. 소셜 로그인 제외" in text
    assert SENTINEL in text


def test_format_work_order_handles_minimal_order():
    minimal = NextOrder(unit="u9", goal="최소 주문")
    text = format_work_order(minimal)
    assert "u9" in text
    assert "최소 주문" in text


def test_humanrelay_satisfies_executor_protocol():
    assert isinstance(HumanRelayExecutor(), Executor)


def test_stdin_collect_reads_until_sentinel(monkeypatch):
    import io

    monkeypatch.setattr(
        "sys.stdin", io.StringIO(f"line1\nline2\n{SENTINEL}\nline3\n")
    )
    assert _stdin_collect() == "line1\nline2"


def test_stdin_collect_reads_until_eof(monkeypatch):
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO("only\nlines\n"))
    assert _stdin_collect() == "only\nlines"
