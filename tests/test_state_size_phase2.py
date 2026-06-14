"""WO#109 — OMC #3 phase 2: state.yaml 크기 측정-결과 박제 (trace가 지배적·정직 no-op).

#102가 RunEvidence.trace(지배적 단일 블롭)를 data-plane로 뺐다. phase 2 측정 결과:
post-#102로 state.yaml에서 8KB 임계를 넘는 *단일 write-once 블롭은 0건*이고, WO가 1순위로
지목한 `prompt`(work order 전체 텍스트) 필드는 state.yaml에 *존재하지 않는다*(Event엔 짧은
`work_order_ref` 참조만). 따라서 새 오프로드 코드를 더하지 않고(speculative 금지), 그 측정
사실을 여기 박제한다. 더해서 artifacts 인프라가 *미래의* 어떤 write-once 블롭에도 재사용
가능함(kind-범용)을 round-trip으로 증명한다 = phase-2 readiness.

전부 판정 무관·back-compat·코드 무변경(이 파일과 docs/STATE_SIZE_PHASE2.md만 추가).
측정 상세는 docs/STATE_SIZE_PHASE2.md 참고.
"""

from haetae.artifacts import (
    ARTIFACT_INLINE_THRESHOLD,
    compute_hash,
    offload_state_artifacts,
    resolve_artifact,
    write_artifact,
)
from haetae.models import State

# 대표 완주 run(crowdsim-ns·kanban-3of3)의 *모양*을 본뜬 합성 State(헤르메틱 — scratch 비의존).
# 이벤트별 result(~수백 B), 작은 trace(<8KB), cost_parts 레저, critique/transitions 포함.
_SMALL_TRACE = '{"wall_crossings": 0, "overlap_pairs": 0}'  # <8KB → #102가 인라인 유지
_RESULT = "유닛 u%d 빌드 완료. " + "산출물 동선/충돌 회피 구현. " * 20  # ~수백 B write-once


def _representative_state() -> State:
    events = []
    for i in range(9):  # crowdsim 규모(9 이벤트)
        events.append({
            "seq": i,
            "unit": f"u{i}",
            "work_order_ref": "resume(parent-done)" if i % 3 == 0 else f"reuse_of=p{i}",
            "result": _RESULT % i,
            "learnings": None,
            "checks": [{
                "ac_id": f"ac{i}", "check_type": "run", "status": "pass",
                "run_evidence": {"booted": True, "trace": _SMALL_TRACE},
            }],
        })
    cost_parts = [{"tokens": 100000 + i, "source": "executor", "tier": "high",
                   "kind": "build", "unit": f"u{i % 9}"} for i in range(35)]
    transitions = [{"stage": s, "unit": f"u{i}", "ts": None}
                   for i, s in enumerate(["synth", "build", "verify", "replan"] * 9)]
    return State.model_validate({
        "spec_ref": "spec.yaml", "spec_version": 1, "status": "done",
        "plan": [{"unit": f"u{i}", "state": "done"} for i in range(8)],
        "events": events,
        "cost_parts": cost_parts,
        "transitions": transitions,
        "spec_critique": {"verdict": "adequate", "gaps": [], "note": "x" * 1500},
    })


def _dump_post_102(state: State, base_dir) -> dict:
    """_save_state와 동일 경로(dump → offload). post-#102 직렬화 표현."""
    return offload_state_artifacts(state.model_dump(by_alias=True, mode="json"), base_dir)


