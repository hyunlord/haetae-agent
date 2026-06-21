"""LocalAgentExecutor 테스트 (WO#137) — 약한 로컬 모델 빌더 provider.

엔드포인트는 *모킹*(post_chat monkeypatch)이라 CI에서 라이브 불요. 라이브 통합은 opt-in
(HAETAE_LOCAL_IT=1). **적대 분리 단언**: 로컬 provider가 judge/run-judge/gate/critic에
안 닿음(구조적·소스 양쪽).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

import haetae.providers.local_agent as la
from haetae.providers.local_agent import (
    DONE_MARKER,
    FileEdit,
    LocalAgentError,
    LocalAgentExecutor,
    _accumulate_tokens,
    _extract_path,
    apply_edits,
    is_done,
    parse_edits,
    safe_target,
)
from haetae.llm import LLMClient
from haetae.loop import Executor
from haetae.metering import Usage
from haetae.models import Check, CheckType, NextOrder


def _order(unit="u1", goal="작은 모듈 구현", refs=None) -> NextOrder:
    return NextOrder(
        unit=unit,
        goal=goal,
        scope="이 유닛만",
        context_refs=refs or [],
        local_checks=[Check(type=CheckType.test, cmd="pytest -q")],
        executor="local",
        deliverable="변경 파일",
    )


class FakePost:
    """post_chat 모킹 — 스크립트된 응답을 순서대로(소진 시 마지막 반복) 돌려준다.

    scripted 항목: content(str) 또는 {"content":..., "usage":{"prompt_tokens":.., "completion_tokens":..}}.
    호출 인자(endpoint/payload/timeout)는 self.calls에 기록.
    """

    def __init__(self, scripted):
        self.scripted = scripted
        self.calls: list[dict] = []

    def __call__(self, endpoint, payload, timeout):
        self.calls.append({"endpoint": endpoint, "payload": payload, "timeout": timeout})
        item = self.scripted[min(len(self.calls) - 1, len(self.scripted) - 1)]
        content = item if isinstance(item, str) else item.get("content", "")
        usage = None if isinstance(item, str) else item.get("usage")
        resp = {"choices": [{"message": {"role": "assistant", "content": content}}]}
        if usage is not None:
            resp["usage"] = usage
        return resp


def _block(path: str, body: str, lang: str = "") -> str:
    info = f"{lang} path={path}".strip()
    return f"```{info}\n{body}\n```"


# ──────────────────────────── 순수 파싱/안전 단위 ────────────────────────────


def test_parse_edits_path_tagged_block():
    text = _block("src/a.ts", "const x = 1;")
    edits = parse_edits(text)
    assert edits == [FileEdit("src/a.ts", "const x = 1;\n")]


def test_parse_edits_tolerates_language_token_and_preamble():
    """#134: 모델이 언어 토큰(```ts)·프리앰블 산문을 붙여도 견고 파싱."""
    text = (
        "먼저 파일을 만들겠습니다. 설명 산문...\n\n"
        + _block("src/a.ts", "export const a = 1;", lang="ts")
        + "\n중간 설명...\n"
        + _block("src/b.py", "x = 2", lang="python")
        + f"\n{DONE_MARKER}\n"
    )
    edits = parse_edits(text)
    assert [e.path for e in edits] == ["src/a.ts", "src/b.py"]
    assert is_done(text)


def test_parse_edits_ignores_plain_language_fence_without_path():
    """경로 없는 단순 언어 펜스(예시/설명)는 편집이 아니라 무시한다."""
    text = "예시:\n```typescript\nconsole.log('demo')\n```\n실제 편집 없음"
    assert parse_edits(text) == []


def test_parse_edits_bare_path_fence():
    text = "```src/c.js\nmodule.exports = {}\n```"
    edits = parse_edits(text)
    assert edits == [FileEdit("src/c.js", "module.exports = {}\n")]


def test_parse_edits_later_block_wins_for_same_path():
    text = _block("a.txt", "old") + "\n" + _block("a.txt", "new")
    edits = parse_edits(text)
    assert edits == [FileEdit("a.txt", "new\n")]


def test_extract_path_variants():
    assert _extract_path("path=src/a.ts") == "src/a.ts"
    assert _extract_path("ts path=src/a.ts") == "src/a.ts"
    assert _extract_path('path="src/a.ts"') == "src/a.ts"  # 따옴표 제거(공백 없는 경로)
    assert _extract_path("src/bare.ts") == "src/bare.ts"
    assert _extract_path("typescript") is None
    assert _extract_path("") is None


def test_safe_target_confines_to_workdir(tmp_path):
    assert safe_target(tmp_path, "src/a.ts") == (tmp_path / "src/a.ts").resolve()
    # 절대경로는 상대로 강등 → workdir 안으로
    assert safe_target(tmp_path, "/src/a.ts") == (tmp_path / "src/a.ts").resolve()
    # .. 탈출은 거부
    assert safe_target(tmp_path, "../escape.txt") is None
    assert safe_target(tmp_path, "../../etc/passwd") is None
    assert safe_target(tmp_path, "") is None


def test_apply_edits_writes_and_skips_escapes(tmp_path):
    edits = [
        FileEdit("src/a.ts", "A\n"),
        FileEdit("../escape.txt", "X\n"),  # 탈출 → 건너뜀
        FileEdit("nested/dir/b.py", "B\n"),
    ]
    applied = apply_edits(tmp_path, edits)
    assert applied == ["src/a.ts", "nested/dir/b.py"]
    assert (tmp_path / "src/a.ts").read_text() == "A\n"
    assert (tmp_path / "nested/dir/b.py").read_text() == "B\n"
    # workdir 밖에 절대 안 씀
    assert not (tmp_path.parent / "escape.txt").exists()


def test_is_done_marker_and_lone_done():
    assert is_done(f"some text\n{DONE_MARKER}")
    assert is_done("DONE")
    assert is_done("<<DONE>>")
    assert not is_done("not done yet")


def test_accumulate_tokens_sums_and_preserves_none():
    assert _accumulate_tokens(None, None, None) == (None, None)
    assert _accumulate_tokens(None, None, Usage(10, 5)) == (10, 5)
    assert _accumulate_tokens(10, 5, Usage(3, 7)) == (13, 12)
    # 미상(None)은 보존
    assert _accumulate_tokens(10, None, Usage(None, 4)) == (10, 4)


# ──────────────────────────── Executor (모킹) ────────────────────────────


def test_satisfies_executor_protocol():
    ex = LocalAgentExecutor(endpoint="http://x/v1", model="m", workdir="/tmp")
    assert isinstance(ex, Executor)


def test_run_applies_single_edit_and_returns_summary(tmp_path, monkeypatch):
    fake = FakePost([_block("src/a.ts", "export const x = 1") + f"\n{DONE_MARKER}"])
    monkeypatch.setattr(la, "post_chat", fake)
    ex = LocalAgentExecutor(endpoint="http://x/v1", model="qwen", workdir=tmp_path, max_turns=4)
    result = ex.run(_order())
    assert (tmp_path / "src/a.ts").read_text() == "export const x = 1\n"
    assert "src/a.ts" in result
    assert ex.last_applied == ["src/a.ts"]
    # DONE 신호 → 한 턴에 종료
    assert len(fake.calls) == 1
    assert ex.last_turns == 1


