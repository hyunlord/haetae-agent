"""control/data-plane 분리 + artifact descriptor (WO#102, OMC #3 — §7 #55 일반화).

state.yaml(control-plane = plan·유닛상태·이벤트요약)에 큰 산출물(trace·transcript·...)을
인라인하면 비대해져 파싱비용·오염이 생긴다. OMC 패턴대로 **작으면 인라인, 크면 data-plane
파일 + descriptor 참조**로 나눈다("bounded-handoff"). #55 heartbeat 사이드카·#67 transcript
사이드카가 이미 분리를 시작했고, 여기서 *재사용 가능한* descriptor 인프라로 일반화한다.

핵심 불변(안전):
  - **판정 불변**: 오프로드는 *지속(직렬화)* 단계에서만 한다(offload_state_artifacts는 dump된
    dict에만 작동, in-memory state 무변경). 행동 판정(적대 run-judge)은 캡처 직후 in-memory full
    trace로 수행되므로 출처가 바뀌지 않는다. reader는 *동일 내용*을 해소해 받는다 → 결과 동일.
  - **back-compat 추가형**: descriptor는 새 표현일 뿐. 인라인 state.yaml은 그대로 읽힌다
    (resolve가 인라인 우선). 강제 마이그레이션 없음(신규 run·임계 초과 시에만 descriptor).
  - **무결성·graceful**: content_hash로 파일-descriptor 드리프트 탐지. 누락/손상 → 명확 에러,
    크래시 0(reader는 graceful 폴백).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from haetae.models import ArtifactDescriptor, RunEvidence

# bounded-handoff 임계(바이트). 미만 → 인라인 유지(기존 동작). 이상 → data-plane 파일 + descriptor.
# configurable(호출부 override 가능). 8KB: 작은 trace는 그대로, 큰 sim:trace JSON만 빠진다.
ARTIFACT_INLINE_THRESHOLD = 8192


class ArtifactError(Exception):
    """artifact 해소 실패의 베이스(누락/손상)."""


class ArtifactMissing(ArtifactError):
    """descriptor가 가리키는 data-plane 파일이 없음."""


class ArtifactCorrupt(ArtifactError):
    """파일 내용이 descriptor.content_hash와 불일치(드리프트/손상)."""


def compute_hash(text: str) -> str:
    """내용 해시("sha256:<hex>"). 무결성 검증·idempotent 파일 id 둘 다에 쓴다."""
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _digest(text: str, kind: str, *, head: int = 180) -> str:
    """인라인 표시용 짧은 요약(head 다이제스트) — 파일 미해소 환경서도 *무엇*인지 보이게."""
    h = text[:head].replace("\n", " ").strip()
    suffix = "…" if len(text) > head else ""
    return f"[{kind} {len(text)}B] {h}{suffix}"


def write_artifact(
    base_dir: str | Path,
    kind: str,
    content: str,
    *,
    retention: str = "keep",
    created: str | None = None,
) -> ArtifactDescriptor:
    """content를 `<base_dir>/artifacts/<kind>/<id>.json`에 기록하고 descriptor를 반환.

    id는 content_hash 기반(같은 내용→같은 파일) → **idempotent**(반복 저장이 재기록 안 함).
    base_dir는 보통 run-dir(state.yaml의 부모). descriptor.path는 base_dir *상대*(이식성).
    """
    h = compute_hash(content)
    ident = h.split(":", 1)[1][:16]
    rel = f"artifacts/{kind}/{ident}.json"
    p = Path(base_dir) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    # idempotent: 이미 같은 해시 파일이 있으면 재기록 생략(반복 _save_state 비용 0).
    if not (p.exists() and compute_hash(p.read_text(encoding="utf-8")) == h):
        p.write_text(content, encoding="utf-8")
    return ArtifactDescriptor(
        path=rel,
        kind=kind,
        content_hash=h,
        size_bytes=len(content.encode("utf-8")),
        created=created,
        retention=retention,
        summary=_digest(content, kind),
    )


def resolve_artifact(descriptor: ArtifactDescriptor, base_dir: str | Path) -> str:
    """descriptor가 가리키는 data-plane 파일을 읽어 *원본 내용*을 반환(해시 검증).

    누락 → ArtifactMissing, 해시 불일치 → ArtifactCorrupt. 호출부가 graceful 폴백할 수 있게
    예외로 명확히 신호한다(크래시 0은 호출부 책임 — evidence_trace 참고).
    """
    p = Path(base_dir) / descriptor.path
    if not p.exists():
        raise ArtifactMissing(f"artifact 파일 없음: {descriptor.path}")
    content = p.read_text(encoding="utf-8")
    if compute_hash(content) != descriptor.content_hash:
        raise ArtifactCorrupt(
            f"content_hash 불일치(드리프트/손상): {descriptor.path}"
        )
    return content


def evidence_trace(
    ev: RunEvidence, base_dir: str | Path, *, graceful: bool = True
) -> str:
    """RunEvidence의 trace를 해소 — **인라인 우선**, 없으면 descriptor 해소(해시 검증).

    판정/표시가 *동일 내용*을 받는 공용 경로. graceful=True(기본)면 해소 실패 시 descriptor.summary로
    폴백(크래시 0). graceful=False면 ArtifactError를 올린다(무결성 강제 검증용).
    """
    inline = ev.trace or ""
    if inline:
        return inline
    desc = ev.trace_artifact
    if desc is None:
        return ""
    try:
        return resolve_artifact(desc, base_dir)
    except ArtifactError:
        if graceful:
            return desc.summary
        raise


def _is_run_evidence_dict(node: dict) -> bool:
    """dump된 dict가 RunEvidence 모양인지(booted+trace 동시 보유) — 위치 무관 탐지."""
    return (
        "booted" in node
        and isinstance(node.get("trace"), str)
        and not node.get("trace_artifact")
    )


def offload_state_artifacts(
    dumped: dict,
    base_dir: str | Path,
    *,
    threshold: int = ARTIFACT_INLINE_THRESHOLD,
) -> dict:
    """state dump(dict)을 walk해 임계 초과 RunEvidence.trace를 data-plane로 빼고 descriptor로 치환.

    **dumped dict에만 작동** — in-memory State 객체는 안 건드린다(판정·replan·재개의 in-memory
    경로 무영향). RunEvidence가 어디 중첩돼 있든(events·checks·gateresult 파생) 구조로 탐지한다.
    임계 미만 trace는 그대로 인라인(기존 동작·back-compat). 반환은 *치환된* 같은 dict.
    """
    def walk(node: object) -> None:
        if isinstance(node, dict):
            if _is_run_evidence_dict(node):
                t = node["trace"]
                if len(t.encode("utf-8")) >= threshold:
                    desc = write_artifact(base_dir, "trace", t)
                    node["trace"] = ""  # data-plane로 이동 — 인라인 비움
                    node["trace_artifact"] = desc.model_dump(mode="json")
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(dumped)
    return dumped
