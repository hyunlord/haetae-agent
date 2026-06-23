"""분해 전 director-측 research 단계 (WO#166, pipeline-strengthening B).

지금은 의뢰→바로 synthesize라 director가 유닛 경계·계약을 "장님으로" 결정 → 복잡 태스크
(Mario-급)서 통합 표면이 거칠다. 이 모듈은 첫 synthesize *전* 1회 bounded research 패스를
돌려 **ResearchBrief**(태스크 분석·스택/규약·관련 패턴·후보 disjoint-scope 경계·후보 facade
계약)를 만든다. 합성기가 그 brief를 *입력*으로 소비해 더 정보-기반 분해를 한다.

설계(spec_critic과 동형):
  - **director-side 계획 입력**: brief는 분해 입력이지 *판정 아님*(합성기가 소비, gate가 독립
    판정). research는 오케스트레이션 LLM 콜이지 *executor 아님* → executor 샌드박스 allowlist와 무관.
  - **best-effort·bounded**: research 실패/파싱 불가 → None(브리프 없이 직접 synthesize, 기존
    동작 불변). 정확히 1회 호출(루프 밖·선행). 절대 raise하지 않는다.
  - **복잡도 게이트**: 복잡/대형 의뢰만(휴리스틱). 단순 의뢰는 skip → 추가 LLM 콜 0.
  - **오프라인**: 소스 = #32 스킬 레지스트리(큐레이션, load_skills) + 의뢰 분석. 네트워크 0
    (F.2 deferred — 후속 확장). gate/judge/run-judge/executor를 import하지 않는다(적대 분리).
"""

from __future__ import annotations

from pathlib import Path

from haetae.llm import LLMClient
from haetae.models import ResearchBrief
from haetae.parsing import ParseError, parse_yaml_model
from haetae.skills import load_skills, match_skills

DEFAULT_RESEARCH_PROMPT_PATH = "prompts/research.md"

# 복잡도 휴리스틱 키워드: 다중-서브시스템/행동 신호(게임·시뮬·엔진·대시보드·여러 시스템 등).
# 이런 의뢰가 통합 표면이 거칠어 분해 전 research가 이득. 결정적·LLM 아님(보수적 신호).
_COMPLEXITY_KEYWORDS = (
    "게임", "game", "시뮬", "simulat", "platformer", "플랫포머", "엔진", "engine",
    "대시보드", "dashboard", "에디터", "editor", "여러", "다수", "시스템", "system",
    "통합", "integrat", "멀티", "multi", "agent", "에이전트", "물리", "physics",
    "충돌", "collision", "렌더", "render", "navigation", "경로탐색", "pathfind",
)
_COMPLEXITY_LEN = 200  # 의뢰 원문 길이 임계(긴 의뢰 = 복잡 신호).


class ResearchError(ParseError):
    """research 응답 파싱/검증 실패. raw 응답은 .raw_response에 보존된다."""


def is_complex_order(order: str) -> bool:
    """이 의뢰가 분해 전 research가 이득인 *복잡/대형* 태스크인가(결정적 휴리스틱).

    True 신호: (a) 원문이 길거나(≥임계) (b) 다중-서브시스템/행동 키워드(게임·시뮬·엔진·
    대시보드·여러 시스템 등)를 담거나. 둘 다 아니면 단순 → False(research skip, 추가 콜 0,
    "direct-first then decompose" 백로그 정합). **판정 아님** — research는 best-effort 제안일
    뿐이라 게이트가 과하게 트리거돼도 합성기/critic이 잡는다(보수성보다 over-trigger가 안전).
    """
    text = (order or "").strip()
    if len(text) >= _COMPLEXITY_LEN:
        return True
    low = text.lower()
    return any(kw.lower() in low for kw in _COMPLEXITY_KEYWORDS)


def _build_user(order: str, patterns: list) -> str:
    pat = (
        "\n".join(f"- {p.name}: {(p.body or '')[:240]}" for p in patterns)
        or "(매칭된 패턴 없음)"
    )
    return (
        f"# 의뢰(order)\n{order}\n\n"
        f"# 관련 패턴 (오프라인 #32 스킬 레지스트리 — 참고)\n{pat}\n\n"
        "위 의뢰와 패턴을 바탕으로 ResearchBrief를 작성하라(분해 전 조사). 후보 유닛 경계는 "
        "*배타적 소유 파일*(형제 간 scope ∩=∅, #165)로, 유닛 간 결합은 facade 계약"
        "(export/import, #160 — 파일 공유 아님)으로 제안하라. 이는 *제안*이지 mandate 아니다."
    )