def test_run_payload_shape_and_protocol_system_prompt(tmp_path, monkeypatch):
    fake = FakePost([_block("a.txt", "hi") + f"\n{DONE_MARKER}"])
    monkeypatch.setattr(la, "post_chat", fake)
    ex = LocalAgentExecutor(endpoint="http://host:8089/v1", model="qwen2.5-coder:7b", workdir=tmp_path)
    ex.run(_order(goal="고유한_목표_문자열"))
    payload = fake.calls[0]["payload"]
    assert payload["model"] == "qwen2.5-coder:7b"
    assert payload["stream"] is False
    assert fake.calls[0]["endpoint"] == "http://host:8089/v1"
    sys_msg = payload["messages"][0]
    user_msg = payload["messages"][1]
    assert sys_msg["role"] == "system" and "path=" in sys_msg["content"]
    assert "고유한_목표_문자열" in user_msg["content"]


def test_run_applies_multiple_files(tmp_path, monkeypatch):
    text = _block("src/a.ts", "A", lang="ts") + "\n" + _block("src/b.ts", "B") + f"\n{DONE_MARKER}"
    monkeypatch.setattr(la, "post_chat", FakePost([text]))
    ex = LocalAgentExecutor(endpoint="http://x/v1", model="m", workdir=tmp_path)
    ex.run(_order())
    assert (tmp_path / "src/a.ts").read_text() == "A\n"
    assert (tmp_path / "src/b.ts").read_text() == "B\n"


def test_turn_cap_when_never_done(tmp_path, monkeypatch):
    """DONE 없이 매 턴 편집만 내면 turn 상한에서 멈춘다(경제성)."""
    # 매 턴 같은(또는 새) 편집, DONE 없음
    fake = FakePost([_block("src/a.ts", "v1"), _block("src/a.ts", "v2"), _block("src/a.ts", "v3")])
    monkeypatch.setattr(la, "post_chat", fake)
    ex = LocalAgentExecutor(endpoint="http://x/v1", model="m", workdir=tmp_path, max_turns=2)
    ex.run(_order())
    assert len(fake.calls) == 2  # max_turns에서 cap
    assert ex.last_turns == 2


def test_error_feedback_one_turn(tmp_path, monkeypatch):
    """verify 실패 시 *한 번* 에러를 피드백해 재시도한다(에러-피드백 1턴)."""
    fake = FakePost([
        _block("src/a.py", "x = ") + f"\n{DONE_MARKER}",      # turn0: 깨진 코드 + (성급한) DONE
        _block("src/a.py", "x = 1") + f"\n{DONE_MARKER}",     # turn1(피드백): 고친 코드
    ])
    monkeypatch.setattr(la, "post_chat", fake)

    calls = {"n": 0}

    def verify(workdir: Path) -> str | None:
        calls["n"] += 1
        body = (workdir / "src/a.py").read_text()
        return "SyntaxError: invalid syntax" if body.strip().endswith("=") else None

    ex = LocalAgentExecutor(
        endpoint="http://x/v1", model="m", workdir=tmp_path, max_turns=3, verify=verify
    )
    ex.run(_order())
    # turn0 DONE이었지만 verify 실패가 우선 → 2번째 호출(피드백)이 일어났다
    assert len(fake.calls) == 2
    # 2번째 호출의 user 메시지에 에러가 들어갔다
    fb_messages = fake.calls[1]["payload"]["messages"]
    assert any("SyntaxError" in m.get("content", "") for m in fb_messages if m["role"] == "user")
    # 최종 파일은 고쳐졌다
    assert (tmp_path / "src/a.py").read_text() == "x = 1\n"


def test_verify_pass_no_feedback_turn(tmp_path, monkeypatch):
    fake = FakePost([_block("src/a.py", "x = 1") + f"\n{DONE_MARKER}"])
    monkeypatch.setattr(la, "post_chat", fake)
    ex = LocalAgentExecutor(
        endpoint="http://x/v1", model="m", workdir=tmp_path, max_turns=3,
        verify=lambda wd: None,  # 항상 통과
    )
    ex.run(_order())
    assert len(fake.calls) == 1  # 피드백 턴 없음


def test_last_usage_accumulates_across_turns(tmp_path, monkeypatch):
    fake = FakePost([
        {"content": _block("a.txt", "1"), "usage": {"prompt_tokens": 100, "completion_tokens": 20}},
        {"content": _block("b.txt", "2") + f"\n{DONE_MARKER}",
         "usage": {"prompt_tokens": 50, "completion_tokens": 10}},
    ])
    monkeypatch.setattr(la, "post_chat", fake)
    ex = LocalAgentExecutor(endpoint="http://x/v1", model="qwen", workdir=tmp_path, max_turns=3)
    ex.run(_order())
    assert len(fake.calls) == 2
    assert ex.last_usage is not None
    assert ex.last_usage.input_tokens == 150
    assert ex.last_usage.output_tokens == 30
    assert ex.last_usage.model == "qwen"


def test_no_usage_keeps_last_usage_none(tmp_path, monkeypatch):
    monkeypatch.setattr(la, "post_chat", FakePost([_block("a.txt", "1") + f"\n{DONE_MARKER}"]))
    ex = LocalAgentExecutor(endpoint="http://x/v1", model="m", workdir=tmp_path)
    ex.run(_order())
    assert ex.last_usage is None  # 날조 금지


def test_no_edits_raises_local_agent_error(tmp_path, monkeypatch):
    monkeypatch.setattr(la, "post_chat", FakePost(["설명만 하고 편집 블록 없음(프로토콜 위반)"]))
    ex = LocalAgentExecutor(endpoint="http://x/v1", model="m", workdir=tmp_path, max_turns=2)
    with pytest.raises(LocalAgentError):
        ex.run(_order())


def test_run_rejects_path_escape(tmp_path, monkeypatch):
    """모델이 workdir 밖 경로를 내도 *적용 안 한다*(안전 경계)."""
    text = _block("../escape.txt", "PWNED") + "\n" + _block("ok.txt", "fine") + f"\n{DONE_MARKER}"
    monkeypatch.setattr(la, "post_chat", FakePost([text]))
    ex = LocalAgentExecutor(endpoint="http://x/v1", model="m", workdir=tmp_path)
    ex.run(_order())
    assert (tmp_path / "ok.txt").exists()
    assert not (tmp_path.parent / "escape.txt").exists()
    assert ex.last_applied == ["ok.txt"]


def test_max_turns_must_be_positive():
    with pytest.raises(ValueError):
        LocalAgentExecutor(endpoint="http://x/v1", model="m", max_turns=0)


