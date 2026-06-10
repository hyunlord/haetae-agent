"""능력 획득 거버넌스 — 큐레이션 발견 + POC + 사람 승인 + provenance (WO#53 Phase F.1).

빌드가 *없는 능력*(라이브러리/툴)을 필요로 할 때, **거버넌스 하에** 능력을 발견·검증·채택한다:

    능력 요청(gap) → 발견(큐레이션 후보) → POC(증거) → 사람 승인(escalate) → 채택(+provenance)

핵심 안전 불변(이 모듈이 강제):
  1. **자동 채택 절대 없음.** 발견·POC는 자동이나 *신뢰 결정(채택)은 사람*. 승인(allowlist)된
     후보에만 provenance를 만들고, 미승인은 escalation(사람 검토 대기)으로만 surface한다.
  2. **이 모듈엔 인터넷 검색·임의 fetch·subprocess·네트워크 import가 *없다*.** 큐레이션 소스
     (`capabilities/*.yaml`)는 직접 로드하고, 인터넷 발견(F.2)은 *주입된* `CapabilitySearcher`로만
     한다 — 실제 네트워크는 별 모듈 `capability_search.py`에 격리(이 모듈은 인터페이스만 안다).
     코드 실행(F.3)·라이브 POC(F.1b)도 여기엔 없다.
  3. **executor sandbox 무관.** 이 모듈은 executor에 네트워크를 주지 않는다(데이터 처리만).
     인터넷 검색은 *director-side*(검색 모듈)라 ALLOWED_SANDBOXES와 무관.
  4. **best-effort.** 로드/발견/검색/POC 실패는 흡수 — 절대 raise하지 않는다.

F.2(인터넷 발견, discovery-only): opt-in 시 능력 요청을 인터넷 레지스트리에서도 검색해 **원격
후보**(`source="remote:<registry>"`)를 만들고 큐레이션 후보와 함께 기존 escalation에 surface한다.
**실행 0**(원격 후보는 install/import/run 안 함 — 메타데이터 POC, ok=None), **자동 채택 없음**
(allowlist 게이트 그대로). searcher 미주입이면 원격 후보 0(기존 F.1 동작과 완전 동일).

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

# 인터넷 발견 searcher 시그니처(F.2, 주입형): request → 원격 후보 원시 dict 리스트.
# **이 모듈은 인터페이스만 안다** — 실제 네트워크 구현은 capability_search.py에 격리(주입).
CapabilitySearcher = Callable[[CapabilityRequest], list]


def _is_remote(entry: CapabilityEntry) -> bool:
    """원격(인터넷 발견) 후보인가 — source가 `remote:`로 시작하면 미검증·정밀검토 대상."""
    return entry.source.startswith("remote:")


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


def _parse_remote_entry(data: object, request: CapabilityRequest) -> CapabilityEntry | None:
    """searcher가 돌려준 원시 dict → CapabilityEntry. **source는 항상 `remote:<registry>`로 표기.**

    identifier 없으면 None(스킵). registry는 dict의 registry/ecosystem에서, capability는 dict 또는
    요청에서. 네트워크 호출 없음(이미 검색된 결과를 정규화만). install/imports를 *데이터로만* 보유
    (실행 안 함 — 원격 후보 POC는 메타데이터 ok=None).
    """
    if not isinstance(data, dict):
        return None
    identifier = str(data.get("identifier") or data.get("name") or "").strip()
    if not identifier:
        return None
    registry = str(data.get("registry") or data.get("ecosystem") or "unknown").strip() or "unknown"
    capability = str(data.get("capability") or request.capability or "").strip() or identifier
    return CapabilityEntry(
        capability=capability,
        keywords=(capability.lower(),),
        identifier=identifier,
        ecosystem=str(data.get("ecosystem") or registry).strip() or "unknown",
        source=f"remote:{registry}",  # 명확 표기 — 큐레이션(curated:)과 구분, 정밀검토 신호
        license=str(data.get("license") or "unknown").strip() or "unknown",
        install=_strs(data.get("install")),
        imports=_strs(data.get("imports")),
        note=str(data.get("note") or "").strip(),
    )


def discover_remote(
    request: CapabilityRequest, *, searcher: CapabilitySearcher | None
) -> list[CapabilityEntry]:
    """인터넷 레지스트리에서 원격 후보를 발견한다(F.2, discovery-only·best-effort).

    searcher 미주입(None)이면 빈 리스트(off-by-default — 기존 F.1 동작과 완전 동일). 주입 시
    best-effort 호출 → 반환 dict들을 `source="remote:<registry>"` 엔트리로 정규화. searcher 예외·
    형식 불량은 흡수 → [](큐레이션-only 폴백). **네트워크 import 없음**(searcher가 주입됨).
    """
    if searcher is None:
        return []
    try:
        raw = searcher(request)
    except Exception:  # noqa: BLE001 — 검색 실패는 흡수(큐레이션-only 폴백, 절대 raise 안 함)
        return []
    if not isinstance(raw, list):
        return []
    out: list[CapabilityEntry] = []
    for item in raw:
        entry = _parse_remote_entry(item, request)
        if entry is not None:
            out.append(entry)
    return out


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
    remote_total = 0
    for req, cand_pocs in unresolved:
        candidates = []
        curated_n = remote_n = 0
        for entry, poc in cand_pocs:
            remote = _is_remote(entry)
            if remote:
                remote_n += 1
            else:
                curated_n += 1
            candidates.append({
                "candidate": to_candidate(entry).model_dump(mode="json"),
                "poc": poc.model_dump(mode="json"),
                "license": entry.license,
                "source": entry.source,
                # F.2: 사람이 출처로 신뢰도 판단 — 큐레이션(in-repo 검증) vs 원격(미검증·정밀검토).
                "trust": "remote-unverified" if remote else "curated-verified",
                "needs_scrutiny": remote,  # 원격 후보는 사람 정밀검토 필요(실행 0·메타데이터만)
            })
        remote_total += remote_n
        # 큐레이션을 앞, 원격(정밀검토 요)을 뒤로 정렬 — 검토 우선순위.
        candidates.sort(key=lambda c: 1 if c["needs_scrutiny"] else 0)
        reqs.append({
            "capability": req.capability,
            "unit": req.unit,
            "why": req.reason,
            "candidates": candidates,
            "curated_count": curated_n,
            "remote_count": remote_n,
        })
    n = len(reqs)
    remote_note = (
        f" 그중 원격(인터넷 발견·미검증) 후보 {remote_total}건은 *실행되지 않았으며*(메타데이터만) "
        "사람 정밀검토가 필요하다." if remote_total else ""
    )
    return {
        "reason": f"능력 획득 승인 대기 — 미승인 능력 요청 {n}건 (사람 검토 필요).{remote_note}",
        "capability_gate": True,
        "how_to_approve": (
            "후보·POC 증거·라이선스·source(curated vs remote)를 검토하고, 신뢰하면 식별자를 "
            "capability-allowlist에 추가해 *재실행*하라(out-of-band 승인). 자동 채택은 하지 않는다. "
            "원격(remote:) 후보는 미검증이니 더 신중히 — 실행되지 않았고 메타데이터 증거뿐이다."
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
    searcher: CapabilitySearcher | None = None,
) -> CapabilityOutcome:
    """능력 요청 → 발견(큐레이션+원격) → POC → 승인 분기. 순수·best-effort(절대 raise 안 함).

    각 요청에 대해 큐레이션 후보(discover) + 원격 후보(discover_remote, searcher 주입 시)를 발견하고:
      - **승인(allowlist)된 후보**가 있으면 → provenance 기록(채택 결정). 그 요청은 해소됨.
      - 승인된 후보가 없으면(미승인 후보 or 후보 0) → escalation에 모은다(사람 검토 대기).
    반환 CapabilityOutcome: provenance(채택된 것) + escalation(미승인 있으면 dict, 없으면 None).

    F.2 불변:
      - **자동 채택 없음**: provenance는 *승인(allowlist)된 후보에만* — 원격도 동일 게이트(allowlist).
      - **원격 후보는 실행 0**: poc_runner를 *주지 않는다*(메타데이터 POC, ok=None). 라이브 POC는
        큐레이션 후보에만(F.1b runner). searcher 미주입이면 원격 후보 0(기존 F.1 동작 불변).
    """
    registry = load_capability_registry(registry_dir)
    provenance: list[CapabilityProvenance] = []
    unresolved: list[tuple[CapabilityRequest, list[tuple[CapabilityEntry, CapabilityPOC]]]] = []

    def _poc(entry: CapabilityEntry) -> CapabilityPOC:
        # 원격 후보는 **절대 라이브 실행 안 함** — runner 무시(메타데이터 ok=None). 큐레이션만 F.1b runner.
        return run_poc(entry, runner=None if _is_remote(entry) else poc_runner)

    for req in requests or []:
        candidates = discover(req, registry) + discover_remote(req, searcher=searcher)
        approved = [e for e in candidates if is_approved(e, allowlist)]
        if approved:
            for entry in approved:
                provenance.append(build_provenance(
                    entry, approved_by=approved_by, approved_at=approved_at, poc=_poc(entry)))
        else:
            # 미승인(또는 후보 0) → 후보별 POC 증거를 모아 사람 검토로 escalate.
            cand_pocs = [(e, _poc(e)) for e in candidates]
            unresolved.append((req, cand_pocs))
    escalation = build_capability_escalation(unresolved) if unresolved else None
    return CapabilityOutcome(provenance=provenance, escalation=escalation)
