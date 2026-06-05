"""RunHarness 테스트 — 실제 앱 없이 소형 인라인 스크립트로 캡처/타임아웃/never-raise 검증."""

import sys

from haetae.run_harness import MAX_TRACE_BYTES, run_artifact

PY = sys.executable


def _py(code: str) -> str:
    """인라인 파이썬 스크립트를 셸 cmd로(작은따옴표는 안 쓰는 코드만)."""
    return f'{PY} -c "{code}"'


# ──────────────────────────── 정상 부팅 + trace ────────────────────────────


def test_booted_with_json_trace(tmp_path):
    # 따옴표 없는 JSON 배열을 방출(셸 이스케이프 회피) → trace로 그대로 캡처되는지.
    ev = run_artifact(_py("import json; print(json.dumps([1, 2, 3]))"),
                      workdir=tmp_path, timeout=10)
    assert ev.booted is True
    assert ev.exit_code == 0
    assert ev.timed_out is False
    assert "[1, 2, 3]" in ev.trace  # stdout(=trace) 캡처
    assert ev.reason is None
    assert ev.duration_s >= 0


# ──────────────────────────── 크래시(exit≠0) ────────────────────────────


def test_crash_is_not_booted_captures_stderr(tmp_path):
    ev = run_artifact(_py("import sys; sys.stderr.write(chr(66)+'oom'); sys.exit(1)"),
                      workdir=tmp_path, timeout=10)
    assert ev.booted is False
    assert ev.exit_code == 1
    assert ev.timed_out is False
    assert "Boom" in ev.stderr_tail
    assert ev.reason and "exit 1" in ev.reason


# ──────────────────────────── 타임아웃(hang) ────────────────────────────


def test_hang_times_out_without_raising(tmp_path):
    # 5초 sleep을 0.3초 timeout으로 → timed_out, raise 없음.
    ev = run_artifact(_py("import time; time.sleep(5)"), workdir=tmp_path, timeout=0.3)
    assert ev.timed_out is True
    assert ev.booted is False
    assert ev.exit_code is None
    assert ev.reason and "timeout" in ev.reason


# ──────────────────────────── never-raise (bad cwd/cmd) ────────────────────────────


def test_bad_workdir_is_absorbed_not_raised(tmp_path):
    missing = tmp_path / "does_not_exist"
    ev = run_artifact(_py("print(1)"), workdir=missing, timeout=10)
    assert ev.booted is False  # 예외를 흡수해 부팅 실패로 기록
    assert ev.reason  # 사유 기록


def test_nonzero_from_missing_command_is_absorbed(tmp_path):
    # 존재하지 않는 명령 → 셸이 비정상 종료. raise 없이 booted=False.
    ev = run_artifact("haetae_no_such_cmd_xyz --nope", workdir=tmp_path, timeout=10)
    assert ev.booted is False
    assert ev.exit_code is not None and ev.exit_code != 0


# ──────────────────────────── 출력 상한(cap) ────────────────────────────


def test_trace_output_is_capped(tmp_path):
    # 매우 큰 stdout → trace가 상한 이내로 잘리고 truncated 표식이 붙는다.
    ev = run_artifact(_py("print('x'*300000)"), workdir=tmp_path, timeout=10)
    assert ev.booted is True
    assert len(ev.trace.encode("utf-8")) <= MAX_TRACE_BYTES + 64  # cap + 표식 여유
    assert "truncated" in ev.trace
