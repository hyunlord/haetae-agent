---
name: verification-harness
triggers: [trace, 트레이스, headless, 헤드리스, e2e, harness, 하니스, sim:trace, playwright, puppeteer, chromium, behavior trace]
---

# 검증 하니스 패턴 (게이트 오프라인 환경서 도는 가벼운 node 트레이스)

검증 하니스(`run`/behavior trace 유닛)는 **게이트의 오프라인 clean-install 환경에서 실행**된다 —
네트워크도, 미리 깔린 브라우저 바이너리도 없다. 하니스가 거기서 *못 돌면* 행동을 검증할 수
없고(증거 emit 실패), 빌더가 하니스를 못 돌리는 채로 재시도를 반복해 예산만 태운다(검증 역전).
**그러니 하니스를 환경에 맞춰라 — 가벼운 node 트레이스로.**

## 핵심 원칙: node로 실행 가능한 헤드리스 트레이스
- 하니스는 **`node`(또는 tsx/ts-node)로 바로 실행**되는 스크립트여야 한다. 실제 엔진/로직 모듈을
  **`import`** 해 **순수 JS(또는 JSDOM)** 로 헤드리스 구동하고, `evidence_fields` JSON을 stdout에 emit.
- `package.json` 스크립트 예: `"trace:behavior": "node --import tsx scripts/trace/behavior.ts"`.
  (`vite`/`vitest`처럼 *이미 스캐폴드된* devDep만 쓴다 — 새 무거운 dep 추가 금지.)

## 실브라우저 E2E 금지 (게이트서 안 돈다)
- **playwright · puppeteer · chromium · 실브라우저 · headless Chrome 등 브라우저 바이너리 의존 금지.**
  이들은 별도 바이너리 다운로드/실행이 필요한데 게이트 환경은 **오프라인**이라 `exit 1` 난다.
  (캡스톤 #83: kanban `e2e:trace`·snake `trace:browser`가 이걸로 전체 비용 66~89% 태우고 미수렴.
  md-editor는 가벼운 node 트레이스로 완주.)
- 브라우저 API가 *정말* 필요하면(DOM 이벤트·layout rect 등) **JSDOM**으로 가볍게 흉내 내라 —
  실브라우저를 띄우지 마라. canvas 렌더는 검증 대상이 아니라 *로직*이 대상이다(아래).

## 로직-렌더 분리 (UI 앱도 엔진을 node서 trace)
- 엔진/상태/규칙 로직을 **DOM·canvas·렌더링에서 분리**해 별도 모듈로 둬라 — 그래야 node에서
  렌더 없이 import해 구동할 수 있다.
  - 스네이크: 충돌·성장·점수·속도 = 순수 `engine` 모듈(canvas 없이 tick 구동) → node trace.
  - 칸반: 카드 이동·필터·검색·persistence = 순수 `store`/`reducer` 모듈 → node trace.
  - 마크다운: 렌더 변환·자동저장 로직 = 순수 함수 → node trace(+ layout은 JSDOM rect).
- 키 입력·드래그 같은 인터랙션은 **로직 함수를 직접 호출**(또는 JSDOM 이벤트 dispatch)해 trace한다 —
  실브라우저 키 이벤트를 띄울 필요 없다.

## 구조화 JSON 증거 emit (run-judge가 행동을 판정하게)
- 트레이스는 **카운트만 찍지 마라.** acceptance가 요구하는 `evidence_fields`를 *정확한 키 이름*으로
  구조화 JSON에 담아 stdout에 emit하라(시나리오별 before/after 상태·좌표·불변식 위반 수 등).
- 값/행동이 *맞는지*는 독립 run-judge가 판정한다 — 너는 **실행되는 하니스 + 유효 증거**를 만들면 된다.
  자가채점(빌드가 스스로 `pass:true` 선언) 금지 — 채점은 게이트 몫.
