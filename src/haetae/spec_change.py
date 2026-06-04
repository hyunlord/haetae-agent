"""governed spec-change 적용 — mutability gradient 정책.

spec은 "고정"이 아니라 *governed-mutable*: 변경 가능하되 **무엇을 바꾸느냐**에 따라
자율/리뷰/사람게이트/불변으로 차등한다. 핵심 불변식:

  성공을 정의하는 것(goal · acceptance_criteria · done_when)은 절대 자율 변경 불가.

이게 난이도 기반 goal-erosion("어려우니 합격선을 낮추자")을 코드로 차단한다.
anchor(order_raw)는 아예 불변. assumptions만 evidence가 있을 때 자율 적용된다.

tier 판정은 proposal.target의 머리(head, 첫 '.' 앞)로 한다:

  assumptions.*                         → auto-with-evidence (증거 있으면 자율 적용)
  constraints / non_goals               → review     (escalate)
  acceptance_criteria.*                 → review     (escalate; 기준=합격선)
  goal / done_when                      → human-gated (escalate; 성공 정의)
  order_raw                             → immutable   (escalate로 거부, 절대 적용 안 함)
  그 외/미지                            → escalate     (안전 기본값)

추가 안전: auto tier여도 proposal.from_이 현재 값과 불일치하면 stale 제안으로 보고
escalate한다(잘못된 전제로 덮어쓰기 방지).
"""

from __future__ import annotations

from dataclasses import dataclass

from haetae.models import ProjectSpec, SpecChange, SpecChangeProposal, State

# tier 분류 — head(첫 '.' 앞) 기준
_AUTO_HEAD = "assumptions"
_REVIEW_HEADS = {"constraints", "non_goals", "acceptance_criteria"}
_HUMAN_HEADS = {"goal", "done_when"}  # 성공 정의 — 자율 변경 불가
_IMMUTABLE_HEADS = {"order_raw"}  # anchor — 불변


@dataclass
class SpecChangeOutcome:
    """apply_spec_change 결과. applied면 루프 계속, 아니면 escalate로 종료."""

    applied: bool
    reason: str
    note: dict | None = None  # escalated일 때 pending_escalations에 실을 기록


def _head(target: str) -> str:
    return (target or "").split(".", 1)[0]


def apply_spec_change(
    spec: ProjectSpec, state: State, proposal: SpecChangeProposal
) -> SpecChangeOutcome:
    """제안을 정책에 따라 적용하거나 escalate 결정을 반환한다(부수효과는 applied일 때만)."""
    target = proposal.target or ""
    head = _head(target)

    def escalate(reason: str) -> SpecChangeOutcome:
        return SpecChangeOutcome(
            applied=False,
            reason=reason,
            note={
                "reason": reason,
                "spec_change": proposal.model_dump(by_alias=True, mode="json"),
            },
        )

    # ── 성공 정의·anchor: 자율 변경 절대 불가 ──
    if head in _IMMUTABLE_HEADS:
        return escalate("order_raw는 anchor라 불변 — 변경 거부(적용 안 함)")
    if head in _HUMAN_HEADS:
        return escalate(f"{head}는 성공 정의(human-gated) — 자율 변경 불가")
    if head in _REVIEW_HEADS:
        return escalate(f"{head}는 review tier — 자율 변경 불가(사람 리뷰 필요)")
    if head != _AUTO_HEAD:
        return escalate(f"미지 target {target!r} — 안전 기본값으로 escalate")

    # ── 여기부터 assumptions.* (auto-with-evidence) ──
    if not (proposal.evidence and proposal.evidence.strip()):
        return escalate("assumptions 변경에 evidence 없음 — 난이도 기반 의심으로 차단")

    parts = target.split(".", 1)
    if len(parts) != 2 or not parts[1]:
        return escalate(f"assumptions target에 id 없음: {target!r}")
    aid = parts[1]

    assumption = next((a for a in spec.assumptions if a.id == aid), None)
    if assumption is None:
        return escalate(f"assumptions.{aid}를 spec에서 찾을 수 없음 (stale 제안)")

    # from_ 일치 검사 — 제공됐는데 현재 값과 다르면 stale.
    if proposal.from_ is not None and proposal.from_ != assumption.text:
        return escalate(
            f"from_ 불일치 (제안 {proposal.from_!r} vs 현재 {assumption.text!r}) — stale 제안, 적용 거부"
        )

    if proposal.to is None:
        return escalate("assumptions 변경에 to(새 값) 없음 — 적용 불가")

    # ── 적용: assumption.text 갱신 + 버전업 + 감사 기록 ──
    old = assumption.text
    assumption.text = proposal.to
    spec.version += 1
    state.spec_version = spec.version
    state.spec_changes.append(
        SpecChange(
            seq=len(state.spec_changes) + 1,
            target=target,
            reason=proposal.reason,
            evidence=proposal.evidence,
            version=str(spec.version),
        )
    )
    return SpecChangeOutcome(
        applied=True,
        reason=f"assumptions.{aid} 갱신 ({old!r} → {assumption.text!r}); version={spec.version}",
    )
