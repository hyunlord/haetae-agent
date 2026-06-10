"""인터넷 능력 발견 어댑터 — `CapabilitySearcher` 구현 (WO#61 Phase F.2, discovery-only).

**이 파일이 repo에서 *유일하게* 네트워크 import(urllib)를 가진 능력-획득 코드다.** `capability.py`는
네트워크-free를 유지하고(인터페이스만), 실제 HTTP는 여기에 격리한다. director-side·opt-in:
`--capability-search`로 켤 때만 import/사용되며, executor에 네트워크를 주지 않는다(sandbox 불변).

안전 불변(F.2):
  - **discovery-only — 실행 0.** 레지스트리 *메타데이터*만 읽는다(install/import/run 안 함).
    호출부(capability.preflight)가 원격 후보를 메타데이터 POC(ok=None)로만 다룬다.
  - **자동 채택 없음.** 여기선 후보 dict만 만든다. 채택은 사람 allowlist 게이트(capability.py).
  - **best-effort.** 타임아웃·HTTP 오류·파싱 실패는 흡수 → [](큐레이션-only 폴백). 절대 raise 안 함.
  - **bounded.** 요청당 소수 후보만(무한 크롤 금지), 짧은 타임아웃.

검색 방식(F.2+):
  - **npm = 진짜 의미 검색.** `registry.npmjs.org/-/v1/search?text=<능력텍스트>`가 키워드/설명
    매칭 + 관련도 랭킹을 준다 → 이름이 안 맞아도 적합 패키지를 찾는다(의미 검색의 주 vehicle).
  - **pypi = 이름 기반(한계).** pypi엔 안정적 공식 JSON 검색 API가 없어 *이름 직조회*로 남긴다
    (능력 텍스트→슬러그 추측). 이름이 안 맞으면 놓친다 — pypi 의미 검색은 후속(서드파티 인덱스 필요).
  - **멀티 레지스트리.** `make_searcher("npm,pypi")` → composite(각 조회→병합→identifier+registry dedup).

반환 dict 스키마(capability._parse_remote_entry가 정규화): {identifier, registry, ecosystem,
license, imports?, note?(=description), keywords?, relevance?}. source는 호출부에서 항상
`remote:<registry>`로 박힌다. note/keywords/relevance는 escalation에 노출돼 사람 판단을 돕는다.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

from haetae.models import CapabilityRequest

PYPI_JSON_URL = "https://pypi.org/pypi/{name}/json"
NPM_SEARCH_URL = "https://registry.npmjs.org/-/v1/search?text={text}&size={size}"
_DEFAULT_TIMEOUT = 5.0
_MAX_CANDIDATES = 4  # pypi: 요청당 조회할 *이름* 후보 상한(bounded — 무한 크롤 금지)
_NPM_SIZE = 5  # npm: 요청당 반환할 검색 결과 상한(bounded, 관련도순)
_TOKEN_RE = re.compile(r"[a-z0-9]+(?:[-_][a-z0-9]+)*")


def _candidate_names(capability: str) -> list[str]:
    """능력 자유 텍스트에서 *조회할 패키지 이름 후보*를 보수적으로 뽑는다(bounded·결정적).

    예: "pathfinding for grid" → ["pathfinding-for-grid", "pathfinding", "pathfinding_for_grid"].
    pypi 이름 규칙(소문자·하이픈/언더스코어)에 맞춘 슬러그 + 개별 토큰. 중복 제거, 상한 컷.
    """
    text = (capability or "").strip().lower()
    if not text:
        return []
    tokens = _TOKEN_RE.findall(text)
    if not tokens:
        return []
    names: list[str] = []
    slug = "-".join(tokens)
    names.append(slug)
    if "-" in slug:
        names.append(slug.replace("-", "_"))
    if tokens[0] != slug:
        names.append(tokens[0])  # 첫 토큰 단독(예: "pathfinding")
    # 중복 제거(순서 보존) + 상한.
    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        if n and n not in seen:
            seen.add(n)
            out.append(n)
        if len(out) >= _MAX_CANDIDATES:
            break
    return out


def _license_from_info(info: dict) -> str:
    """pypi info에서 라이선스 신호 추출 — license 필드 우선, 없으면 classifiers의 License 분류자."""
    lic = (info.get("license") or "").strip()
    if lic:
        return lic
    for c in info.get("classifiers") or []:
        if isinstance(c, str) and c.startswith("License ::"):
            return c.split("::")[-1].strip()
    return "unknown"


class PypiSearcher:
    """pypi `/<name>/json`을 조회하는 `CapabilitySearcher`(discovery-only·best-effort).

    능력 텍스트에서 후보 이름을 슬러그로 만들어 *존재하는* 패키지의 메타데이터만 수집한다.
    pypi엔 안정적 검색 JSON API가 없어 이름 직조회를 쓴다(보수적·결정적·bounded). 실패는 흡수.
    timeout/opener는 테스트 주입 가능(실네트워크 없이 단위 테스트 — 보통은 mock searcher 사용).
    """

    def __init__(self, *, timeout: float = _DEFAULT_TIMEOUT, opener=None):
        self.timeout = timeout
        # opener(name)->info dict|None 주입 시 그걸 사용(테스트). 기본은 실제 pypi HTTP.
        self._opener = opener or self._http_fetch

    def _http_fetch(self, name: str) -> dict | None:
        """pypi에서 패키지 JSON을 가져온다. 없거나 실패면 None(best-effort, 네트워크 격리 지점)."""
        url = PYPI_JSON_URL.format(name=urllib.parse.quote(name, safe=""))
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 — https pypi
                if getattr(resp, "status", 200) != 200:
                    return None
                data = json.loads(resp.read().decode("utf-8"))
            return data if isinstance(data, dict) else None
        except (urllib.error.URLError, OSError, ValueError, TimeoutError):
            return None

    def __call__(self, request: CapabilityRequest) -> list[dict]:
        out: list[dict] = []
        seen: set[str] = set()
        for name in _candidate_names(request.capability):
            try:
                data = self._opener(name)
            except Exception:  # noqa: BLE001 — opener 버그도 흡수(best-effort)
                data = None
            if not isinstance(data, dict):
                continue
            info = data.get("info") if isinstance(data.get("info"), dict) else {}
            ident = str(info.get("name") or name).strip()
            if not ident or ident.lower() in seen:
                continue
            seen.add(ident.lower())
            summary = str(info.get("summary") or "").strip()
            out.append({
                "identifier": ident,
                "registry": "pypi",
                "ecosystem": "pip",
                "license": _license_from_info(info),
                "imports": [ident.replace("-", "_")],  # 메타데이터 힌트(실행 안 함)
                "note": summary[:200],
            })
        return out


def _npm_license(pkg: dict) -> str:
    """npm package의 license 신호 — 문자열 또는 {type:...} dict 둘 다 흡수."""
    lic = pkg.get("license")
    if isinstance(lic, str) and lic.strip():
        return lic.strip()
    if isinstance(lic, dict) and str(lic.get("type") or "").strip():
        return str(lic["type"]).strip()
    return "unknown"


class NpmSearcher:
    """npm 검색 API로 *의미(키워드/설명) 검색*하는 `CapabilitySearcher`(discovery-only·best-effort).

    `registry.npmjs.org/-/v1/search?text=<능력텍스트>&size=N` — 능력 자유 텍스트를 그대로 query로
    써서 관련도 랭킹된 패키지를 받는다(이름 추측 아님 = pypi 대비 진짜 의미 검색). 응답
    `objects[].package`에서 name/description/keywords/license + score.final(관련도)을 수집, 상한 N.
    실패(타임아웃·비-200·파싱)는 흡수→[]. opener 주입 가능(실네트워크 없이 단위 테스트).
    """

    def __init__(self, *, timeout: float = _DEFAULT_TIMEOUT, size: int = _NPM_SIZE, opener=None):
        self.timeout = timeout
        self.size = size
        # opener(query)->검색 JSON dict|None 주입 시 그걸 사용(테스트). 기본은 실제 npm HTTP.
        self._opener = opener or self._http_search

    def _http_search(self, query: str) -> dict | None:
        """npm 검색 API 조회. 실패면 None(best-effort, 네트워크 격리 지점)."""
        url = NPM_SEARCH_URL.format(text=urllib.parse.quote(query, safe=""), size=self.size)
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 — https npm
                if getattr(resp, "status", 200) != 200:
                    return None
                data = json.loads(resp.read().decode("utf-8"))
            return data if isinstance(data, dict) else None
        except (urllib.error.URLError, OSError, ValueError, TimeoutError):
            return None

    def __call__(self, request: CapabilityRequest) -> list[dict]:
        query = (request.capability or "").strip()
        if not query:
            return []
        try:
            data = self._opener(query)
        except Exception:  # noqa: BLE001 — opener 버그도 흡수(best-effort)
            data = None
        if not isinstance(data, dict):
            return []
        objects = data.get("objects")
        if not isinstance(objects, list):
            return []
        out: list[dict] = []
        seen: set[str] = set()
        for obj in objects[: self.size]:  # 관련도순 상한(bounded)
            if not isinstance(obj, dict):
                continue
            pkg = obj.get("package") if isinstance(obj.get("package"), dict) else {}
            ident = str(pkg.get("name") or "").strip()
            if not ident or ident.lower() in seen:
                continue
            seen.add(ident.lower())
            kws = pkg.get("keywords") if isinstance(pkg.get("keywords"), list) else []
            score = obj.get("score") if isinstance(obj.get("score"), dict) else {}
            final = score.get("final")
            out.append({
                "identifier": ident,
                "registry": "npm",
                "ecosystem": "npm",
                "license": _npm_license(pkg),
                "note": str(pkg.get("description") or "")[:200],
                "keywords": [str(k) for k in kws if str(k).strip()][:10],
                "relevance": float(final) if isinstance(final, (int, float)) else None,
            })
        return out


class _CompositeSearcher:
    """여러 레지스트리 searcher를 순서대로 조회→병합→(identifier+registry) dedup(멀티 레지스트리)."""

    def __init__(self, searchers: list):
        self._searchers = searchers

    def __call__(self, request: CapabilityRequest) -> list[dict]:
        merged: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for s in self._searchers:
            try:
                results = s(request)
            except Exception:  # noqa: BLE001 — 한 레지스트리 실패가 다른 것을 막지 않는다
                results = []
            if not isinstance(results, list):
                continue
            for item in results:
                if not isinstance(item, dict):
                    continue
                ident = str(item.get("identifier") or item.get("name") or "").strip().lower()
                reg = str(item.get("registry") or "").strip().lower()
                if not ident or (ident, reg) in seen:
                    continue
                seen.add((ident, reg))
                merged.append(item)
        return merged


_REGISTRY_BUILDERS = {
    "npm": lambda timeout: NpmSearcher(timeout=timeout),
    "pypi": lambda timeout: PypiSearcher(timeout=timeout),
}


def make_searcher(registries: str = "npm", *, timeout: float = _DEFAULT_TIMEOUT):
    """레지스트리 이름(콤마 리스트 수용)으로 CapabilitySearcher 생성. director-side·opt-in 진입점.

    "npm"·"pypi"·"npm,pypi" 등. 여러 개면 composite(병합·dedup). 미지 레지스트리 → ValueError.
    """
    names = [r.strip().lower() for r in str(registries).split(",") if r.strip()]
    if not names:
        raise ValueError("capability-search 레지스트리가 비어 있음 (예: npm | pypi | npm,pypi)")
    built = []
    for r in names:
        builder = _REGISTRY_BUILDERS.get(r)
        if builder is None:
            raise ValueError(
                f"지원하지 않는 capability-search 레지스트리: {r!r} (지원: npm, pypi)"
            )
        built.append(builder(timeout))
    return built[0] if len(built) == 1 else _CompositeSearcher(built)