def test_post_chat_builds_url_and_parses(monkeypatch):
    """post_chat이 베이스 URL에 /chat/completions를 붙이고 JSON을 파싱한다(urlopen 모킹)."""
    import json as _json

    captured = {}

    class _FakeResp:
        def __init__(self, body):
            self._b = body.encode("utf-8")

        def read(self):
            return self._b

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = _json.loads(req.data.decode("utf-8"))
        return _FakeResp(_json.dumps({"choices": [{"message": {"content": "hi"}}]}))

    monkeypatch.setattr(la.urllib.request, "urlopen", fake_urlopen)
    out = la.post_chat("http://host:8089/v1", {"model": "m", "messages": []}, timeout=5)
    assert captured["url"] == "http://host:8089/v1/chat/completions"
    assert captured["body"]["model"] == "m"
    assert out["choices"][0]["message"]["content"] == "hi"


def test_post_chat_http_error_becomes_local_agent_error(monkeypatch):
    import urllib.error

    def boom(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(la.urllib.request, "urlopen", boom)
    with pytest.raises(LocalAgentError):
        la.post_chat("http://x/v1", {"model": "m", "messages": []}, timeout=1)


# ──────────────────────── 적대 분리 단언 (sacred — 최우선) ────────────────────────


def test_local_executor_is_not_a_judge_client():
    """구조적 분리: LocalAgentExecutor엔 complete()가 없어 LLMClient(judge client)가 될 수 없다.

    → judge/run-judge/critic 경로(LLMClient.complete를 부름)에 *끼울 수조차 없다*.
    """
    ex = LocalAgentExecutor(endpoint="http://x/v1", model="m")
    assert isinstance(ex, Executor)          # 빌더로는 됨
    assert not isinstance(ex, LLMClient)     # judge client로는 안 됨
    assert not hasattr(ex, "complete")


def test_judge_gate_critic_sources_do_not_reference_local_provider():
    """소스 분리: 판정/critic 모듈이 로컬 provider를 *import·언급조차 안 한다*.

    빌더 전용 불변(약한 모델이 판정 경로에 새지 않음)을 소스 레벨에서 못 박는다.
    """
    import haetae.decomp_critic as decomp_critic
    import haetae.gate as gate
    import haetae.judge as judge
    import haetae.spec_critic as spec_critic

    for mod in (judge, gate, spec_critic, decomp_critic):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        assert "local_agent" not in src, f"{mod.__name__}이 local_agent를 참조함(분리 위반)"
        assert "LocalAgentExecutor" not in src, f"{mod.__name__}이 LocalAgentExecutor를 참조함(분리 위반)"


def test_local_provider_does_not_import_judge_or_gate():
    """로컬 provider는 judge/gate/critic을 import하지 않는다(역방향 분리·순환 차단)."""
    src = Path(la.__file__).read_text(encoding="utf-8")
    for forbidden in ("haetae.judge", "haetae.gate", "haetae.spec_critic", "haetae.decomp_critic"):
        assert forbidden not in src, f"local_agent이 {forbidden}을 import함(분리 위반)"


# ──────────────────────── 빌더-측 스모크 (WO#139) ────────────────────────


def test_builder_smoke_compile_catches_syntax(tmp_path):
    (tmp_path / "bad.py").write_text("def f(:\n    pass\n")
    err = la.builder_smoke(tmp_path)
    assert err is not None and "컴파일" in err and "bad.py" in err


def test_builder_smoke_clean_passes(tmp_path):
    (tmp_path / "ok.py").write_text("x = 1\n")
    assert la.builder_smoke(tmp_path) is None  # 컴파일 OK + 테스트 없음(collect exit 5) → None


def test_builder_smoke_collect_catches_import_error(tmp_path):
    """실 pytest collect-only: test가 없는 모듈을 import하면 collection error를 잡는다(#138 케이스)."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text(
        "from nonexistent_pkg_xyz_138 import thing\n\ndef test_a():\n    assert True\n"
    )
    err = la.builder_smoke(tmp_path)
    assert err is not None and "collect" in err.lower()


def test_builder_smoke_collect_only_does_not_run_or_score(tmp_path):
    """적대 분리: collect/discover는 *findability*만 — 실행하면 FAIL할 테스트도 *실행/채점 안 함*."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("def test_would_fail():\n    assert False\n")
    # gate가 pytest면 bare-fn도 findable → collect 통과(채점 아님; would-fail이어도 None)
    assert la.builder_smoke(tmp_path, check_cmds=["python -m pytest tests/test_x.py"]) is None


def test_builder_smoke_skips_when_pytest_absent(tmp_path, monkeypatch):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("def test_a():\n    assert True\n")
    monkeypatch.setattr(la, "_smoke_run", lambda cmd, cwd, timeout: (1, "No module named pytest"))
    assert la.builder_smoke(tmp_path) is None  # 툴 부재 → skip(빌드 막지 않음)


def test_builder_smoke_collect_error_via_seam(tmp_path, monkeypatch):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_m.py").write_text(
        "import unittest\nclass T(unittest.TestCase):\n    def test_a(self):\n        pass\n"
    )
    monkeypatch.setattr(
        la, "_smoke_run",
        lambda cmd, cwd, timeout: (2, "ImportError: cannot import name 'winner' from 'rules'"),
    )
    err = la.builder_smoke(tmp_path, check_cmds=["python -m pytest tests/"])
    assert err is not None and "exit 2" in err and "winner" in err


class _ScriptedSmoke:
    """N번 실패(에러 반환) 후 통과(None). 호출 인자 기록."""

    def __init__(self, fail_times: int, err: str = "ImportError: cannot import name 'winner'"):
        self.fail_times = fail_times
        self.err = err
        self.calls = 0

    def __call__(self, workdir):
        self.calls += 1
        return self.err if self.calls <= self.fail_times else None


def test_smoke_feedback_iterates_not_one_shot(tmp_path, monkeypatch):
    """#139 핵심: 스모크가 2회 실패하면 *2번 다* 에러를 피드백해 self-fix(1회 한정 아님)."""
    fake = FakePost([
        _block("src/a.py", "v1"),
        _block("src/a.py", "v2"),
        _block("src/a.py", "v3") + f"\n{DONE_MARKER}",
    ])
    monkeypatch.setattr(la, "post_chat", fake)
    smoke = _ScriptedSmoke(fail_times=2)
    ex = LocalAgentExecutor(
        endpoint="http://x/v1", model="m", workdir=tmp_path, max_turns=4, verify=smoke
    )
    ex.run(_order())
    assert len(fake.calls) == 3            # build → fix → fix(pass)
    assert ex.smoke_feedback_count == 2    # 두 실패 모두 피드백(반복)
    assert ex.last_smoke_passed is True
    # 2번째·3번째 호출 user 메시지에 정확한 에러가 주입됐다
    for ci in (1, 2):
        msgs = fake.calls[ci]["payload"]["messages"]
        assert any("cannot import name 'winner'" in m.get("content", "")
                   for m in msgs if m["role"] == "user")


