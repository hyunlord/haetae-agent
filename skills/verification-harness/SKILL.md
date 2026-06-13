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

## stdout 위생 (가장 흔한 실패 — 하니스가 *돌지만* 미파싱)
게이트는 트레이스 **stdout을 그대로 `JSON.parse`** 한다. stdout에 JSON 외 *한 글자라도* 섞이면
(npm 배너·`console.log`·진행 메시지·경고·스택) 파싱 실패 → 하니스가 **exit 0으로 깨끗이 실행돼도**
"구조화 JSON 아님"으로 fail한다. (캡스톤 #85: node 하니스가 0.37s에 부팅 성공했으나 stdout 노이즈로
계약 체크 미파싱 → 미수렴. 완주한 md-editor는 깨끗한 JSON-only stdout으로 통과.)

- **stdout = 단일 JSON만.** 트레이스 끝에 `process.stdout.write(JSON.stringify(evidence))` *한 번*.
  중간 상태를 stdout에 흘리지 마라(JSON 객체 하나가 통째로 `JSON.parse` 가능해야 한다).
- **로그·진단·진행 = `stderr`.** `console.log`(→stdout) 대신 **`console.error`/`process.stderr.write`**.
  디버그 출력도 전부 stderr. stdout은 *오직 결과 JSON* 전용.
- **npm 배너 억제.** `npm run` 경유면 **`npm run --silent <script>`** 로 npm 자체 출력을 끈다.
  더 안전하게는 **트레이스 스크립트를 `node`/`tsx`로 직접 실행**(npm 래퍼 우회):
  `"trace:behavior": "node --import tsx scripts/trace/behavior.ts"` → `cmd: "npm run --silent trace:behavior"`
  또는 acceptance `cmd`를 아예 `"node --import tsx scripts/trace/behavior.ts"`로. (md-editor가 간 길.)
- **자가검증 팁**: 로컬에서 `npm run --silent trace:x 1>out.json 2>err.log`로 돌려
  `JSON.parse(read(out.json))`가 성공하는지 확인하라 — stdout에 노이즈가 있으면 여기서 깨진다.

## test·빌드 cmd 위생 (스택-맞는 러너 — 임의 플래그 금지)
test/빌드 명령은 **스캐폴드(package.json `scripts`·`devDeps`)가 정한 러너**를 그대로 쓴다.
- **러너-특정 플래그를 임의로 더하지 마라.** vitest 스캐폴드에 Jest 플래그(`--runInBand`·
  `--testNamePattern`·`--ci` 등)를 붙이면 러너가 인식 못 해 크래시한다(캡스톤 #87 snake: vitest에
  `--runInBand` → exit≠0 → 미수렴·escalate).
- 기본은 **package.json 스크립트 그대로**: `npm test`, `npm run build`. 추가가 필요하면 *그 러너의
  네이티브 플래그*만(vitest: `run`·`--reporter`·`--run <file>`·`-t <name>`). 어느 러너인지 모르면
  package.json `devDependencies`를 보고 확인하라(vitest vs jest).
