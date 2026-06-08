"""능력 획득 거버넌스 — 큐레이션 발견 + POC + 사람 승인 + provenance (WO#53 Phase F.1).

빌드가 *없는 능력*(라이브러리/툴)을 필요로 할 때, **거버넌스 하에** 능력을 발견·검증·채택한다:

    능력 요청(gap) → 발견(큐레이션 후보) → POC(증거) → 사람 승인(escalate) → 채택(+provenance)

핵심 안전 불변(이 모듈이 강제):
  1. **자동 채택 절대 없음.** 발견·POC는 자동이나 *신뢰 결정(채택)은 사람*. 승인(allowlist)된
     후보에만 provenance를 만들고, 미승인은 escalation(사람 검토 대기)으로만 surface한다.
  2. **큐레이션 소스만(F.1).** in-repo 검증된 `capabilities/*.yaml` 엔트리만. **이 모듈엔
     인터넷 검색·임의 fetch·subprocess·네트워크 import가 *없다*** (넓은 검색=F.2, 코드실행=F.3).
  3. **executor sandbox 무관.** 이 모듈은 executor에 네트워크를 주지 않는다(데이터 처리만).
  4. **best-effort.** 로드/발견/POC 실패는 흡수 — 절대 raise하지 않는다.

POC(F.1): 기본은 **메타데이터 증거**(코드 *미실행* — 안전). 후보의 선언된 import/설치 needs를
증거로 캡처하되 ok=None(미실행)로 둔다. *라이브 기능 스모크*(실제 import/실행)는 주입 runner로
열어두되(=F.1b), 이 모듈 자체는 어떤 후보 코드도 실행하지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import yaml

from haetae.models import (
    CapabilityCandidate,
    CapabilityPOC,
    CapabilityProvenance,
    CapabilityRequest,
)

CAPABILITY_GLOB = "*.yaml"


@dataclass(frozen=True)
class CapabilityEntry:
    """큐레이션 레지스트리 엔트리 — in-repo 검증된 하나의 능력.

    capability: 능력 키(예: "pathfinding"). keywords: 요청 매칭 키워드(소문자 정규화).
    identifier: 패키지/툴 식별자. ecosystem: npm|pip|tool. source/license: provenance용.
    install: host-side 설치 명령(채택 *실행*은 F.1b — 여기선 데이터로만 보유, 실행 안 함).
    imports: POC가 참조할 모듈명(증거). note: 사람이 읽는 설명.
    """

    capability: str
    keywords: tuple[str, ...]
    identifier: str
    ecosystem: str
    source: str
    license: str
    install: tuple[str, ...] = ()
    imports: tuple[str, ...] = ()
    note: str = ""


# POC runner 시그니처(주입형, F.1b/테스트용): entry → CapabilityPOC. 기본 None=메타데이터 POC.
PocRunner = Callable[[CapabilityEntry], CapabilityPOC]


def _strs(v: object) -> tuple[str, ...]:
    if v is None:
        return ()
    if isinstance(v, str):
        return (v,)
    if isinstance(v, list):
        return tuple(str(x) for x in v if str(x).strip())
    return ()


def _parse_entry(data: object) -> CapabilityEntry | None:
    """YAML dict → CapabilityEntry. 필수(capability/identifier) 없으면 None(스킵)."""
    if not isinstance(data, dict):
        return None
    capability = str(data.get("capability") or "").strip()
    identifier = str(data.get("identifier") or "").strip()
    if not capability or not identifier:
        return None
    keywords = tuple(k.strip().lower() for k in _strs(data.get("keywords")) if k.strip())
    if not keywords:
        keywords = (capability.lower(),)  # 키워드 없으면 capability 자체로 매칭
    return CapabilityEntry(
        capability=capability,
        keywords=keywords,
        identifier=identifier,
        ecosystem=str(data.get("ecosystem") or "unknown").strip(),
        source=str(data.get("source") or "curated:unknown").strip(),
        license=str(data.get("license") or "unknown").strip(),
        install=_strs(data.get("install")),
        imports=_strs(data.get("imports")),
        note=str(data.get("note") or "").strip(),
    )


def load_capability_registry(registry_dir: str | Path | None) -> list[CapabilityEntry]:
    """`capabilities/*.yaml` 큐레이션 엔트리를 로드한다(읽기전용·best-effort).

    디렉토리 부재/접근 실패 → 빈 리스트. 개별 엔트리 파싱 실패 → 그것만 스킵. 절대 raise 안 함.
    결과는 파일명 정렬 → 결정적 순서. registry_dir=None → 빈 리스트(능력 없음).
    """
    if registry_dir is None:
        return []
    entries: list[CapabilityEntry] = []
    try:
        base = Path(registry_dir)
        if not base.is_dir():
            return []
        for f in sorted(base.glob(CAPABILITY_GLOB)):
            try:
                entry = _parse_entry(yaml.safe_load(f.read_text(encoding="utf-8")))
            except Exception:  # noqa: BLE001 — 깨진 엔트리 하나가 전체를 막지 않는다
                continue
            if entry is not None:
                entries.append(entry)
    except Exception:  # noqa: BLE001 — 레지스트리 로드는 run을 죽이지 않는다
        return []
    return entries


def discover(request: CapabilityRequest, registry: list[CapabilityEntry]) -> list[CapabilityEntry]:
    """요청의 capability(자유 텍스트)에 키워드가 (대소문자 무시) 매칭되는 엔트리들을 반환.

    무매칭 → 빈 리스트(=큐레이션에 후보 없음 → 사람 검토로 escalate). 결정적 순서(레지스트리 정렬).
    """
    text = (request.capability or "").lower()
    if not text:
        return []
    return [e for e in registry if any(kw in text or text in kw for kw in e.keywords)]


def to_candidate(entry: CapabilityEntry) -> CapabilityCandidate:
    """레지스트리 엔트리 → 직렬화 가능한 후보(escalation/provenance 표면화용)."""
    return CapabilityCandidate(
        capability=entry.capability,
        identifier=entry.identifier,
        ecosystem=entry.ecosystem,
        source=entry.source,
        license=entry.license,
        install=list(entry.install),
    )


def run_poc(entry: CapabilityEntry, *, runner: PocRunner | None = None) -> CapabilityPOC:
    """후보 POC 증거를 만든다(best-effort, **절대 raise 안 함**).

    runner 주입 시(F.1b/테스트): 라이브 스모크 결과를 받는다. runner 예외도 흡수(ok=False).
    runner 없으면(F.1 기본): **코드 미실행** 메타데이터 POC — 선언된 import/설치 needs만 증거로
    캡처하고 ok=None(미실행). 이 모듈은 어떤 후보 코드도 직접 실행하지 않는다(안전).
    """
    if runner is not None:
        try:
            poc = runner(entry)
            return poc if isinstance(poc, CapabilityPOC) else CapabilityPOC(
                identifier=entry.identifier, ok=False, detail="POC runner가 CapabilityPOC 미반환")
        except Exception as e:  # noqa: BLE001 — POC 실패는 흡수(루프 안 죽임)
            return CapabilityPOC(identifier=entry.identifier, ok=False, detail=f"POC runner 예외: {e}")
    needs = []
    if entry.install:
        needs.append(f"{entry.ecosystem} install: {' '.join(entry.install)}")
    return CapabilityPOC(
        identifier=entry.identifier,
        ok=None,  # 미실행(메타데이터만) — 라이브 기능 스모크는 F.1b runner 필요
        imports=list(entry.imports),
        needs=needs,
        detail="메타데이터 POC(코드 미실행) — 라이브 기능 스모크는 주입 runner(F.1b) 필요",
    )


def is_approved(entry: CapabilityEntry, allowlist: set[str] | list[str] | None) -> bool:
    """후보가 사람 승인 allowlist에 있는지(identifier 또는 capability 키로 매칭).

    allowlist는 *사람이 out-of-band로 승인*한 식별자 집합(재실행 시 주입). 비어 있으면 항상 False
    → 자동 채택 없음(승인 없이는 채택 경로 안 탐).
    """
    allow = {str(a).strip() for a in (allowlist or []) if str(a).strip()}
    return entry.identifier in allow or entry.capability in allow


def build_provenance(
    entry: CapabilityEntry, *, approved_by: str, approved_at: str, poc: CapabilityPOC | None = None
) -> CapabilityProvenance:
    """승인된 후보의 채택 provenance(무엇·출처·라이선스·누가·언제)."""
    return CapabilityProvenance(
        capability=entry.capability,
        identifier=entry.identifier,
        source=entry.source,
        license=entry.license,
        approved_by=approved_by,
        approved_at=approved_at,
        poc_ok=(poc.ok if poc is not None else None),
    )


def build_capability_escalation(
    unresolved: list[tuple[CapabilityRequest, list[tuple[CapabilityEntry, CapabilityPOC]]]],
) -> dict:
    """미승인 능력 요청들을 *구조화 escalation*으로 직렬화(후보+POC 증거+provenance-to-be).

    사람이 검토해 allowlist에 추가(승인)한 뒤 *재실행*하면 채택된다(out-of-band 승인).
    """
    reqs = []
    for req, cand_pocs in unresolved:
        candidates = []
        for entry, poc in cand_pocs:
            candidates.append({
                "candidate": to_candidate(entry).model_dump(mode="json"),
                "poc": poc.model_dump(mode="json"),
                "license": entry.license,
                "source": entry.source,
            })
        reqs.append({
            "capability": req.capability,
            "unit": req.unit,
            "why": req.reason,
            "candidates": candidates,
        })
    n = len(reqs)
    return {
        "reason": f"능력 획득 승인 대기 — 미승인 능력 요청 {n}건 (사람 검토 필요)",
        "capability_gate": True,
        "how_to_approve": (
            "후보·POC 증거·라이선스를 검토하고, 신뢰하면 식별자를 capability-allowlist에 "
            "추가해 *재실행*하라(out-of-band 승인). 자동 채택은 하지 않는다."
        ),
        "requests": reqs,
    }


@dataclass
class CapabilityOutcome:
    """preflight 결과 — 호출부(loop)가 escalate/진행을 결정하는 데 쓴다."""

    provenance: list[CapabilityProvenance] = field(default_factory=list)  # 승인되어 채택된 것
    escalation: dict | None = None  # 미승인 후보가 있으면 구조화 escalation(없으면 None)


def governed_capability_preflight(
    requests: list[CapabilityRequest],
    *,
    registry_dir: str | Path | None,
    allowlist: set[str] | list[str] | None,
    approved_at: str,
    poc_runner: PocRunner | None = None,
    approved_by: str = "human-allowlist",
) -> CapabilityOutcome:
    """능력 요청 → 발견 → POC → 승인 분기. 순수·best-effort(절대 raise 안 함).

    각 요청에 대해 큐레이션 후보를 발견하고:
      - **승인(allowlist)된 후보**가 있으면 → provenance 기록(채택 결정). 그 요청은 해소됨.
      - 승인된 후보가 없으면(미승인 후보 or 후보 없음) → escalation에 모은다(사람 검토 대기).
    반환 CapabilityOutcome: provenance(채택된 것) + escalation(미승인 있으면 dict, 없으면 None).
    **자동 채택 없음**: provenance는 *승인된 후보에만* 생기고, 미승인은 escalation으로만 surface.
    """
    registry = load_capability_registry(registry_dir)
    provenance: list[CapabilityProvenance] = []
    unresolved: list[tuple[CapabilityRequest, list[tuple[CapabilityEntry, CapabilityPOC]]]] = []
    for req in requests or []:
        candidates = discover(req, registry)
        approved = [e for e in candidates if is_approved(e, allowlist)]
        if approved:
            for entry in approved:
                poc = run_poc(entry, runner=poc_runner)
                provenance.append(build_provenance(
                    entry, approved_by=approved_by, approved_at=approved_at, poc=poc))
        else:
            # 미승인(또는 후보 0) → 후보별 POC 증거를 모아 사람 검토로 escalate.
            cand_pocs = [(e, run_poc(e, runner=poc_runner)) for e in candidates]
            unresolved.append((req, cand_pocs))
    escalation = build_capability_escalation(unresolved) if unresolved else None
    return CapabilityOutcome(provenance=provenance, escalation=escalation)