def test_smoke_pass_first_try_no_feedback(tmp_path, monkeypatch):
    monkeypatch.setattr(la, "post_chat", FakePost([_block("a.py", "1") + f"\n{DONE_MARKER}"]))
    ex = LocalAgentExecutor(
        endpoint="http://x/v1", model="m", workdir=tmp_path, verify=lambda wd: None
    )
    ex.run(_order())
    assert ex.smoke_feedback_count == 0 and ex.last_smoke_passed is True


def test_smoke_exhausts_turns_returns_not_raises(tmp_path, monkeypatch):
    """스모크가 끝내 통과 못 해도 turn 상한서 멈추고 *반환*한다(gate가 이후 판정)."""
    monkeypatch.setattr(la, "post_chat", FakePost([_block("a.py", "x")]))  # 매번 같은 편집
    ex = LocalAgentExecutor(
        endpoint="http://x/v1", model="m", workdir=tmp_path, max_turns=2,
        verify=lambda wd: "ImportError: always",
    )
    result = ex.run(_order())  # raise 안 함(적용 파일 있음)
    assert ex.last_smoke_passed is False
    assert ex.smoke_feedback_count >= 1
    assert "미통과" in result


def test_smoke_is_builder_side_not_judge():
    """적대 분리: 스모크(builder_smoke)는 judge/gate를 import·호출하지 않는다(컴파일+collect만)."""
    src = Path(la.__file__).read_text(encoding="utf-8")
    # builder_smoke 본문이 judge/gate/run_judge를 부르지 않음(소스 스캔은 모듈 전역 분리 테스트가 커버)
    assert "py_compile" in src and "--collect-only" in src
    for forbidden in ("haetae.judge", "haetae.gate", "GateResult", "run_judge"):
        assert forbidden not in src, f"local_agent이 {forbidden} 참조(분리 위반)"


# ──────────────────── 스모크 v2: gate-discovery 정렬 + 속도 (WO#141) ────────────────────


def test_parse_unittest_discovery():
    assert la._parse_unittest_discovery(["python -m unittest discover -s tests -p test_*.py"]) == ("tests", "test_*.py")
    assert la._parse_unittest_discovery(["python -m unittest discover"]) == (".", "test*.py")
    assert la._parse_unittest_discovery(["python -m pytest tests/"]) is None


def test_smoke_conventions_mirror_gate_runner(tmp_path):
    (tmp_path / "tests").mkdir()
    # pytest-only gate → unittest 강제 안 함(over-constraint 회피)
    assert la._smoke_conventions(tmp_path, ["python -m pytest tests/test_x.py"]) == (True, None)
    # unittest gate → unittest 디스커버리 미러
    assert la._smoke_conventions(tmp_path, ["python -m unittest discover -s tests -p test_*.py"]) == (True, ("tests", "test_*.py"))
    # 모름 → 양쪽(tests/ 있으면 거기)
    assert la._smoke_conventions(tmp_path, []) == (True, ("tests", "test*.py"))


def _write_pkg_with_test(tmp_path, test_body):
    (tmp_path / "rules.py").write_text("def winner(b):\n    return None\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_rules.py").write_text(test_body)


def test_smoke_unittest_gate_catches_unfindable_test(tmp_path):
    """#140 exit-5 fix: gate가 unittest면, pytest는 수집해도 unittest가 못 찾는 bare-fn 테스트를 잡는다."""
    _write_pkg_with_test(tmp_path, "from rules import winner\n\ndef test_w():\n    assert winner([]) is None\n")
    err = la.builder_smoke(tmp_path, check_cmds=["python -m unittest discover -s tests -p test_*.py"])
    assert err is not None and "unittest 발견 실패" in err


def test_smoke_pytest_gate_no_unittest_overconstraint(tmp_path):
    """gate가 pytest면 bare-fn 테스트도 통과(unittest findability 강제 안 함)."""
    _write_pkg_with_test(tmp_path, "from rules import winner\n\ndef test_w():\n    assert winner([]) is None\n")
    assert la.builder_smoke(tmp_path, check_cmds=["python -m pytest tests/test_rules.py"]) is None


def test_smoke_unittest_testcase_passes_and_not_scored(tmp_path):
    """적대 분리 유지: 디스커버리는 *발견만* — 실행하면 FAIL할 TestCase도 통과(채점 아님)."""
    _write_pkg_with_test(
        tmp_path,
        "import unittest\nclass T(unittest.TestCase):\n    def test_fails(self):\n        self.assertTrue(False)\n",
    )
    assert la.builder_smoke(tmp_path, check_cmds=["python -m unittest discover -s tests"]) is None


def test_smoke_unknown_cmds_checks_both(tmp_path):
    """check_cmds 모르면 양쪽 디스커버리 — bare-fn 테스트는 unittest 기본 디스커버리가 잡는다."""
    _write_pkg_with_test(tmp_path, "from rules import winner\n\ndef test_w():\n    assert winner([]) is None\n")
    err = la.builder_smoke(tmp_path)  # check_cmds 없음 → 양쪽
    assert err is not None and "unittest 발견 실패" in err


def test_call_verify_passes_check_cmds_or_falls_back():
    rec = {}
    def smoke_like(workdir, check_cmds=None):
        rec["cmds"] = check_cmds
        return None
    la._call_verify(smoke_like, "/tmp/x", ["unit cmd"])
    assert rec["cmds"] == ["unit cmd"]
    # 1-arg 콜백(테스트 모킹)엔 workdir만 — back-compat
    assert la._call_verify(lambda wd: "ERR", "/tmp/x", ["ignored"]) == "ERR"


def test_executor_passes_unit_check_cmds_to_smoke(tmp_path, monkeypatch):
    """executor가 order.local_checks 명령을 스모크에 check_cmds로 넘긴다(디스커버리 정렬 배선)."""
    rec = {}
    def recording_verify(workdir, check_cmds=None):
        rec["cmds"] = check_cmds
        return None
    monkeypatch.setattr(la, "post_chat", FakePost([_block("a.py", "x") + f"\n{DONE_MARKER}"]))
    ex = LocalAgentExecutor(endpoint="http://x/v1", model="m", workdir=tmp_path, verify=recording_verify)
    order = NextOrder(
        unit="u1", goal="g",
        local_checks=[Check(type=CheckType.test, cmd="python -m unittest discover -s tests")],
    )
    ex.run(order)
    assert rec["cmds"] == ["python -m unittest discover -s tests"]


def test_smoke_pass_returns_immediately_speed(tmp_path, monkeypatch):
    """#141 속도: 구조 통과면 *즉시 반환*(불필요 continue 턴 0) — DONE 없어도 1턴서 끝."""
    fake = FakePost([_block("a.py", "x")])  # DONE 없음, 매번 같은 편집
    monkeypatch.setattr(la, "post_chat", fake)
    ex = LocalAgentExecutor(
        endpoint="http://x/v1", model="m", workdir=tmp_path, max_turns=5, verify=lambda wd: None
    )
    ex.run(_order())
    assert len(fake.calls) == 1            # 스모크 통과 → 즉시 반환(불필요 턴 0)
    assert ex.last_smoke_passed is True
    assert ex.last_turns == 1


