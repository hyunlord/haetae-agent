"""replan 러너 — (spec, state, last_result) → Decision.

director의 두 번째 능력: 게이트 판정이 난 직후 호출되어 다음 단 하나의 결정을 낸다.
replan.md를 시스템 프롬프트로, (spec + state + last_result)를 user로 태운다.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from haetae.llm import LLMClient
from haetae.models import Action, CheckType, Decision, ProjectSpec, State
from haetae.parsing import ParseError, parse_yaml_model

DEFAULT_PROMPT_PATH = "prompts/replan.md"

_CHECK_TYPES = {t.value for t in CheckType}


def degenerate_next_order(decision: Decision | None) -> str | None:
    """action이 next_order/retry인데 next_order 본문이 *비었으면* 재프롬프트용 피드백을 반환한다(WO#172).

    #171 라이브: 약 brain replan이 action은 next_order/retry로 내고 *next_order 본문(unit·goal)을 비워*
    루프가 즉시 escalate("next_order 본문 없음")하던 실패. 이건 **빈 산출**(파싱은 됨, 본문 부실)이지
    #54 idle(침묵)이 *아니다* — 그래서 파싱 실패(ReplanError)와 *동형*으로 에러-피드백 재프롬프트(#31)로
    흡수한다. 빈 = next_order None / unit blank / goal blank. 정상이면 None.

    **판정 아님** — brain 산출 *품질* 가드(director-side). gate/run_judge 무접촉. 반환 피드백은 다음
    replan 시도의 user 메시지에 얹혀 약 brain이 *완전한* next_order를 내도록 유도한다.
    """
    if decision is None or decision.action not in (Action.next_order, Action.retry):
        return None
    no = decision.next_order
    if no is None:
        return (
            "action을 next_order/retry로 냈는데 next_order 본문이 비었다(null). "
            "완전한 next_order(unit·goal 필수, 가능하면 local_checks·deliverable 포함)를 담아 "
            "Decision YAML을 다시(그리고 그것만) 출력하라."
        )
    if not (no.unit or "").strip() or not (no.goal or "").strip():
        return (
            "next_order의 unit 또는 goal이 비었다. 둘 다 채운 *완전한* next_order를 담아 "
            "Decision YAML을 다시(그리고 그것만) 출력하라."
        )
    return None


class ReplanError(ParseError):
    """replan 응답 파싱/검증 실패. raw 응답은 .raw_response에 보존된다."""


def _normalize_decision_dict(data: dict) -> dict:
    """replan(Decision) 출력의 흔한 변종을 스키마 모양으로 보정한다.

    흡수 대상(WO#12 live 실행에서 관측):
      - next_order.local_checks[].command → cmd
      - next_order.local_checks[].type이 CheckType에 없으면 → "test"
        (local_checks는 명령 기반 보조 체크라 test로 수렴해도 안전. "smoke" 등.)

    정규화는 만능이 아니다 — 못 잡는 변종은 그대로 검증 실패로 두고, 루프의
    재시도/escalate가 흡수한다. enum 자체는 canonical 유지(여기 경계에서만 보정).
    """
    if not isinstance(data, dict):
        return data
    d = dict(data)

    no = d.get("next_order")
    if isinstance(no, dict):
        no = dict(no)
        checks = no.get("local_checks")
        if isinstance(checks, list):
            new_checks = []
            for chk in checks:
                if isinstance(chk, dict):
                    chk = dict(chk)
                    if "cmd" not in chk and "command" in chk:
                        chk["cmd"] = chk.pop("command")
                    t = chk.get("type")
                    if t is not None and t not in _CHECK_TYPES:
                        chk["type"] = "test"
                new_checks.append(chk)
            no["local_checks"] = new_checks
        d["next_order"] = no

    return d


def _dump_yaml(model) -> str:
    return yaml.safe_dump(
        model.model_dump(by_alias=True, mode="json"),
        allow_unicode=True,
        sort_keys=False,
    )


def _build_user(
    spec: ProjectSpec,
    state: State,
    last_result: str,
    feedback: str | None = None,
) -> str:
    base = (
        "# spec (pinned · north-star)\n"
        f"```yaml\n{_dump_yaml(spec)}```\n\n"
        "# state (지금까지의 진행)\n"
        f"```yaml\n{_dump_yaml(state)}```\n\n"
        "# last_result (방금 executor가 돌려준 결과 + 게이트 판정)\n"
        f"{last_result}"
    )
    if feedback:
        base += (
            "\n\n# ⚠️ 직전 응답이 검증에 실패했다 — 아래 오류를 고쳐 "
            "Decision YAML을 다시(그리고 그것만) 출력하라\n"
            f"{feedback}"
        )
    return base


def replan(
    spec: ProjectSpec,
    state: State,
    last_result: str,
    client: LLMClient,
    prompt_path: str | Path = DEFAULT_PROMPT_PATH,
    *,
    feedback: str | None = None,
) -> Decision:
    """spec+state+last_result를 보고 다음 Decision을 합성해 반환한다.

    feedback: 직전 시도의 검증 에러. 주어지면 user 메시지에 얹어 모델이 스스로
              고치게 한다(루프의 재시도 경로에서 사용).

    실패 시(YAML 파싱 불가 / 스키마 검증 불통과) raw 응답을 담은 ReplanError.
    """
    system = Path(prompt_path).read_text(encoding="utf-8")
    user = _build_user(spec, state, last_result, feedback)
    raw = client.complete(system, user)
    # WO#173 #4: raw-빈 응답(파싱 *전* — #172 라이브서 14b가 빈 replan 텍스트 3× 반환)을 *명시 감지*해
    #   재프롬프트(#31)에 또렷한 피드백을 준다(parse_yaml_model의 'NoneType' 메시지보다 actionable).
    #   파싱불가는 parse_yaml_model이 ReplanError로 잡는다 — 둘 다 루프의 재시도→소진 시 결정적 fallback에 합류.
    if not (raw or "").strip():
        raise ReplanError(
            "replan 응답이 비었다(raw empty). verdict·action·rationale·(action이 next_order/retry면) "
            "next_order(unit·goal 필수)를 담은 완전한 Decision YAML을 다시(그리고 그것만) 출력하라.",
            raw or "",
        )
    return parse_yaml_model(raw, Decision, ReplanError, normalize=_normalize_decision_dict)
