# haetae 아키텍처

이 문서는 haetae의 **설계 원리**를 코드 파일과 매핑해 설명한다. 사용법은
[`../README.md`](../README.md), 작업 규약은 [`../CLAUDE.md`](../CLAUDE.md) 참고.

---

## 1. 데이터 모델 — spec vs state, 두 피드백 루프

haetae의 상태는 두 조각으로 갈린다(`models.py`):

- **`ProjectSpec`** — north-star 아티팩트. order에서 합성되며, 무엇이 "완성"인지
  정의한다(`goal`, `acceptance_criteria`, `done_when`). *governed-mutable*: 변경 가능하되
  거버넌스 정책의 통제를 받는다(§4).
- **`State`** — mutable 절반. 매 iteration 갱신된다. `events`(실행+판정 이력),
  `plan`(유닛 진행), `spec_changes`(거버넌스 감사 로그), `budget`/`pending_escalations`.

이 분리가 **두 개의 피드백 루프**를 만든다:

1. **빠른 루프 (실행)**: replan → dispatch → gate → event. spec은 그대로 두고 state만
   갱신하며 일을 진행한다.
2. **느린 루프 (거버넌스)**: 증거가 쌓이면 spec 자체를 governed하게 바꾼다(assumptions
   갱신 등). 성공 정의는 이 루프에서도 사람이 쥔다.

스키마 원본은 `spec/projectspec.schema.yaml`, `spec/state.schema.yaml`. pydantic v2 모델이
enum을 강제해 자유 문자열을 막는다.

핵심 반환 계약: gate는 verdict만 주는 게 아니라 `GateResult(verdict, checks)` — 근거를
**반환에 동봉**한다. 루프가 그 근거를 mutable 속성에서 몰래 꺼내는 게 아니라, 반환받아
`Event.checks`에 실어 state 파일을 진짜 감사 로그로 만든다.

---

## 2. 오케스트레이션 — `loop.py`

`run_loop`은 손으로 돌리던 director 루프의 코드판이다:

```
synthesize(order) → ProjectSpec
while running:
    decision = replan(spec, state, last_result)   # 검증 실패 시 재시도→escalate
    dispatch(decision):
        next_order/retry      → executor.run → gate.judge → Event 기록 → plan 갱신
        stop                  → done
        escalate              → escalated + 질문 기록
        replan_approach       → 접근 폐기, 다음 루프에서 재계획
        propose_spec_change   → apply_spec_change (§4): applied=계속 / 거부=escalated
```

executor와 gate는 `Protocol`(`Executor`, `Gate`)로 주입된다 — 구체 provider를 몰라도
오케스트레이션이 돈다. 그래서 테스트는 `MockExecutor`/`MockGate`/`MockClient`로 네트워크
없이 전 흐름을 검증한다.

**내성(crash 금지)**: LLM은 비결정적이라 깨진 출력을 낸다. 합성 실패는 traceback 대신
escalated state를 반환하고, replan 검증 실패는 직전 에러를 피드백으로 얹어 재시도한 뒤
소진되면 escalate한다. 단 하나의 나쁜 출력이 루프를 죽이지 않는다.

브레인(합성·replan·judge)은 `LLMClient` Protocol(`llm.py`) 뒤에 있고, 현재 구현은
`CodexClient`(`providers/codex.py`)다. `codex exec`를 read-only/ephemeral로 한 턴 돌려
최종 메시지만 캡처한다 — 저수준 `exec_codex` 헬퍼를 자율 executor와 공유한다.

---

## 3. GATE 철학 — 독립 · 적대 · 감사

gate는 haetae의 wedge다. 세 원리로 선다(`gate.py`, `judge.py`):

- **독립(independent)**: executor가 "다 됐다"고 *자기보고*해도 gate는 따로 판단한다. 기계
  기준은 실제로 명령을 돌려 exit-code를 보고, judge 기준은 executor와 **다른 모델**로 줄 수
  있는 read-only LLM이 본다(cross-provider decorrelation, best-effort).
- **적대(adversarial)**: `LLMJudge`는 회의적 리뷰어로 프레이밍된다(`prompts/judge.md`).
  "기준을 *충족 못 한* 이유를 찾아라, 명확·완전 충족일 때만 pass." self-report 요약만 믿지
  않고 workdir의 **실제 산출 파일**까지 읽혀 합리화를 막는다(용량 cap, 바이너리 제외).
- **감사(auditable)**: per-check 근거(`CheckReport`: 무슨 cmd로 어떤 exit-code/판정)가
  `GateResult`에 담겨 state에 남는다. verdict의 *근거*가 항상 추적된다.

라우팅(`CompositeGate`): 기준의 `check.type`으로 기계(`CheckRunner._run_check` 재사용) /
judge(`LLMJudge`로 한 번에 batch) / human·명령없음(skipped)을 나눈다. 집계 규칙은
`aggregate_verdict` 공유 헬퍼로 일원화(CheckRunner와 CompositeGate가 동일 규칙).

**비용 불변식**: judge 타입 기준이 없거나 judge client가 없으면 `LLMJudge`를 아예 만들지
않아 judge 호출 0회. 기계 전용 spec은 judge 도입 전과 비용·행동이 동일하다.

---

## 4. spec 거버넌스 — new-info 허용 / difficulty 차단

`apply_spec_change`(`spec_change.py`)는 변경 제안을 **무엇을 바꾸느냐(target head)** 로
차등한다. 근본 원리 하나:

> 새 *정보*로 가정을 갱신하는 것은 허용한다. *어려움*을 이유로 합격선을 낮추는 것은 막는다.

| target head | tier | 처리 |
|---|---|---|
| `assumptions` | auto-with-evidence | evidence 있고 `from` 일치 → 자율 적용 + 버전업 + 감사 |
| `constraints`/`non_goals`/`acceptance_criteria` | review | escalate |
| `goal`/`done_when` | human-gated | escalate |
| `order_raw` | immutable | 거부 |
| 그 외 | — | escalate (안전 기본값) |

성공을 정의하는 3종(goal · acceptance_criteria · done_when)은 auto 경로에 도달하기 전에
무조건 escalate되므로 **코드로** 자율 변경이 불가능하다 — evidence가 있어도. anchor
(order_raw)는 불변. 이게 난이도 기반 goal-erosion을 구조적으로 차단한다.

자율 적용은 부수효과 3종을 묶어 처리한다: ① 해당 assumption의 `text` 갱신 ②
`spec.version`/`state.spec_version` +1 ③ `state.spec_changes`에 감사 기록 append. 적용 직전
제안의 `from`이 현재 값과 다르면 stale로 보고 거부한다(잘못된 전제로 덮어쓰기 방지).

---

## 5. 한계 (설계상)

README의 "상태 & 한계"와 동일하다. 요약:

- 격리는 프로세스 수준(workspace-write + workdir)뿐 — 하드 격리 미구현, scratch 전용.
- budget/stuck 정식 처리 미구현 — 상한은 `--max-iters`뿐.
- judge 독립성은 `--judge-model`을 줘야 강해짐(미지정 시 best-effort).
- spec-change는 assumptions의 `text`만 — confidence/추가·삭제 미지원.
- plan 유닛 상태는 gate가 매번 전체 기준을 봐서 in_progress로 보일 수 있는 관측 quirk.
- codex 합성·replan 레이턴시가 크고 변동.

이 한계들은 부트스트랩 단계의 의도된 스코프다. 코어(spec→gate→governance)를 먼저
얇게 세우고, 격리·budget·멀티 provider는 후속 hardening으로 둔다.