def test_last_elapsed_surfaced_in_summary(tmp_path, monkeypatch):
    monkeypatch.setattr(la, "post_chat", FakePost([_block("a.py", "x") + f"\n{DONE_MARKER}"]))
    ex = LocalAgentExecutor(endpoint="http://x/v1", model="m", workdir=tmp_path)
    result = ex.run(_order())
    assert ex.last_elapsed_s >= 0.0
    assert "s\n" in result or "s " in result  # 벽시계 표면화


# ──────────────── 정밀 자기-테스트 피드백 + smoke -k 미러 (WO#144) ────────────────


def test_parse_pytest_k():
    """gate 체크 명령서 pytest -k <expr> 키워드를 뽑는다(없으면 None)."""
    assert la._parse_pytest_k(["python -m pytest -k board_rules"]) == "board_rules"
    assert la._parse_pytest_k(["pytest -k 'a and b'"]) == "a and b"
    assert la._parse_pytest_k(["python -m pytest tests/"]) is None
    assert la._parse_pytest_k(["python -m unittest discover -s tests"]) is None
    assert la._parse_pytest_k([]) is None


def test_smoke_pytest_k_catches_unmatched(tmp_path):
    """wart#2: gate가 `pytest -k board_rules`인데 테스트가 그 키워드에 안 맞으면 스모크가 findability로 잡는다."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_other.py").write_text("def test_other():\n    assert True\n")
    err = la.builder_smoke(tmp_path, check_cmds=["python -m pytest -k board_rules"])
    assert err is not None and "발견 실패" in err and "board_rules" in err


def test_smoke_pytest_k_matched_passes(tmp_path):
    """-k 키워드에 맞는 테스트가 있으면 통과(over-constraint 아님)."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_board_rules.py").write_text("def test_board_rules_x():\n    assert True\n")
    assert la.builder_smoke(tmp_path, check_cmds=["python -m pytest -k board_rules"]) is None


def test_smoke_no_k_unaffected(tmp_path):
    """-k 없는 gate cmd면 기존대로 collect-all(무회귀) — would-fail도 collect 통과(채점 아님)."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("def test_would_fail():\n    assert False\n")
    assert la.builder_smoke(tmp_path, check_cmds=["python -m pytest tests/test_x.py"]) is None


def test_selftest_cmd_normalizes_interpreter():
    """check_cmds의 pytest/unittest 명령을 venv python -m 형으로 정규화한다(없으면 None)."""
    assert la._selftest_cmd(["python -m pytest -k board_rules"]) == [sys.executable, "-m", "pytest", "-k", "board_rules"]
    assert la._selftest_cmd(["pytest -q"]) == [sys.executable, "-m", "pytest", "-q"]
    assert la._selftest_cmd(["python -m unittest discover -s tests"]) == [sys.executable, "-m", "unittest", "discover", "-s", "tests"]
    assert la._selftest_cmd(["echo hi"]) is None
    assert la._selftest_cmd([]) is None


def test_builder_selftest_failing_test_returns_detail(tmp_path):
    """#144 A: builder_selftest가 *테스트를 실행*해 실패 시 detail(테스트명·assertion) 반환."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_rules.py").write_text("def test_w():\n    assert 1 == 2\n")
    detail = la.builder_selftest(tmp_path, check_cmds=["python -m pytest -q"])
    assert detail is not None
    assert "자기-테스트 실패" in detail
    assert "test_w" in detail  # 실패 테스트명
    assert "assert" in detail.lower()  # assertion detail


def test_builder_selftest_passing_returns_none(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_ok.py").write_text("def test_ok():\n    assert 1 == 1\n")
    assert la.builder_selftest(tmp_path, check_cmds=["python -m pytest -q"]) is None


def test_builder_selftest_no_tests_returns_none(tmp_path):
    (tmp_path / "mod.py").write_text("x = 1\n")
    assert la.builder_selftest(tmp_path, check_cmds=["python -m pytest -q"]) is None


def test_builder_selftest_no_test_cmd_returns_none(tmp_path):
    """실행할 테스트 명령을 모르면 skip(None) — findability는 스모크 안전망."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_a():\n    assert False\n")
    assert la.builder_selftest(tmp_path, check_cmds=["true"]) is None


def test_builder_selftest_skips_when_pytest_absent(tmp_path, monkeypatch):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_a():\n    assert False\n")
    monkeypatch.setattr(la, "_smoke_run", lambda cmd, cwd, timeout: (127, ""))
    assert la.builder_selftest(tmp_path, check_cmds=["python -m pytest -q"]) is None


def test_builder_selftest_exit5_no_match_returns_none(tmp_path, monkeypatch):
    """exit 5(테스트/매칭 0)은 자기-테스트 실패 아님 — findability는 스모크 소관(None)."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n")
    monkeypatch.setattr(la, "_smoke_run", lambda cmd, cwd, timeout: (5, "no tests ran"))
    assert la.builder_selftest(tmp_path, check_cmds=["python -m pytest -k zzz"]) is None


class _ScriptedSelftest:
    """N번 실패(detail 반환) 후 통과(None). 호출 인자(check_cmds) 기록."""

    def __init__(self, fail_times, detail="[빌더 자기-테스트 실패] AssertionError: assert winner([]) is None"):
        self.fail_times = fail_times
        self.detail = detail
        self.calls = 0
        self.seen_cmds = None

    def __call__(self, workdir, check_cmds=None):
        self.calls += 1
        self.seen_cmds = check_cmds
        return self.detail if self.calls <= self.fail_times else None


def test_selftest_feedback_injects_detail_and_iterates(tmp_path, monkeypatch):
    """#144 A 핵심: 스모크 통과 *후* 자기-테스트 실패 detail을 다음 턴에 주입 → 타겟 수정 → green까지 반복."""
    fake = FakePost([
        _block("src/a.py", "v1"),
        _block("src/a.py", "v2"),
        _block("src/a.py", "v3") + f"\n{DONE_MARKER}",
    ])
    monkeypatch.setattr(la, "post_chat", fake)
    st = _ScriptedSelftest(fail_times=2)
    ex = LocalAgentExecutor(
        endpoint="http://x/v1", model="m", workdir=tmp_path, max_turns=4,
        verify=lambda wd: None,   # 스모크(findability) 항상 통과
        selftest=st,               # 자기-테스트 2회 실패 후 green
    )
    ex.run(_order())
    assert len(fake.calls) == 3            # build → fix → fix(green)
    assert ex.selftest_feedback_count == 2
    assert ex.last_selftest_passed is True
    # 주입된 메시지에 *정확한 실패 detail*(assertion)과 타겟-수정 유도 문구가 들어갔다
    for ci in (1, 2):
        msgs = fake.calls[ci]["payload"]["messages"]
        assert any("AssertionError" in m.get("content", "") for m in msgs if m["role"] == "user")
        assert any("타겟 수정" in m.get("content", "") for m in msgs if m["role"] == "user")
    # 자기-테스트가 유닛 gate 체크 명령(order.local_checks)을 받았다(gate 러너 미러)
    assert st.seen_cmds == ["pytest -q"]


