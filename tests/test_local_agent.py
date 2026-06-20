"""LocalAgentExecutor 테스트 (WO#137) — 약한 로컬 모델 빌더 provider.

엔드포인트는 *모킹*(post_chat monkeypatch)이라 CI에서 라이브 불요. 라이브 통합은 opt-in
(HAETAE_LOCAL_IT=1). **적대 분리 단언**: 로컬 provider가 judge/run-judge/gate/critic에
안 닿음(구조적·소스 양쪽).
"""

from __future__ import annotations

import os
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
    """적대 분리: collect-only는 *import 가능 여부*만 본다 — 실패하는 테스트도 *실행/채점 안 함*."""
    (tmp_path / "tests").mkdir()
    # import은 되지만 실행하면 FAIL할 테스트 → collect는 성공(None), 채점 안 함
    (tmp_path / "tests" / "test_x.py").write_text("def test_would_fail():\n    assert False\n")
    assert la.builder_smoke(tmp_path) is None


def test_builder_smoke_skips_when_pytest_absent(tmp_path, monkeypatch):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("def test_a():\n    assert True\n")
    monkeypatch.setattr(la, "_smoke_run", lambda cmd, cwd, timeout: (1, "No module named pytest"))
    assert la.builder_smoke(tmp_path) is None  # 툴 부재 → skip(빌드 막지 않음)


def test_builder_smoke_collect_error_via_seam(tmp_path, monkeypatch):
    (tmp_path / "m.py").write_text("y = 2\n")
    monkeypatch.setattr(
        la, "_smoke_run",
        lambda cmd, cwd, timeout: (2, "ImportError: cannot import name 'winner' from 'rules'"),
    )
    err = la.builder_smoke(tmp_path)
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
