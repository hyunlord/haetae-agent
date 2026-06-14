"""WO#102 — control/data-plane 분리 + artifact descriptor (OMC #3, 1차: trace) 테스트.

state.yaml에 큰 trace를 인라인하던 걸 data-plane 파일 + descriptor 참조로 분리. 핵심 불변:
**판정 불변**(reader가 동일 내용 해소) · back-compat(인라인 그대로 읽힘) · 무결성/graceful.
오프로드는 *직렬화(_save_state)* 단계에서만 — in-memory 판정 경로 무접촉.
"""

from pathlib import Path

import pytest

from haetae.artifacts import (
    ARTIFACT_INLINE_THRESHOLD,
    ArtifactCorrupt,
    ArtifactMissing,
    compute_hash,
    evidence_trace,
    offload_state_artifacts,
    resolve_artifact,
    write_artifact,
)
from haetae.gate import CompositeGate
from haetae.llm import MockClient
from haetae.loop import _save_state
from haetae.models import (
    ArtifactDescriptor,
    CheckType,
    ProjectSpec,
    RunEvidence,
    State,
    Verdict,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
JUDGE_PROMPT = REPO_ROOT / "prompts" / "judge.md"
RUN_JUDGE_PROMPT = REPO_ROOT / "prompts" / "run_judge.md"

_BIG = '{"wall_crossings": 0, "overlap_pairs": 0, "pad": "' + "x" * 9000 + '"}'  # > 8KB
_SMALL = '{"wall_crossings": 0, "overlap_pairs": 0}'  # < 8KB


# ════════════════════ 1. descriptor 인프라 round-trip ════════════════════


def test_write_resolve_round_trip(tmp_path):
    """큰 내용 기록 → descriptor → resolve_artifact == 원본. content_hash 일치."""
    desc = write_artifact(tmp_path, "trace", _BIG)
    assert desc.kind == "trace"
    assert desc.size_bytes == len(_BIG.encode("utf-8"))
    assert desc.content_hash == compute_hash(_BIG)
    assert (tmp_path / desc.path).exists()
    assert desc.path.startswith("artifacts/trace/")
    assert resolve_artifact(desc, tmp_path) == _BIG  # 동일 내용
    assert desc.summary  # 인라인 표시용 다이제스트 존재


def test_write_is_idempotent(tmp_path):
    """같은 내용 → 같은 경로(content-hash id) → 재기록 안 함(반복 저장 비용 0)."""
    d1 = write_artifact(tmp_path, "trace", _BIG)
    d2 = write_artifact(tmp_path, "trace", _BIG)
    assert d1.path == d2.path and d1.content_hash == d2.content_hash


def test_descriptor_reusable_for_transcript_kind(tmp_path):
    """재사용 인프라: 같은 헬퍼가 transcript 등 다른 kind에도 동형 작동(2차 적용 토대)."""
    desc = write_artifact(tmp_path, "transcript", _BIG, retention="expire")
    assert desc.kind == "transcript" and desc.retention == "expire"
    assert resolve_artifact(desc, tmp_path) == _BIG


# ════════════════════ 2. 무결성 / graceful ════════════════════


def test_resolve_missing_file_raises(tmp_path):
    desc = ArtifactDescriptor(path="artifacts/trace/nope.json", kind="trace",
                              content_hash="sha256:00", size_bytes=1)
    with pytest.raises(ArtifactMissing):
        resolve_artifact(desc, tmp_path)


def test_resolve_corrupt_hash_raises(tmp_path):
    """파일 내용이 descriptor 해시와 불일치(드리프트/손상) → ArtifactCorrupt."""
    desc = write_artifact(tmp_path, "trace", _BIG)
    (tmp_path / desc.path).write_text("tampered", encoding="utf-8")  # 변조
    with pytest.raises(ArtifactCorrupt):
        resolve_artifact(desc, tmp_path)


def test_evidence_trace_graceful_on_missing(tmp_path):
    """evidence_trace(graceful=True): 해소 실패 시 descriptor.summary 폴백(크래시 0)."""
    desc = ArtifactDescriptor(path="artifacts/trace/gone.json", kind="trace",
                              content_hash="sha256:00", size_bytes=1, summary="[trace] digest")
    ev = RunEvidence(booted=True, trace="", trace_artifact=desc)
    assert evidence_trace(ev, tmp_path) == "[trace] digest"      # 폴백
    with pytest.raises(ArtifactMissing):
        evidence_trace(ev, tmp_path, graceful=False)             # 강제 검증 모드


# ════════════════════ 3. evidence_trace 해소 (인라인 우선·descriptor 폴백) ════════════════════


def test_evidence_trace_inline_priority(tmp_path):
    """인라인 trace가 있으면 그대로(back-compat) — descriptor 무관."""
    ev = RunEvidence(booted=True, trace=_SMALL)
    assert evidence_trace(ev, tmp_path) == _SMALL


def test_evidence_trace_resolves_descriptor(tmp_path):
    """인라인 비고 descriptor 있으면 파일 해소 == 원본(동일 내용)."""
    desc = write_artifact(tmp_path, "trace", _BIG)
    ev = RunEvidence(booted=True, trace="", trace_artifact=desc)
    assert evidence_trace(ev, tmp_path) == _BIG


# ════════════════════ 4. offload (bounded-handoff: 작으면 인라인·크면 descriptor) ════════════════════


def _state_with_trace(trace: str) -> State:
    from haetae.models import CheckReport
    ev = RunEvidence(booted=True, trace=trace)
    cr = CheckReport(ac_id="ac1", check_type=CheckType.run, status="pass", run_evidence=ev)
    return State.model_validate({
        "spec_ref": "spec.yaml", "spec_version": 1, "status": "running",
        "events": [{"seq": 1, "checks": [cr.model_dump(mode="json")]}],
    })


def test_offload_large_trace_to_dataplane(tmp_path):
    """큰 trace → data-plane 파일 + descriptor(state dict 인라인 비움)."""
    dumped = _state_with_trace(_BIG).model_dump(by_alias=True, mode="json")
    out = offload_state_artifacts(dumped, tmp_path)
    re = out["events"][0]["checks"][0]["run_evidence"]
    assert re["trace"] == ""                       # 인라인 비움
    assert re["trace_artifact"] is not None        # descriptor 부착
    assert re["trace_artifact"]["kind"] == "trace"
    assert (tmp_path / re["trace_artifact"]["path"]).exists()


def test_offload_small_trace_stays_inline(tmp_path):
    """작은 trace는 인라인 유지(임계 미만 → 기존 동작·descriptor 없음)."""
    dumped = _state_with_trace(_SMALL).model_dump(by_alias=True, mode="json")
    out = offload_state_artifacts(dumped, tmp_path)
    re = out["events"][0]["checks"][0]["run_evidence"]
    assert re["trace"] == _SMALL                   # 그대로
    assert not re.get("trace_artifact")            # 미부착


def test_save_state_offloads_and_reloads_identical(tmp_path):
    """_save_state: 큰 trace run → state.yaml엔 descriptor(인라인 아님), 재로드+해소 == 원본."""
    state = _state_with_trace(_BIG)
    sp = tmp_path / "state.yaml"
    _save_state(state, sp)
    raw = sp.read_text(encoding="utf-8")
    assert _BIG not in raw                          # 큰 trace가 state.yaml에 인라인 안 됨
    assert "trace_artifact" in raw and "artifacts/trace/" in raw
    # 재로드 → evidence_trace 해소 == 원본(판정이 받는 것과 동일 내용)
    reloaded = State.from_yaml(sp)
    ev = reloaded.events[0].checks[0].run_evidence
    assert ev.trace == "" and ev.trace_artifact is not None
    assert evidence_trace(ev, tmp_path) == _BIG     # 동일 내용 복원


def test_save_state_small_trace_inline_backcompat(tmp_path):
    """작은 trace는 _save_state서도 인라인 — 기존 state.yaml 표현 무변경(back-compat)."""
    sp = tmp_path / "state.yaml"
    _save_state(_state_with_trace(_SMALL), sp)
    raw = sp.read_text(encoding="utf-8")
    # 인라인 유지 + descriptor 미생성(필드는 스키마상 `trace_artifact: null`로만 존재).
    assert _SMALL in raw
    assert "artifacts/trace/" not in raw          # data-plane 파일 미참조
    assert not (tmp_path / "artifacts").exists()   # 파일 미기록


def test_save_state_in_memory_unchanged(tmp_path):
    """오프로드는 dump dict에만 — in-memory State는 full trace 보존(판정/replan 경로 불변)."""
    state = _state_with_trace(_BIG)
    _save_state(state, tmp_path / "state.yaml")
    assert state.events[0].checks[0].run_evidence.trace == _BIG  # in-memory 그대로


# ════════════════════ 5. back-compat: 인라인 state.yaml 읽힘 ════════════════════


def test_backcompat_inline_state_reads(tmp_path):
    """descriptor 없는 *기존* 인라인 state.yaml도 그대로 로드·해소(강제 마이그레이션 없음)."""
    sp = tmp_path / "state.yaml"
    # 구 표현: run_evidence.trace 인라인, trace_artifact 없음
    state = _state_with_trace(_SMALL)
    import yaml
    sp.write_text(yaml.safe_dump(state.model_dump(by_alias=True, mode="json"), allow_unicode=True),
                  encoding="utf-8")
    reloaded = State.from_yaml(sp)
    ev = reloaded.events[0].checks[0].run_evidence
    assert evidence_trace(ev, tmp_path) == _SMALL   # 인라인 우선 — 파일 불필요


# ════════════════════ 6. 판정 불변 (gate/run-judge가 동일 내용으로 동일 판정) ════════════════════


def _gate(tmp_path, client=None) -> CompositeGate:
    return CompositeGate(
        workdir=tmp_path, judge_client=client,
        judge_prompt_path=JUDGE_PROMPT, run_judge_prompt_path=RUN_JUDGE_PROMPT,
        run_timeout=10, install_deps=False,
    )


def _run_spec(cmd: str) -> ProjectSpec:
    return ProjectSpec.model_validate({
        "spec_id": "ad-judge", "version": 1, "order_raw": "x", "goal": "g",
        "task_type": "feature_impl", "verifiability": "objective", "mode": "normal",
        "acceptance_criteria": [{"id": "ac1", "desc": "동선", "unit": "u1",
            "check": {"type": "run", "cmd": cmd, "pass": "ok"}}],
        "non_goals": ["n"], "done_when": "ac1",
        "decomposition": [{"unit": "u1", "desc": "엔진"}],
    })


def test_judgment_unchanged_large_trace(tmp_path):
    """gate.judge는 in-memory full trace로 판정 — 큰 trace여도 결과 동일(오프로드는 저장 단계만).

    run-judge가 행동 fail을 내면 그대로 fail이 잡힌다(descriptor와 무관 — 판정 경로 무접촉).
    """
    big_echo = "echo '" + _BIG.replace("'", "") + "'"
    spec = _run_spec(big_echo)
    client = MockClient("verdicts:\n  - ac_id: ac1\n    status: fail\n    reason: 그리드락\n")
    gr = _gate(tmp_path, client=client).judge("결과", spec, unit="u1")
    rep = [c for c in gr.checks if c.ac_id == "ac1"][0]
    assert rep.status == "fail" and "그리드락" in rep.detail  # 행동 판정 그대로
    # 그리고 그 큰 trace는 저장 시 data-plane로 빠진다(판정엔 무영향) — 직렬화 라운드트립 확인.
    assert rep.run_evidence is not None and len(rep.run_evidence.trace) >= ARTIFACT_INLINE_THRESHOLD
    dumped = offload_state_artifacts(
        {"e": {"booted": rep.run_evidence.booted, "trace": rep.run_evidence.trace}}, tmp_path)
    assert dumped["e"]["trace"] == "" and dumped["e"]["trace_artifact"]