def test_selftest_runs_only_after_smoke_passes(tmp_path, monkeypatch):
    """순서: 스모크(findability) 실패 동안엔 자기-테스트 안 돌고, 스모크 통과 후에야 돈다."""
    fake = FakePost([
        _block("src/a.py", "v1"),
        _block("src/a.py", "v2") + f"\n{DONE_MARKER}",
    ])
    monkeypatch.setattr(la, "post_chat", fake)
    smoke = _ScriptedSmoke(fail_times=1, err="ImportError: x")  # 1턴 스모크 실패
    st = _ScriptedSelftest(fail_times=0)  # 자기-테스트는 항상 green
    ex = LocalAgentExecutor(
        endpoint="http://x/v1", model="m", workdir=tmp_path, max_turns=4,
        verify=smoke, selftest=st,
    )
    ex.run(_order())
    # turn0: 스모크 실패(자기-테스트 안 돔). turn1: 스모크 통과 → 자기-테스트 1회(green).
    assert smoke.calls == 2
    assert st.calls == 1
    assert ex.last_selftest_passed is True


def test_selftest_off_by_default_no_extra_turns(tmp_path, monkeypatch):
    """selftest 미지정이면 자기-테스트 단계 없음(스모크만) — #141 즉시-반환 무회귀."""
    fake = FakePost([_block("a.py", "x")])  # DONE 없음
    monkeypatch.setattr(la, "post_chat", fake)
    ex = LocalAgentExecutor(
        endpoint="http://x/v1", model="m", workdir=tmp_path, max_turns=5, verify=lambda wd: None
    )
    ex.run(_order())
    assert len(fake.calls) == 1  # 스모크 통과 → 즉시 반환(selftest 없음)
    assert ex.last_selftest_passed is False
    assert ex.selftest_feedback_count == 0


def test_selftest_exhausts_turns_returns_not_raises(tmp_path, monkeypatch):
    """자기-테스트가 끝내 green 못 해도 turn 상한서 멈추고 *반환*(gate가 이후 판정)."""
    monkeypatch.setattr(la, "post_chat", FakePost([_block("a.py", "x")]))  # 매번 같은 편집
    ex = LocalAgentExecutor(
        endpoint="http://x/v1", model="m", workdir=tmp_path, max_turns=2,
        verify=lambda wd: None,
        selftest=lambda wd, check_cmds=None: "[빌더 자기-테스트 실패] AssertionError: always",
    )
    result = ex.run(_order())
    assert ex.last_selftest_passed is False
    assert ex.selftest_feedback_count >= 1
    assert "자기-테스트" in result  # 요약에 자기-테스트 상태 표면화


def test_selftest_is_builder_side_not_judge():
    """적대 분리(sacred): builder_selftest는 *자기 워크트리서 테스트 실행*만 — judge/gate/run-judge 무접촉.

    빌더가 *자기 유닛 테스트*를 green으로 모는 TDD 자기검사일 뿐, 독립 적대 gate(행동 run-judge·
    hollow 탐지·통합)는 불변·독립(소스 레벨 단언; 모듈 전역 분리는 위 테스트들이 커버).
    """
    src = Path(la.__file__).read_text(encoding="utf-8")
    assert "def builder_selftest" in src
    # gate-판정 토큰 무참조(complete() 부재는 구조적 test_local_executor_is_not_a_judge_client가 커버).
    for forbidden in ("haetae.judge", "haetae.gate", "GateResult", "run_judge", "CompositeGate"):
        assert forbidden not in src, f"local_agent이 {forbidden} 참조(분리 위반)"


# ──────────────────────────── (선택) 라이브 통합 — opt-in ────────────────────────────


@pytest.mark.skipif(
    os.environ.get("HAETAE_LOCAL_IT") != "1",
    reason="실 로컬 엔드포인트 통합 테스트는 opt-in (HAETAE_LOCAL_IT=1; 예: GB10 llama.cpp :8089)",
)
def test_local_executor_integration_creates_file(tmp_path):
    endpoint = os.environ.get("HAETAE_LOCAL_ENDPOINT", "http://100.70.109.50:8089/v1")
    model = os.environ.get("HAETAE_LOCAL_MODEL", "qwen2.5-coder:7b")
    ex = LocalAgentExecutor(endpoint=endpoint, model=model, workdir=str(tmp_path), max_turns=3)
    order = NextOrder(
        unit="it1",
        goal="파일 src/hello.ts를 만들고 `export const hello = () => 'hi';` 한 줄을 써라.",
        deliverable="만든 파일",
    )
    ex.run(order)
    assert (tmp_path / "src" / "hello.ts").exists()


@pytest.mark.skipif(
    os.environ.get("HAETAE_LOCAL_IT") != "1",
    reason="실 로컬 엔드포인트 통합 테스트는 opt-in (HAETAE_LOCAL_IT=1; 예: GB10 llama.cpp :8089)",
)
def test_smoke_live_self_consistency(tmp_path):
    """WO#139 라이브: 실 7B + builder_smoke로 *구조적 자기-정합* impl+test가 collect되게 만든다(#138 floor)."""
    endpoint = os.environ.get("HAETAE_LOCAL_ENDPOINT", "http://100.70.109.50:8089/v1")
    model = os.environ.get("HAETAE_LOCAL_MODEL", "qwen2.5-coder:7b")
    ex = LocalAgentExecutor(
        endpoint=endpoint, model=model, workdir=str(tmp_path), max_turns=4, verify=la.builder_smoke
    )
    order = NextOrder(
        unit="it_smoke",
        goal=(
            "Python으로 src/calc.py에 add(a, b)와 sub(a, b)를 구현하고, tests/test_calc.py에 그 둘을 "
            "import해 검증하는 pytest를 작성하라. test의 import 경로가 impl과 정확히 일치해야 한다."
        ),
        deliverable="calc 모듈 + 임포트 정합 테스트",
    )
    ex.run(order)
    # 스모크가 self-fix까지 끌어 구조적 자기-정합 달성(컴파일+collect 통과)
    assert la.builder_smoke(tmp_path) is None


# ──────────────────── 스택 감지 + JS/vitest 자기-테스트 확장 (WO#146) ────────────────────
#
# #145: #144 자기-테스트가 u1 수렴 실증(메커니즘 작동)했으나 Python(pytest/unittest) 전용 —
# 합성기가 게임에 흔히 고르는 JS/vitest 스택엔 inert. 북극성 태스크(snake/crowd-sim/platformer)가
# 다 JS/Canvas이므로 증명된 lift 메커니즘을 JS로 확장. 적대 분리 sacred(빌더-측 자기-테스트, gate 불변).


