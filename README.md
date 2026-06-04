# 해태 · haetae-agent

> 주문 하나로 검증된 완성까지 — **멈출 줄 아는** 자율 director.

해태(獬豸)는 옳고 그름을 가려내는 짐승이다. 이 프로젝트의 본질도 같다:
자율 에이전트가 *끝없이 일하게* 만드는 건 쉽다. 어려운 건 **언제 멈출지 판단하는 것**이다.

---

## 무엇인가

haetae는 한 줄짜리 주문(order)을 받아

```
spec → plan → dispatch → gate → replan
```

루프로 끝까지 모는 **자율 director**다. *검증된 완성(done)* 에 도달하거나 사람에게
escalate할 때까지 스스로 반복한다.

차별점은 "손(executor)"이 아니라 **GATE** — "이 정도면 됐다"를 자기채점 없이, 여러
조건으로, 빡세게 판정하는 부분이다. 구체적으로:

- **언제 멈출지 아는 governed GATE** — 기계 체크(exit-code)와 독립·적대적 LLM judge가
  합격선을 판정하고, 그 근거가 감사 로그로 남는다. executor가 "다 됐다"고 *자기보고*해도
  gate가 독립적으로 판단한다.
- **spec 거버넌스** — spec은 고정이 아니라 *governed-mutable*: 무엇을 바꾸느냐에 따라
  자율/리뷰/사람게이트/불변으로 차등한다. **성공을 정의하는 것(goal·기준·done_when)은
  사람이 쥔다** — 난이도 때문에 합격선을 자율로 낮추는 일(goal-erosion)을 막는다.
- **provider-agnostic** — 브레인(합성·replan·judge)과 executor가 Protocol 뒤에 있어
  provider를 갈아끼울 수 있다. 현재 구현은 codex.

worker-level 코딩 오케스트레이터나 단일 executor(주문을 한 번 실행하고 끝)와는 결이
다르다. 그것들은 *일을 한다*. haetae는 그 위에서 **일을 계획하고, 결과를 판정하고, 다음을
결정하는 루프**를 돈다 — 그리고 멈춰야 할 때 멈춘다.

---

## 빠른 시작

