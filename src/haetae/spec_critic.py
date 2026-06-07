"""adversarial spec critic — synthesize 직후 기준의 *물렁함*을 적대적으로 비평.

governance(WO#16)는 brain이 합격선을 *낮추는* 걸 막지만, 합격선이 *처음부터 낮게
깔리는* 건 못 막는다(store-sim의 교훈: "충돌 금지"를 격자-이산으로 받아 어려움을 spec
차원에서 증발시킴). 그래서 합성 직후 독립된 적대적 critic이 order 대비 spec을 보고
*구체적 cheap-path / 빠진 hard-part*를 찾아 플래그한다.

설계(judge와 동형):
  - critic은 합성기와 *다른 모델* 권장(독립성). client는 호출부에서 주입.
  - 적대적 프레이밍은 prompts/spec_critic.md에 있다(막연한 지적 금지, 구체일 때만 soft).
  - 견고성: 출력이 깨지면 crash 말고 "adequate"(=평가 불가, 진행 안 막음)로 흡수하되
    그 사실을 note에 남긴다. brain을 못 믿어서 합성을 멈추면 안 된다.

하이브리드((a)+(b)): 비평은 *항상* surface/기록(a), 구체 gap이 있으면 *정확히 1회*
재합성(b). 재합성 결과가 검증 실패하면 원본 spec으로 폴백. 2회차 재합성은 없다(바운드).
"""

from __future__ import annotations

from pathlib import Path

import yaml

from haetae.intake import SynthesisError, synthesize
from haetae.llm import LLMClient
from haetae.models import ProjectSpec, SpecCritique
from haetae.parsing import ParseError, parse_yaml_model

DEFAULT_CRITIC_PROMPT_PATH = "prompts/spec_critic.md"

DEFAULT_PROMPT_PATH = "prompts/synthesizer.md"  # 재합성에 쓰는 합성기 프롬프트


class SpecCriticError(ParseError):
    """critic 응답 파싱/검증 실패. raw 응답은 .raw_response에 보존된다."""


def _norm_verdict(v: str | None) -> str:
    """verdict 변종을 soft|adequate로 정규화. 'soft'만 soft, 그 외 전부 adequate(보수적)."""
    return "soft" if (v or "").strip().lower() == "soft" else "adequate"


def _normalize_critique_dict(data: dict) -> dict:
    """critic 출력의 흔한 변종을 SpecCritique 스키마 모양으로 보정한다(보조 안전망).

    - gaps가 없거나 None → 빈 리스트.
    - gaps 항목이 문자열이면 → {area: <문자열>}로 감싼다.
    - gap 항목의 cheap-path / cheappath / fix / strengthen 키 변종 흡수.
    """
    if not isinstance(data, dict):
        return data
    d = dict(data)

    gaps = d.get("gaps")
    if gaps is None:
        d["gaps"] = []
        return d
    if isinstance(gaps, list):
        new_gaps = []
        for g in gaps:
            if isinstance(g, str):
                new_gaps.append({"area": g})
                continue
            if isinstance(g, dict):
                g = dict(g)
                if "cheap_path" not in g:
                    for k in ("cheap-path", "cheappath", "cheap"):
                        if k in g:
                            g["cheap_path"] = g.pop(k)
                            break
                if "strengthening" not in g:
                    for k in ("strengthen", "fix", "strengthened"):
                        if k in g:
                            g["strengthening"] = g.pop(k)
                            break
                if "area" not in g:
                    g["area"] = "(unspecified)"
            new_gaps.append(g)
        d["gaps"] = new_gaps
    return d


def _dump_spec(spec: ProjectSpec) -> str:
    return yaml.safe_dump(
        spec.model_dump(by_alias=True, mode="json"),
        allow_unicode=True,
        sort_keys=False,
    )