class _ScriptedSmokeRun:
    """_smoke_run 모킹 — cmd argv를 합쳐 substr 매칭으로 (rc, out) 반환(미매칭=(0,'')). 호출 기록."""

    def __init__(self, rules):  # rules: list[(substr, (rc, out))]
        self.rules = rules
        self.calls: list[list[str]] = []

    def __call__(self, cmd, cwd, timeout):
        self.calls.append(cmd)
        joined = " ".join(cmd)
        for sub, ret in self.rules:
            if sub in joined:
                return ret
        return (0, "")


def _write_js_pkg(tmp_path, *, vitest=True, test_file="tests/rules.test.ts", test_body="import {x} from '../src/rules';\ntest('w', () => { expect(x()).toBe(1); });\n"):
    (tmp_path / "package.json").write_text(
        '{"name":"g","scripts":{"test":"vitest run"}' + (',"devDependencies":{"vitest":"^2"}' if vitest else "") + "}\n"
    )
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src" / "rules.ts").write_text("export const x = () => 1;\n")
    if test_file:
        p = tmp_path / test_file
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(test_body)


# ── 스택 감지 ──────────────────────────────────────────────────────────────

def test_detect_stack_js_by_check_cmds():
    assert la._detect_stack(Path("/x"), ["npx vitest run"]) == "js"
    assert la._detect_stack(Path("/x"), ["npm test"]) == "js"
    assert la._detect_stack(Path("/x"), ["jest --ci"]) == "js"


def test_detect_stack_python_by_check_cmds_wins(tmp_path):
    """pytest/unittest 명령은 권위적 — package.json이 있어도 python 라우팅(혼합 회피)."""
    (tmp_path / "package.json").write_text("{}\n")
    assert la._detect_stack(tmp_path, ["python -m pytest -q"]) == "python"
    assert la._detect_stack(tmp_path, ["python -m unittest discover"]) == "python"


def test_detect_stack_js_by_package_json(tmp_path):
    (tmp_path / "package.json").write_text("{}\n")
    assert la._detect_stack(tmp_path, []) == "js"


def test_detect_stack_python_default(tmp_path):
    """신호 없음(빈 cmds·package.json 없음) → python 기본(기존 동작 무회귀)."""
    (tmp_path / "mod.py").write_text("x=1\n")
    assert la._detect_stack(tmp_path, []) == "python"
    assert la._detect_stack(tmp_path, None) == "python"


# ── JS 자기-테스트 명령 정규화 ────────────────────────────────────────────────

def test_selftest_js_cmd_normalizes():
    # npx vitest run … → 그대로(이미 one-shot)
    assert la._selftest_js_cmd(["npx vitest run tests/rules.test.ts"]) == ["npx", "vitest", "run", "tests/rules.test.ts"]
    # bare vitest → npx 접두 + one-shot 'run' 보장(watch-mode hang 회피)
    assert la._selftest_js_cmd(["vitest"]) == ["npx", "--no-install", "vitest", "run"]
    # npm test → 그대로(npm on PATH)
    assert la._selftest_js_cmd(["npm test"]) == ["npm", "test"]
    # jest → npx 접두(one-shot 기본)
    assert la._selftest_js_cmd(["jest"]) == ["npx", "--no-install", "jest"]
    # JS 러너 아님 → None
    assert la._selftest_js_cmd(["python -m pytest"]) is None
    assert la._selftest_js_cmd([]) is None


# ── JS 스모크(findability) ─────────────────────────────────────────────────

def test_builder_smoke_js_node_check_catches_syntax(tmp_path, monkeypatch):
    _write_js_pkg(tmp_path, test_file=None)
    (tmp_path / "src" / "bad.js").write_text("function f( {\n")  # 구문 오류
    monkeypatch.setattr(la, "_smoke_run", _ScriptedSmokeRun([("node --check", (1, "SyntaxError: Unexpected token"))]))
    err = la.builder_smoke(tmp_path, check_cmds=["npx vitest run"])
    assert err is not None and "node --check" in err and "SyntaxError" in err


def test_builder_smoke_js_clean_passes(tmp_path, monkeypatch):
    """JS 컴파일 OK + 테스트 파일 없음 → None(구조적 OK; mirror Python clean-pass)."""
    _write_js_pkg(tmp_path, test_file=None)
    (tmp_path / "src" / "ok.js").write_text("export const ok = 1;\n")
    monkeypatch.setattr(la, "_smoke_run", _ScriptedSmokeRun([("node --check", (0, ""))]))
    assert la.builder_smoke(tmp_path, check_cmds=["npx vitest run"]) is None


def test_builder_smoke_js_vitest_collect_catches_import_error(tmp_path, monkeypatch):
    """vitest list(수집)가 테스트의 import 에러를 findability 실패로 잡는다(#138-JS 동형)."""
    _write_js_pkg(tmp_path)
    run = _ScriptedSmokeRun([
        ("node --check", (0, "")),
        ("vitest list", (1, "Error: Cannot find module '../src/rules'")),
    ])
    monkeypatch.setattr(la, "_smoke_run", run)
    err = la.builder_smoke(tmp_path, check_cmds=["npx vitest run"])
    assert err is not None and "수집 실패" in err and "Cannot find module" in err


def test_builder_smoke_js_discovery_only_would_fail_passes(tmp_path, monkeypatch):
    """적대 분리: vitest list는 *발견만* — 실행하면 FAIL할 테스트도 수집되면 통과(채점 아님)."""
    _write_js_pkg(tmp_path, test_body="import {x} from '../src/rules';\ntest('w', () => { expect(x()).toBe(999); });\n")
    monkeypatch.setattr(la, "_smoke_run", _ScriptedSmokeRun([
        ("node --check", (0, "")),
        ("vitest list", (0, "src/rules.test.ts > w")),  # 수집 성공(would-fail이어도 list는 0)
    ]))
    assert la.builder_smoke(tmp_path, check_cmds=["npx vitest run"]) is None


def test_builder_smoke_js_skips_when_vitest_absent(tmp_path, monkeypatch):
    _write_js_pkg(tmp_path)
    monkeypatch.setattr(la, "_smoke_run", _ScriptedSmokeRun([
        ("node --check", (0, "")),
        ("vitest", (127, "")),  # vitest 미설치 → skip(빌드 막지 않음)
    ]))
    assert la.builder_smoke(tmp_path, check_cmds=["npx vitest run"]) is None


# ── JS 자기-테스트 ─────────────────────────────────────────────────────────

def test_builder_selftest_js_failing_returns_detail(tmp_path, monkeypatch):
    _write_js_pkg(tmp_path)
    monkeypatch.setattr(la, "_smoke_run", _ScriptedSmokeRun([
        ("vitest run", (1, "FAIL  tests/rules.test.ts > w\nexpected 1 to be 2 // Object.is equality")),
    ]))
    detail = la.builder_selftest(tmp_path, check_cmds=["npx vitest run"])
    assert detail is not None and "자기-테스트 실패" in detail
    assert "expected 1 to be 2" in detail  # expected vs actual detail