def research(
    order: str,
    client: LLMClient,
    *,
    skills_dir: str | Path | None = None,
    prompt_path: str | Path = DEFAULT_RESEARCH_PROMPT_PATH,
    max_patterns: int = 5,
) -> ResearchBrief | None:
    """1회 bounded research 패스 → ResearchBrief(또는 실패 시 None — 절대 raise 안 함).

    오프라인: skills_dir(#32 레지스트리, 주어지면)서 패턴을 로드·매칭(네트워크 0). client는
    오케스트레이션 LLM(critic-model/director-side)이지 *executor 아님*. best-effort: 어떤 실패도
    None으로 흡수(브리프 없이 직접 synthesize — 기존 동작). 정확히 1회 complete 호출(bounded).
    """
    patterns: list = []
    if skills_dir is not None:
        try:
            patterns = match_skills(load_skills(skills_dir), order, max_skills=max_patterns)
        except Exception:  # noqa: BLE001 — 레지스트리 로드는 research를 막지 않는다(오프라인 best-effort)
            patterns = []
    try:
        system = Path(prompt_path).read_text(encoding="utf-8")
        raw = client.complete(system, _build_user(order, patterns))
        brief = parse_yaml_model(raw, ResearchBrief, ResearchError)
    except ParseError:
        return None  # 파싱/검증 실패 → 브리프 없이 진행(기존 동작)
    except Exception:  # noqa: BLE001 — research는 advisory: 어떤 실패도 run을 죽이면 안 된다
        return None
    return brief


def render_brief(brief: ResearchBrief) -> str:
    """ResearchBrief를 synth_context에 얹을 텍스트 섹션으로 렌더(합성기 입력 — *제안*)."""
    lines = [
        "# 리서치 브리프 (분해 전 조사 — *제안*이지 mandate 아님, WO#166)",
        "아래는 director-측 research가 조사한 *제안*이다. 더 나은 분해의 출발점으로 쓰되, "
        "맞지 않으면 override하라(합성기 판단 우선 — 적대 spec/decomp critic은 그대로 작동).",
    ]
    if brief.task_analysis:
        lines.append(f"\n## 태스크 분석\n{brief.task_analysis}")
    if brief.stack:
        lines.append(f"\n## 스택/규약\n{brief.stack}")
    if brief.patterns:
        lines.append(
            "\n## 관련 패턴 (오프라인 #32)\n" + "\n".join(f"- {p}" for p in brief.patterns)
        )
    if brief.candidate_units:
        lines.append("\n## 후보 disjoint-scope 분해 (#165 — 형제 간 소유 scope ∩=∅, 제안)")
        for u in brief.candidate_units:
            sc = ", ".join(u.scope) or "(미정)"
            dp = ", ".join(u.deps) or "없음"
            lines.append(f"- {u.unit}: {u.desc} · 소유 scope=[{sc}] · deps=[{dp}]")
    if brief.candidate_contracts:
        lines.append("\n## 후보 facade 계약 (#160 — 파일 공유 아닌 export/import 결합, 제안)")
        for c in brief.candidate_contracts:
            cons = ", ".join(c.consumers) or "(미정)"
            lines.append(
                f"- {c.producer} exports `{c.export_name}` from {c.module_path} → consumers: {cons}"
            )
    if brief.note:
        lines.append(f"\n## 메모\n{brief.note}")
    return "\n".join(lines)


def maybe_research(
    order: str,
    client: LLMClient,
    synth_context: str | None,
    *,
    skills_dir: str | Path | None = None,
    prompt_path: str | Path = DEFAULT_RESEARCH_PROMPT_PATH,
    progress=None,
) -> str | None:
    """복잡 의뢰면 research → brief를 synth_context에 *제안*으로 얹어 반환, 단순이면 그대로(skip).

    run-start 통합 헬퍼(replan 루프 *밖*·1회). 단순 의뢰 → 추가 LLM 콜 0(직접 synthesize).
    research 실패(None) → synth_context 불변(브리프 없이 진행). brief는 *제안*(합성기 override 가능).
    """
    if not is_complex_order(order):
        return synth_context  # 단순 의뢰 — research skip(추가 콜 0)
    if progress:
        progress("research(분해 전 조사) 중…")
    brief = research(order, client, skills_dir=skills_dir, prompt_path=prompt_path)
    if brief is None:
        if progress:
            progress("research: 브리프 없음 — 직접 synthesize")
        return synth_context  # 실패 흡수 — 기존 동작
    if progress:
        progress(f"research: 브리프 생성(후보 유닛 {len(brief.candidate_units)}개) → 합성기에 제안 주입")
    section = render_brief(brief)
    return f"{synth_context}\n\n{section}" if synth_context else section
