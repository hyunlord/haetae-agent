---
name: frontend-build
triggers: [react, vite, typescript, frontend, dashboard, canvas, ui]
---

# 프론트엔드 빌드 패턴 (캡스톤 교훈 인코딩)

director가 진짜 스택(React/Vite/TS)을 미리 스캐폴드해 host-install해 둔다. 그 위에서 구현하라.
plain-Node로 스택을 *치환하지 마라* — 스캐폴드된 실제 번들러/엔트리를 그대로 쓴다.

## 테스트
- **vitest를 사용하라.** 손수 만든 test-runner·`node --test`·자작 assert 스크립트로 갈음하지 마라.
- 테스트는 *실제 모듈을 import*해 행동을 검증한다(목/스텁으로 우회 금지).

## 엔트리/구조
- 진짜 React + Vite 엔트리: `index.html` + `src/main.tsx`(또는 `.jsx`)에서 루트를 마운트.
- 앱은 *실제 엔진/상태*를 import해 렌더한다 — 화면용 더미 데이터로 때우지 마라.
- `package.json`의 `dev`/`build` 스크립트가 실제로 동작해야 한다(`vite` / `vite build`).

## 검증은 gate 몫 (자가채점 금지)
- **빌드가 스스로 합격을 선언하는 자가채점 스크립트(`sim:judge` 식·자작 toy judge/test-runner)를
  만들지 마라.** run-judge가 그런 자기채점을 거부한다(캡스톤 ac8: 카운트 임계값으로 찍은
  `pass:true`를 독립 judge가 "행동 증거 없음"으로 기각).
- 채점·합격 판정은 독립 게이트(표준 테스트 러너 vitest, run-judge)가 한다. 너는 *만들기만* 한다.

## 동적 행동
- 움직임·실시간 갱신·인터랙션이 정확성에 걸리면, 헤드리스 트레이스 진입점을 제공해
  *실제 엔진 상태*를 tick별 구조화 JSON으로 stdout에 방출하라(build 성공만으론 행동 오류를 못 잡는다).