def test_builder_selftest_js_passing_returns_none(tmp_path, monkeypatch):
    _write_js_pkg(tmp_path)
    monkeypatch.setattr(la, "_smoke_run", _ScriptedSmokeRun([("vitest run", (0, "PASS tests/rules.test.ts"))]))
    assert la.builder_selftest(tmp_path, check_cmds=["npx vitest run"]) is None


def test_builder_selftest_js_no_tests_returns_none(tmp_path, monkeypatch):
    _write_js_pkg(tmp_path, test_file=None)
    monkeypatch.setattr(la, "_smoke_run", _ScriptedSmokeRun([("vitest", (1, "no test files found"))]))
    assert la.builder_selftest(tmp_path, check_cmds=["npx vitest run"]) is None  # 테스트 파일 없음 → skip


def test_builder_selftest_js_tool_absent_returns_none(tmp_path, monkeypatch):
    _write_js_pkg(tmp_path)
    monkeypatch.setattr(la, "_smoke_run", _ScriptedSmokeRun([("vitest", (127, ""))]))
    assert la.builder_selftest(tmp_path, check_cmds=["npx vitest run"]) is None


def test_builder_selftest_js_no_cmd_returns_none(tmp_path, monkeypatch):
    _write_js_pkg(tmp_path)
    monkeypatch.setattr(la, "_smoke_run", _ScriptedSmokeRun([("", (1, "x"))]))
    assert la.builder_selftest(tmp_path, check_cmds=["echo hi"]) is None  # JS 러너 명령 없음 → skip


# ── 라우팅: Python 무회귀 + JS 라우팅 ───────────────────────────────────────

def test_builder_smoke_routes_python_unchanged(tmp_path):
    """Python 워크트리(.py + pytest)는 기존 Python 경로 그대로 — collection error 잡음(무회귀)."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("from nonexistent_pkg_146 import t\ndef test_a():\n    assert True\n")
    err = la.builder_smoke(tmp_path, check_cmds=["python -m pytest tests/"])
    assert err is not None and "collect" in err.lower()


def test_selftest_js_feedback_iterates_end_to_end(tmp_path, monkeypatch):
    """엔드투엔드 JS: 실 builder_smoke+builder_selftest 배선, _smoke_run 모킹으로 JS 자기-테스트
    실패 detail이 다음 턴에 주입되고 타겟-수정 유도 → 2턴째 green까지 반복(루프는 스택-불가지)."""
    # 테스트 파일은 워크트리에 이미 존재(빌더는 impl을 그 테스트에 맞게 고친다 — _js_test_files 비지 않게).
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "rules.test.ts").write_text(
        "import {x} from '../src/rules';\ntest('w', () => { expect(x()).toBe(1); });\n"
    )
    fake = FakePost([
        _block("src/rules.ts", "export const x = () => 2;") + f"\n{DONE_MARKER}",
        _block("src/rules.ts", "export const x = () => 1;") + f"\n{DONE_MARKER}",
    ])
    monkeypatch.setattr(la, "post_chat", fake)
    # node --check OK; vitest list(수집) OK; vitest run: 1턴째 FAIL → 2턴째 PASS.
    state = {"runs": 0}

    def smoke_run(cmd, cwd, timeout):
        joined = " ".join(cmd)
        if "node --check" in joined or "vitest list" in joined:
            return (0, "")
        if "vitest run" in joined:
            state["runs"] += 1
            return (1, "FAIL > w\nexpected 2 to be 1") if state["runs"] == 1 else (0, "PASS")
        return (0, "")

    monkeypatch.setattr(la, "_smoke_run", smoke_run)
    ex = LocalAgentExecutor(
        endpoint="http://x/v1", model="m", workdir=tmp_path, max_turns=4,
        verify=la.builder_smoke, selftest=la.builder_selftest,
    )
    order = NextOrder(unit="u1", goal="JS 모듈", local_checks=[Check(type=CheckType.test, cmd="npx vitest run")], executor="local")
    ex.run(order)
    assert ex.selftest_feedback_count == 1 and ex.last_selftest_passed is True
    # 주입된 메시지에 JS 실패 detail + 타겟-수정 유도
    msgs = fake.calls[1]["payload"]["messages"]
    assert any("expected 2 to be 1" in m.get("content", "") for m in msgs if m["role"] == "user")
    assert any("타겟 수정" in m.get("content", "") for m in msgs if m["role"] == "user")


# ── 적대 분리(sacred) + 서버리스 ───────────────────────────────────────────

def test_js_selftest_is_builder_side_not_judge():
    """적대 분리: JS 자기-테스트/스모크도 judge/gate/run-judge 무참조 + 서버리스(loopback 금지)."""
    src = Path(la.__file__).read_text(encoding="utf-8")
    assert "def _builder_selftest_js" in src and "def _builder_smoke_js" in src
    assert "vitest" in src  # JS 확장 존재
    for forbidden in ("haetae.judge", "haetae.gate", "GateResult", "run_judge", "CompositeGate"):
        assert forbidden not in src, f"local_agent이 {forbidden} 참조(분리 위반)"
    # 서버리스(#128): 워크트리 내 node/vitest 실행만 — 서버 호스팅/loopback listen 없음
    for forbidden in ("127.0.0.1", "0.0.0.0", "http.server", ".listen(", "createServer"):
        assert forbidden not in src, f"local_agent이 {forbidden} 참조(서버리스 위반)"


@pytest.mark.skipif(
    os.environ.get("HAETAE_LOCAL_IT") != "1",
    reason="실 JS 빌더 통합은 opt-in (HAETAE_LOCAL_IT=1; node+vitest 필요)",
)
def test_selftest_js_live_self_consistency(tmp_path):
    """라이브 JS: 실 로컬 빌더 + JS 자기-테스트로 vitest-green 모듈+테스트를 만든다(node/vitest 있으면)."""
    endpoint = os.environ.get("HAETAE_LOCAL_ENDPOINT", "http://100.70.109.50:8089/v1")
    model = os.environ.get("HAETAE_LOCAL_MODEL", "qwen2.5-coder:7b")
    (tmp_path / "package.json").write_text('{"name":"g","scripts":{"test":"vitest run"}}\n')
    ex = LocalAgentExecutor(
        endpoint=endpoint, model=model, workdir=str(tmp_path), max_turns=4,
        verify=la.builder_smoke, selftest=la.builder_selftest,
    )
    order = NextOrder(
        unit="it_js",
        goal="src/calc.ts에 add(a,b)를 구현하고 tests/calc.test.ts에 vitest로 add(2,3)===5를 검증하라.",
        local_checks=[Check(type=CheckType.test, cmd="npx vitest run")],
        executor="local",
        deliverable="calc 모듈 + vitest 테스트",
    )
    ex.run(order)
    assert (tmp_path / "src" / "calc.ts").exists()