def _string_leaves(node, path=""):
    """dict를 walk하며 (leaf_name, byte_size)를 산출."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _string_leaves(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _string_leaves(v, f"{path}[{i}]")
    elif isinstance(node, str):
        leaf = path.rsplit(".", 1)[-1].split("[")[0]
        yield leaf, len(node.encode("utf-8"))


# ════════════════════ 1. 측정 박제: post-#102에 8KB 넘는 단일 write-once 블롭 0건 ════════════════════


def test_no_writeonce_blob_over_threshold_post_102(tmp_path):
    """대표 State를 #102 경로로 직렬화 → trace 외 단일 문자열 leaf가 8KB를 넘지 않음.

    measure: result/learnings 등 write-once 후보 어느 것도 임계 미만 → descriptor 적용 대상 없음.
    """
    dumped = _dump_post_102(_representative_state(), tmp_path)
    over = [(leaf, sz) for leaf, sz in _string_leaves(dumped)
            if leaf != "trace" and sz >= ARTIFACT_INLINE_THRESHOLD]
    assert over == [], f"임계 초과 write-once 블롭(예상 없음): {over}"
    # write-once 후보(result)는 존재하되 임계 한참 미만임을 양성 확인(공허참 방지).
    results = [sz for leaf, sz in _string_leaves(dumped) if leaf == "result"]
    assert results and max(results) < ARTIFACT_INLINE_THRESHOLD


def test_events_is_dominant_but_append_only(tmp_path):
    """events가 최대 기여 필드지만 append-only 리스트 → 단일 descriptor 부적합(WO 제외 사유).

    개별 이벤트 leaf는 임계 미만 → 통째 오프로드는 매 append마다 전체 재기록(비효율). 인라인 유지가 정답.
    """
    import yaml
    dumped = _dump_post_102(_representative_state(), tmp_path)

    def field_bytes(k):
        return len(yaml.safe_dump({k: dumped[k]}, allow_unicode=True, sort_keys=False).encode("utf-8"))

    sizes = {k: field_bytes(k) for k in dumped}
    assert max(sizes, key=sizes.get) == "events"          # 지배 필드
    assert isinstance(dumped["events"], list)             # append-only 타임라인
    # 그 안의 어떤 단일 문자열 leaf도 임계 미만(통째가 아니라 다수 작은 레코드의 누적).
    ev_leaves = [sz for _, sz in _string_leaves(dumped["events"])]
    assert ev_leaves and max(ev_leaves) < ARTIFACT_INLINE_THRESHOLD


# ════════════════════ 2. 측정 박제: `prompt` 필드 부재(WO 1순위 후보가 state에 없음) ════════════════════


def test_state_has_no_prompt_blob_only_short_ref():
    """state.yaml엔 work order *전체 텍스트*(prompt)가 없다 — Event엔 짧은 work_order_ref만.

    WO가 1순위로 지목한 prompt 블롭은 존재하지 않으므로 descriptor를 걸 대상 자체가 없다(정직 보고).
    """
    from haetae.models import Event
    assert "prompt" not in State.model_fields           # State 어디에도 prompt 필드 없음
    assert "prompt" not in Event.model_fields
    fld = Event.model_fields["work_order_ref"]           # 존재하는 건 짧은 *참조*뿐
    assert fld.annotation == (str | None)
    # 대표 State 직렬화 어디에도 prompt 키 없음.
    dumped = _representative_state().model_dump(by_alias=True, mode="json")
    assert "prompt" not in str(dumped.keys())


# ════════════════════ 3. phase-2 readiness: 인프라가 미래 write-once 블롭에 kind-범용 ════════════════════


def test_artifacts_infra_kind_general_for_future_blobs(tmp_path):
    """artifacts.py는 이미 kind-범용 — *미래에* prompt/result/cost가 8KB를 넘으면 재사용만으로 적용.

    새 오프로드 코드 없이도(speculative 금지) 인프라 readiness를 round-trip+해시로 증명한다.
    """
    big = "X" * (ARTIFACT_INLINE_THRESHOLD + 1000)
    for kind in ("prompt", "result", "cost"):
        desc = write_artifact(tmp_path, kind, big)
        assert desc.kind == kind
        assert desc.size_bytes == len(big.encode("utf-8"))
        assert desc.content_hash == compute_hash(big)
        assert resolve_artifact(desc, tmp_path) == big   # 동일 내용 복원(해시 검증)