def critique_spec(
    order: str,
    spec: ProjectSpec,
    client: LLMClient,
    prompt_path: str | Path = DEFAULT_CRITIC_PROMPT_PATH,
) -> SpecCritique:
    """order 대비 spec을 적대적으로 비평해 SpecCritique를 반환한다.

    완전 best-effort(WO#20): critic은 opt-in *보조* 단계라 그 *어떤* 실패도 본 run을
    막아선 안 된다. 따라서 critic 작업 전체(client.complete 호출 + 파싱/검증)를 감싸
    **절대 raise하지 않는다** — 깨진 출력·클라이언트 예외(CodexError 등)·기타 예외 모두
    verdict="adequate"(평가 불가, 진행 막지 않음)로 흡수하되 사유를 note에 남긴다.
    """
    system = Path(prompt_path).read_text(encoding="utf-8")
    user = (
        f"# 원본 주문(order)\n{order}\n\n"
        f"# 합성된 spec(검증 대상)\n```yaml\n{_dump_spec(spec)}```"
    )

    try:
        raw = client.complete(system, user)
        crit = parse_yaml_model(
            raw, SpecCritique, SpecCriticError, normalize=_normalize_critique_dict
        )
    except ParseError as e:
        # brain을 못 믿어서 합성을 멈추지 않는다 — 평가 불가로 흡수하고 기록.
        return SpecCritique(
            verdict="adequate",
            gaps=[],
            note=f"평가 불가: critic 출력 파싱/검증 실패 ({e.message})",
        )
    except Exception as e:  # noqa: BLE001 — critic은 advisory: 어떤 클라이언트/실행 실패도 run을 죽이면 안 된다
        # client.complete가 던지는 예외(CodexError 등) + 기타 전부 흡수. 사유를 note에.
        return SpecCritique(
            verdict="adequate",
            gaps=[],
            note=f"critic 실행 실패: {e} (평가 불가)",
        )

    # verdict 정규화(영문 canonical 보장 — 'SOFT'/'Soft' 등 흡수, 미지값은 adequate).
    crit.verdict = _norm_verdict(crit.verdict)
    return crit


def _should_resynthesize(crit: SpecCritique) -> bool:
    """구체적 gap이 있는 soft일 때만 재합성(막연한 soft는 트리거 안 함)."""
    return _norm_verdict(crit.verdict) == "soft" and len(crit.gaps) > 0


def _build_feedback(crit: SpecCritique) -> str:
    """critique의 gap/strengthening을 재합성 피드백 텍스트로 직렬화."""
    lines = ["적대적 비평가가 지적한 *싸구려 충족 경로 / 빠진 어려움*:"]
    for i, g in enumerate(crit.gaps, 1):
        lines.append(f"{i}. 영역: {g.area}")
        if g.cheap_path:
            lines.append(f"   - 싸구려 충족 경로: {g.cheap_path}")
        if g.strengthening:
            lines.append(f"   - 강화 방향: {g.strengthening}")
    lines.append(
        "→ 위 cheap-path를 막도록 acceptance_criteria/done_when을 더 엄격히 재작성하라. "
        "주문의 *어려운 핵심*이 기준에 반드시 걸리게 하라."
    )
    return "\n".join(lines)


def synthesize_with_critique(
    order: str,
    client: LLMClient,
    critic_client: LLMClient | None,
    *,
    context: str | None = None,
    syn_prompt_path: str | Path = DEFAULT_PROMPT_PATH,
    critic_prompt_path: str | Path = DEFAULT_CRITIC_PROMPT_PATH,
) -> tuple[ProjectSpec, SpecCritique | None]:
    """합성 + (opt-in) 적대적 비평 + 바운드 1회 재합성.

    critic_client가 None → critic을 돌리지 않고 (spec, None) 반환(기존 동작·비용 불변).
    있으면 비평을 surface/기록하고, 구체 gap이 있으면 *정확히 1회* 재합성한다.
    재합성 결과가 검증 실패하면 원본 spec으로 폴백(crash 금지). 2회차 재합성 없음.

    첫 synthesize의 SynthesisError는 흡수하지 않고 그대로 전파한다(루프가 escalate 처리).
    """
    spec = synthesize(order, client, context=context, prompt_path=syn_prompt_path)
    if critic_client is None:
        return spec, None

    crit = critique_spec(order, spec, critic_client, prompt_path=critic_prompt_path)

    if _should_resynthesize(crit):
        feedback = _build_feedback(crit)
        try:
            stronger = synthesize(
                order, client, context=context,
                prompt_path=syn_prompt_path, feedback=feedback,
            )
            spec = stronger  # 강화된 spec 채택
            crit.resynthesized = True
        except SynthesisError as e:
            # 재합성 실패 → 원본 spec 폴백. crash 금지. 사실을 note에 남긴다.
            # critic이 *옳게* 강화책을 냈는데 적용이 깨지면 약한 원본이 그대로 남는다
            # — 그 유실을 대시보드/사람이 명확히 보도록 "강화책 유실"로 못박는다.
            crit.resynthesized = False
            if isinstance(e.__cause__, yaml.YAMLError):
                reason = f"재합성 YAML 파싱 반복 실패 ({e.message})"
            else:
                reason = f"재합성 결과 검증 실패 ({e.message})"
            fallback_note = (
                f"⚠️ 강화책 유실: critic의 강화 기준이 적용되지 못하고 약한 원본 spec이 "
                f"유지됨 — {reason}"
            )
            crit.note = f"{crit.note} | {fallback_note}" if crit.note else fallback_note

    return spec, crit
