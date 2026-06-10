"""인터넷 능력 발견 어댑터 — `CapabilitySearcher` 구현 (WO#61 Phase F.2, discovery-only).

**이 파일이 repo에서 *유일하게* 네트워크 import(urllib)를 가진 능력-획득 코드다.** `capability.py`는
네트워크-free를 유지하고(인터페이스만), 실제 HTTP는 여기에 격리한다. director-side·opt-in:
`--capability-search`로 켤 때만 import/사용되며, executor에 네트워크를 주지 않는다(sandbox 불변).

안전 불변(F.2):
  - **discovery-only — 실행 0.** 레지스트리 *메타데이터*만 읽는다(install/import/run 안 함).
    호출부(capability.preflight)가 원격 후보를 메타데이터 POC(ok=None)로만 다룬다.
  - **자동 채택 없음.** 여기선 후보 dict만 만든다. 채택은 사람 allowlist 게이트(capability.py).
  - **best-effort.** 타임아웃·HTTP 오류·파싱 실패는 흡수 → [](큐레이션-only 폴백). 절대 raise 안 함.
  - **bounded.** 요청당 소수의 후보 이름만 조회(무한 크롤 금지), 짧은 타임아웃.

반환 dict 스키마(capability._parse_remote_entry가 정규화): {identifier, registry, ecosystem,
license, imports?, note?}. source는 호출부에서 항상 `remote:<registry>`로 박힌다.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

from haetae.models import CapabilityRequest

PYPI_JSON_URL = "https://pypi.org/pypi/{name}/json"
_DEFAULT_TIMEOUT = 5.0
_MAX_CANDIDATES = 4  # 요청당 조회할 후보 이름 상한(bounded — 무한 크롤 금지)
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


def make_searcher(registry: str = "pypi", *, timeout: float = _DEFAULT_TIMEOUT):
    """레지스트리 이름으로 CapabilitySearcher 생성(현재 pypi). director-side·opt-in 진입점."""
    if registry == "pypi":
        return PypiSearcher(timeout=timeout)
    raise ValueError(f"지원하지 않는 capability-search 레지스트리: {registry!r} (현재 pypi만)")
