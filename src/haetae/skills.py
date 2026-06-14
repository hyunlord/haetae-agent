"""스킬 레지스트리 — 읽기전용 패턴 문서를 유닛 작업지시서에 매칭 주입(Phase B v1).

LEAP의 LeanSearch(루프 중 mathlib lemma를 검색해 주입)의 haetae 버전:
캡스톤 교훈(스택 치환·자가채점 cheat·벽 쌓임·버스트 스폰·카운트만 trace)을 *스킬*로
durable하게 인코딩해, 매칭되는 유닛의 work order에 *빌더(executor) 가이드*로 덧붙인다.

핵심 불변(반드시 보존):
  - **빌더 전용.** judge/run-judge/spec_critic/gate 어디에도 주입하지 않는다 — 검증
    독립성(적대적 분리). 스킬이 verifier로 새면 "자기 패턴으로 자기를 채점"하는 collapse.
  - **읽기전용·무네트워크.** 스킬은 repo에 커밋된 `skills/<name>/SKILL.md`. 레지스트리는
    읽기만 한다. 임의 네트워크 툴/MCP/플러그인 자동획득 없음(로드맵 안전선).
  - **best-effort.** 로드/파싱/매칭 실패는 흡수(주입 없이 진행) — 절대 raise하지 않는다.

매칭은 v1에서 **트리거 키워드**(결정적·테스트가능): 작업지시서 텍스트에 트리거가
(대소문자 무시) 나타나면 매칭. LLM/임베딩 의미검색은 v2.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

# 주입 캡 — executor 프롬프트 비대화 방지.
DEFAULT_MAX_SKILLS = 3  # 매칭 다수 시 상위 K개만
DEFAULT_MAX_BODY_CHARS = 4000  # 주입 본문 총합 상한(초과분은 잘라냄)

SKILL_FILENAME = "SKILL.md"
SKILL_SECTION_HEADER = "## 참고 패턴 (스킬)"


@dataclass(frozen=True)
class Skill:
    """스킬 하나 = 트리거 키워드 + 패턴 본문.

    name:     스킬 식별자(기본 디렉토리명, frontmatter `name`로 override 가능).
    triggers: 매칭 키워드(소문자 정규화). 하나라도 작업지시서에 나타나면 매칭.
    body:     executor에 주입할 패턴/베스트프랙티스 본문(frontmatter 뒤 전체).
    """

    name: str
    triggers: tuple[str, ...]
    body: str


def _split_frontmatter(text: str) -> tuple[str | None, str]:
    """`---` 펜스로 감싼 YAML frontmatter와 본문을 분리한다.

    frontmatter가 없으면 (None, 원문). 닫는 `---`가 없으면 frontmatter 취급 안 함.
    """
    stripped = text.lstrip("﻿")  # BOM 방어
    lines = stripped.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            fm = "\n".join(lines[1:i])
            body = "\n".join(lines[i + 1 :])
            return fm, body
    return None, text


def _parse_skill(name: str, text: str) -> Skill | None:
    """SKILL.md 텍스트 → Skill. frontmatter/triggers 없으면 None(매칭 불가)."""
    fm, body = _split_frontmatter(text)
    if fm is None:
        return None
    meta = yaml.safe_load(fm)
    if not isinstance(meta, dict):
        return None
    raw_triggers = meta.get("triggers") or []
    if isinstance(raw_triggers, str):
        raw_triggers = [raw_triggers]
    if not isinstance(raw_triggers, list):
        return None
    triggers = tuple(
        str(t).strip().lower() for t in raw_triggers if str(t).strip()
    )
    if not triggers:
        return None  # 트리거 없으면 영영 매칭 안 됨 → 의미 없는 스킬
    skill_name = str(meta.get("name") or name)
    return Skill(name=skill_name, triggers=triggers, body=body.strip())


def load_skills(skills_dir: str | Path) -> list[Skill]:
    """`skills_dir` 아래 각 `<name>/SKILL.md`를 파싱해 Skill 목록을 반환한다.

    완전 best-effort: 디렉토리 부재/접근 실패 → 빈 리스트. 개별 스킬 파싱 실패 →
    그 스킬만 스킵(다른 스킬은 살림). 절대 raise하지 않는다.
    결과는 디렉토리명 정렬 → 결정적 순서(매칭 캡도 결정적).
    """
    skills: list[Skill] = []
    try:
        base = Path(skills_dir)
        if not base.is_dir():
            return []
        for sub in sorted(base.iterdir()):
            # WO#103 거버넌스(자동채택 0): `_`/`.` 접두 디렉토리는 *비활성 staging*(예: _candidates)
            # 이거나 숨김 — 활성 레지스트리에서 제외한다. 학습 후보(skills/_candidates/<name>/)는
            # 사람이 명시 승인(approve)해 *활성 skills/<name>/로 옮겨질 때만* 로드/주입된다.
            if sub.name.startswith("_") or sub.name.startswith("."):
                continue
            md = sub / SKILL_FILENAME
            if not md.is_file():
                continue
            try:
                skill = _parse_skill(sub.name, md.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001 — 깨진 스킬 하나가 전체를 막으면 안 됨
                continue
            if skill is not None:
                skills.append(skill)
    except Exception:  # noqa: BLE001 — 레지스트리 로드는 run을 죽이면 안 됨
        return []
    return skills


def boundary_match(needle: str, haystack: str) -> bool:
    """needle이 haystack에서 **왼쪽 word-boundary**(앞이 ascii 영숫자 아님 또는 문자열 시작)에
    나타나는가 — 오른쪽은 *열림*(stem/prefix·suffix 허용). WO#107.

    naive substring의 ascii 중간-단어 과매칭("ui"가 build/guid/fluid에)을 차단하되:
      - **stem/prefix 보존**: "sim" → "simulation"·"sim:trace"(오른쪽 열림).
      - **멀티워드 구**: "behavior trace"·"crowd simulator"는 구 그대로 매칭(공백 포함).
      - **한글 교착 보존**: 왼쪽 경계는 *ascii 영숫자만* 차단하므로 한글-인접/조사 부착
        ("드래그앤드롭" ⊂ "드래그앤드롭으로", "X드래그앤드롭")은 그대로 매칭(#97 #72 보존).
    needle/haystack 모두 소문자 가정. 결정적(정규식)·LLM 아님(의미 매칭은 v2 보류).
    """
    if not needle:
        return False
    # (?<![a-z0-9]) = 직전 문자가 ascii 영숫자가 아님(=왼쪽 word-boundary; 한글/기호/공백/시작 허용).
    return re.search(r"(?<![a-z0-9])" + re.escape(needle), haystack) is not None


def match_skills(
    skills: list[Skill],
    work_order_text: str,
    *,
    max_skills: int = DEFAULT_MAX_SKILLS,
) -> list[Skill]:
    """work_order_text에 트리거가 (대소문자 무시) 나타나는 스킬을 반환(상위 K개로 캡).

    무매칭 → 빈 리스트. 순서는 입력 skills 순서(=로드 정렬)라 결정적.
    """
    text = (work_order_text or "").lower()
    matched = [
        sk for sk in skills
        if any(boundary_match(trig, text) for trig in sk.triggers)
    ]
    return matched[:max_skills] if max_skills >= 0 else matched


def inject_skills(
    work_order_text: str,
    matched: list[Skill],
    *,
    max_body_chars: int = DEFAULT_MAX_BODY_CHARS,
) -> str:
    """매칭된 스킬 본문을 `## 참고 패턴 (스킬)` 섹션으로 덧붙인 텍스트를 반환한다.

    매칭이 비어 있으면 원본을 그대로 반환(주입 없음). 총 본문이 max_body_chars를
    넘으면 넘는 지점에서 잘라낸다(executor 프롬프트 비대화 방지).
    """
    if not matched:
        return work_order_text
    blocks: list[str] = []
    used = 0
    for sk in matched:
        remaining = max_body_chars - used
        if remaining <= 0:
            break
        body = sk.body
        if len(body) > remaining:
            body = body[:remaining].rstrip() + "\n…(생략)"
        used += len(body)
        blocks.append(f"### {sk.name}\n{body}")
    section = (
        f"{SKILL_SECTION_HEADER}\n\n"
        "다음은 이 작업에 매칭된 읽기전용 참고 패턴이다 "
        "(검증 기준이 아니라 빌더 가이드 — 그대로 베끼지 말고 맥락에 맞게 적용하라):\n\n"
        + "\n\n".join(blocks)
    )
    base = work_order_text or ""
    return f"{base}\n\n{section}" if base.strip() else section
