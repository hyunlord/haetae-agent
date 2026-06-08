"""실행 폼 옵션 디스크립터 (provider-agnostic launch-options) — WO#45.

haetae는 executor pluggable이므로 **"쓸 수 있는 옵션·기본값"은 provider가 선언하는
메타데이터**여야 한다. 이 모듈이 그 *디스크립터*다 — 실행 로직과 완전히 분리된 순수
메타데이터(+ best-effort 설정 읽기)뿐.

엔진-격리 불변식(WO#28/#37): 대시보드는 read-only 뷰어라 엔진/실행 코드를 import하면
안 된다. 그래서 이 모듈은 **엔진-free 리프**다 — stdlib(tomllib/os/pathlib/dataclasses)만
쓰고 `haetae.*`를 일절 import하지 않는다. 덕분에 provider(`providers.codex`)와
대시보드(`dashboard`)가 *둘 다* 안전하게 import해 같은 디스크립터를 공유한다(미러링 불필요).

best-effort 원칙: `~/.codex/config.toml`을 못 읽거나 깨져도 정적 기본값으로 폴백(무크래시).
실행 동작·sandbox·판정과 무관 — 이건 *옵션 표시*만 한다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:  # py3.11+ stdlib. (requires-python >=3.11이라 항상 존재하나 방어적으로.)
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - 3.10 이하 폴백(이 레포는 3.11+)
    tomllib = None  # type: ignore[assignment]

# codex 추론 강도 화이트리스트(WO#38과 동일). codex의 `model_reasoning_effort` 값과 일치.
# **sandbox 권한과 무관** — ALLOWED_SANDBOXES를 절대 건드리지 않는다.
REASONING_EFFORT_LEVELS = ("minimal", "low", "medium", "high", "xhigh")
DEFAULT_REASONING_EFFORT = "medium"

# 사용자 codex 설정 경로(세팅 추적 pre-fill용). CODEX_HOME 존중.
_CODEX_CONFIG_ENV = "CODEX_HOME"
_CODEX_CONFIG_REL = "config.toml"


@dataclass
class LaunchOption:
    """provider-agnostic 실행 옵션 한 개의 디스크립터(렌더용 메타데이터).

    name:        argv/옵션 키(영어). 폼·validate_options와 매칭되는 식별자.
    label:       사람이 읽는 라벨(한국어 가능).
    kind:        "select" | "text"  (number/checkbox는 비-provider 옵션이라 폼 정적 처리).
    default:     기본값(pre-fill). select는 빈 문자열이면 "미지정"을 뜻함.
    choices:     select일 때 선택지(없으면 자유 입력 text).
    optional:    비워도 되는지(=비우면 provider 기본 자동).
    hint:        권장/비용 등 안내 텍스트.
    placeholder: text 입력의 placeholder.
    """

    name: str
    label: str
    kind: str
    default: str = ""
    choices: tuple[str, ...] = field(default_factory=tuple)
    optional: bool = True
    hint: str = ""
    placeholder: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "kind": self.kind,
            "default": self.default,
            "choices": list(self.choices),
            "optional": self.optional,
            "hint": self.hint,
            "placeholder": self.placeholder,
        }


def _codex_config_path(config_path: str | Path | None = None) -> Path:
    if config_path is not None:
        return Path(config_path)
    home = os.environ.get(_CODEX_CONFIG_ENV)
    base = Path(home) if home else Path.home() / ".codex"
    return base / _CODEX_CONFIG_REL


def read_codex_config(config_path: str | Path | None = None) -> dict[str, Any]:
    """`~/.codex/config.toml`에서 현재 `model`/`model_reasoning_effort`를 best-effort로 읽는다.

    read-only·best-effort: 파일 부재·파싱 실패·tomllib 부재 → 빈 dict(무크래시). 실행/샌드박스
    /판정과 무관 — pre-fill용 표시 데이터일 뿐이다. 반환 키는 존재하는 것만(부분 가능).
    """
    if tomllib is None:
        return {}
    path = _codex_config_path(config_path)
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, ValueError, tomllib.TOMLDecodeError):  # 부재/권한/깨진 TOML 전부 흡수
        return {}
    except Exception:  # noqa: BLE001 — 표시용 read는 절대 폼/서버를 죽이지 않는다
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, Any] = {}
    model = data.get("model")
    if isinstance(model, str) and model.strip():
        out["model"] = model.strip()
    effort = data.get("model_reasoning_effort")
    if isinstance(effort, str) and effort.strip() in REASONING_EFFORT_LEVELS:
        out["reasoning_effort"] = effort.strip()
    return out


def codex_launch_options(config_path: str | Path | None = None) -> list[LaunchOption]:
    """codex provider의 launch-options 디스크립터(provider가 선언하는 속성).

    best-effort로 사용자 codex 설정을 읽어 기본값을 pre-fill(= 세팅 추적). 못 읽으면 정적 기본
    (effort=medium, model=빈값=자동). sandbox/실행 경로와 무관.
    """
    cfg = read_codex_config(config_path)
    effort_default = cfg.get("reasoning_effort", DEFAULT_REASONING_EFFORT)
    if effort_default not in REASONING_EFFORT_LEVELS:
        effort_default = DEFAULT_REASONING_EFFORT
    model_default = cfg.get("model", "")
    return [
        LaunchOption(
            name="reasoning_effort",
            label="reasoning-effort",
            kind="select",
            default=effort_default,
            choices=REASONING_EFFORT_LEVELS,
            optional=True,
            hint=(
                "반복/이터레이션엔 medium, 정밀하게 한 유닛 풀 땐 high/xhigh — "
                "xhigh는 토큰 3-5배·비쌈. 비우면 codex 기본(medium)."
            ),
        ),
        LaunchOption(
            name="model",
            label="model",
            kind="text",
            default=model_default,
            choices=(),
            optional=True,
            hint="비우면 codex 기본 모델 자동(최신).",
            placeholder="비우면 자동·최신",
        ),
    ]


# provider 레지스트리: executor 이름 → 디스크립터 생성기(provider-agnostic).
# 다른 provider가 붙을 때 여기에 자신의 launch_options를 등록하면 폼이 자동 적응한다.
_PROVIDERS = {
    "codex": codex_launch_options,
    "human": lambda config_path=None: [],  # 사람 릴레이는 provider 옵션 없음
}


def launch_options_for(
    executor: str, config_path: str | Path | None = None
) -> list[LaunchOption]:
    """executor 이름 → 그 provider의 옵션 디스크립터. 미지 executor면 빈 리스트(무크래시)."""
    gen = _PROVIDERS.get(executor)
    if gen is None:
        return []
    return gen(config_path)


def all_launch_options(
    config_path: str | Path | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """등록된 모든 provider의 디스크립터를 JSON-가능 dict로(폼/엔드포인트용)."""
    return {
        name: [opt.to_dict() for opt in gen(config_path)]
        for name, gen in _PROVIDERS.items()
    }