요구사항: Python ≥ 3.11, [`uv`](https://docs.astral.sh/uv/), 그리고 PATH에 설치된
`codex` CLI(브레인·judge·자율 executor가 `codex exec`를 호출한다).

```bash
uv sync
```

**사람 릴레이 모드**(기본) — haetae가 work order를 제시하면 사람이 실행하고 결과를 붙여넣는다:

```bash
uv run python -m haetae.run --order "회문 판별 함수와 테스트를 추가해"
```

**자율 모드**(opt-in) — codex가 직접 코드를 쓴다. 반드시 버리는 scratch 폴더에:

```bash
uv run python -m haetae.run \
  --order "회문 판별 함수와 테스트를 추가해" \
  --executor codex \
  --workdir ~/haetae-scratch \
  --state-path ~/haetae-scratch/state.yaml
```

전체 플래그는 `uv run python -m haetae.run --help` 참고:

| 플래그 | 뜻 |
|---|---|
| `--order` | 주문 원문 (필수) |
| `--workdir` | gate 체크 실행 + codex executor의 cwd (기본 `.`) |
| `--model` | executor/브레인 codex 모델 override (기본: codex 설정) |
| `--judge-model` | judge 전용 codex 모델 (executor와 다르게 줘 독립성 확보) |
| `--executor` | `human`(기본) 또는 `codex`(자율 쓰기) |
| `--state-path` | 최종 State를 저장할 YAML 경로 |
| `--max-iters` | 최대 루프 횟수 (기본 20) |

---

## 루프

```
order
  │
  ▼
synthesize ──► ProjectSpec (north-star, governed-mutable)
  │
  ▼
┌─────────────────────────────────────────────┐
│  replan  → Decision (next_order/retry/...)   │
│     │                                         │
│     ▼                                         │
│  dispatch → executor 가 work order 실행       │
│     │                                         │
│     ▼                                         │
│  gate    → GateResult(verdict + 근거)         │
│     │                                         │
│     ▼                                         │
│  기록    → State.events[].checks (감사 로그)  │
└─────────────────────────────────────────────┘
  │
  ▼
done · escalated · stopped (멈춤은 다조건)
```

루프는 LLM 출력 하나로 **crash하지 않는다**: 합성/replan 출력이 검증에 실패하면 직전
에러를 피드백으로 얹어 재시도하고, 소진되면 traceback 대신 escalated 상태로 종료한다.

멈춤 조건은 단일이 아니다:
- gate가 합격선을 판정하고 replan이 `done_when` 충족으로 판단 → **done**
- replan이 사람 판단을 요청 / governed 정책이 자율 변경을 거부 → **escalated**
- `--max-iters` 도달 → **stopped_stuck**

---

## GATE — 멈출 줄 아는 부분

`CompositeGate`는 수용 기준(acceptance criteria)을 타입별로 라우팅한다:

- **기계 기준**(`test`/`bench`/`lint`/`build`/`schema`, 명령 있음) → `CheckRunner`가
  실제로 셸 명령을 돌려 **exit-code**로 판정.
- **judge 기준**(`judge` 타입, UI·문서·"읽기 쉬운가" 같은 비기계 품질) → `LLMJudge`가
  **독립·적대적** read-only LLM으로 판정. 회의적 리뷰어로 프레이밍해 "기준을 *충족 못 한*
  이유"를 찾게 하고, executor 결과 요약뿐 아니라 **실제 산출 파일 내용**까지 읽혀 self-report
  합리화를 막는다. judge 모델은 executor와 다르게 줄 수 있다(decorrelation).
- **human / 명령 없음** → skipped → 집계상 ambiguous(사람 tier 필요).

집계: fail 하나라도 → `fail_recoverable`, 없고 skipped 있으면 → `ambiguous`, 전부 pass →
`pass`. judge 기준이 없거나 judge 모델이 없으면 judge는 **0회 호출**(기계 전용 spec은 비용·
행동 불변).

판정 결과는 verdict뿐 아니라 **per-check 근거**(무슨 명령을 돌려 어떤 exit-code/판정이
나왔는지)가 `GateResult`에 담겨 `State.events[].checks`에 기록된다 → state 파일이 진짜
**감사 로그**가 된다.

---

## spec 거버넌스 (mutability gradient)

spec 변경 제안(`propose_spec_change`)은 **무엇을 바꾸느냐**에 따라 차등 처리된다
(`apply_spec_change`):

| 대상(target) | tier | 행동 |
|---|---|---|
| `assumptions.*` | auto-with-evidence | `evidence`가 있고 현재 값과 일치하면 **자율 적용**(+버전업+감사). 증거 없으면 거부 |
| `constraints`, `non_goals` | review | escalate (사람 리뷰) |
| `acceptance_criteria.*` | review | escalate (기준 = 합격선) |
| `goal`, `done_when` | human-gated | escalate (성공 정의) |
| `order_raw` | immutable | 거부 — anchor는 절대 안 바뀜 |
| 그 외/미지 | — | escalate (안전 기본값) |

핵심 불변식: **성공을 정의하는 것(goal · acceptance_criteria · done_when)은 자율 변경
불가**, anchor(order_raw)는 불변. 새 정보로 *가정*을 갱신하는 것(evidence 기반)은 허용하되,
*어려우니 합격선을 낮추는 것*은 코드로 차단한다.

추가 안전: 자율 적용 직전 제안의 `from`이 현재 값과 일치하는지 확인해 stale 제안을 거부한다.

---

## 구성요소

| 파일 | 역할 |
|---|---|
| `models.py` | `ProjectSpec`(pinned, governed-mutable) / `State`(mutable: events·plan·spec_changes 감사). enum 강제 |
| `intake.py` | `synthesize`: order → ProjectSpec. 정규화 + 프롬프트 구조계약으로 출력 신뢰성 강화 |
| `replan.py` | `replan`: (spec, state, last_result) → Decision. 정규화 + 재시도 피드백 |
| `loop.py` | `run_loop`: 전체 오케스트레이션. LLM 출력에 crash 안 함, 진행 표시 콜백 |
| `providers/codex.py` | `CodexClient`: `codex exec`(read-only/ephemeral) 브레인. 저수준 `exec_codex` 공유 |
| `executors.py` | `HumanRelayExecutor`(사람=hands) / `CodexExecutor`(자율, workspace-write + workdir 범위) |
| `gate.py` | `CompositeGate` = `CheckRunner`(기계) + judge 라우팅. 공유 집계 헬퍼 |
| `judge.py` | `LLMJudge`: 적대적·독립 read-only judge(비기계 기준), 산출 파일 수집 |
| `spec_change.py` | `apply_spec_change`: mutability gradient 정책 |
| `run.py` | CLI 엔트리(`python -m haetae.run`) — 브레인/executor/gate 배선 |

스키마: `spec/projectspec.schema.yaml`, `spec/state.schema.yaml`. 프롬프트(IP):
`prompts/synthesizer.md`, `prompts/replan.md`, `prompts/judge.md`.

---

## 안전 모델

자율 executor(`--executor codex`)는 LLM이 만든 work order를 **쓰기 권한으로** 실행한다.
방어선은 두 겹이다:

1. 가장 좁은 쓰기 sandbox `workspace-write`(`danger-full-access`는 코드 화이트리스트가 차단).
2. 실행 범위를 `--workdir`로 한정(codex `-C`).

이건 **프로세스 수준 격리라 충분하지 않다.** 지금은 버리는 scratch 폴더에만 써라
(예: `~/haetae-scratch/...`). 진짜 repo에 물리려면 컨테이너/VM 하드 격리가 필요하며 그건
아직 **미구현**이다(후속 hardening).

---

## 상태 & 한계 (정직하게)

코어 루프(합성 → replan → dispatch → gate → governed spec-change)는 동작하고 테스트로
덮여 있다. 단, 다음은 **의도된 한계**이거나 **아직 미구현**이다:

- **격리**: 자율 executor는 프로세스 수준 `workspace-write` + workdir 범위뿐. 컨테이너/VM
  하드 격리는 미구현 — scratch 용도로만 써라.
- **budget / stuck**: 토큰·비용 예산이나 정체(stuck) 감지의 정식 처리는 미구현. 현재 상한은
  `--max-iters`뿐.
- **judge 독립성**: `--judge-model`을 주지 않으면 judge가 executor와 같은 모델일 수 있어
  decorrelation이 약하다(best-effort). 진짜 독립을 원하면 다른 모델을 명시하라.
- **spec-change 범위**: assumptions 적용은 `text`만 갱신한다. `confidence` 갱신이나
  assumption 추가/삭제는 미지원.
- **plan 유닛 상태 quirk**: gate가 매번 spec 전체 기준을 보므로, 부분만 끝난 단계에서도
  초기 유닛이 `in_progress`로 보일 수 있다(관측상 quirk).
- **codex 레이턴시**: 합성·replan이 `codex exec` 호출에 의존해 레이턴시가 크고 변동이 있다.

데몬·CI·패키징·멀티 provider·skill 레지스트리는 아직 만들지 않았다(부트스트랩 단계의
의도된 스코프). 설계 원리는 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) 참고.
