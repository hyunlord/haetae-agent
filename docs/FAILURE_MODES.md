# haetae 실패 모드 카탈로그 (Failure Modes)

> 운영 playbook — haetae가 *실제로 발견한* 실패 모드 + 증상 + 근본 원인 + 가드(WO) + 상태. 새 실패는 여기 추가.
> Loop Engineering(ROADMAP §5)의 failure-modes 담론과 교차 — haetae는 *발견을 가드로 체계화*하는 게 차별점.
> **캡스톤 진척(retail-crowd-sim)**: hang(#54)→비용폭주(#68B)→크레딧크래시(#68A)→CLI 캡 즉사(#76)→**진단 완료(#77): sim 깨짐(그리드락) + 계약 불일치**. 매 run이 정확한 갭 하나씩 드러내며 사다리를 오름.

## 1. 가드된 실패 모드

| 실패 모드 | 증상 | 근본 원인 | 가드(WO) | 상태 |
|---|---|---|---|---|
| **사일런트 hang** | 대시보드 멈춤·수 시간 무이벤트 | codex 호출 무한 대기, idle timeout 없음 | #54 idle-timeout(무음 N초→kill→재시도/escalate) | ✅ |
| **비용 폭주** | 토큰 무제동 증가(캡스톤 44.8M) | 전역 예산 상한 없음 | #68B `--max-tokens` 전역 cap | ✅ |
| **크레딧 소진 크래시** | usage-limit→traceback→run 실패, done 상태 messy | usage-limit 미분류·uncaught 전파 | #68A graceful stop(`stopped_credit`)+#58 재개 | ✅ |
| **유닛 미수렴(돈 빨대)** | 한 유닛 토큰이 압도·거기서 stall | 재시도+OR+통합OR *층 누적*에 ceiling 없음 | #68C 유닛 누적 ceiling→사람 escalate(**바 자동 미완화**) | ✅ |
| **통합 벽** | 유닛 개별 DONE인데 통합 gate 실패(머지충돌/크래시) | 통합 유닛이 엮는 유닛 미의존·stale 재빌드·되감기 없음 | #51 예방 + #48 치료 + #52 되감기 | ✅ |
| **disjoint no-op** | 형제 유닛 파일 겹침→머지충돌, 방지 nudge 무효 | nudge가 criteria-강화 채널 공유→bar-가드에 collateral reject | #59 nudge + #72 전용 scope-only 채널 | ✅ |
| **합성 블라인드/state 지연** | 합성 중 빨간 에러·live↔state 모순 | state는 단계 경계 갱신, 합성 전 state 부재 | #66 관측성(차분한 합성 패널·라이브 배지·sticky) | ✅ |
| **이어가기 재빌드 낭비** | resume가 done 유닛 재빌드 | continue-from이 코드만 시딩·done 상태 미전달 | #71 깊은 증분(done 재사용·delta DAG, anti-erosion) | ✅ |
| **비용 불투명** | 토큰이 어디 갔는지 안 보임 | event.cost가 mixed | #70 unit×tier×source×kind 분해(Σ=total 정합) | ✅ |
| **계약 불일치 = 텅 빈 검증** | 하니스가 judge가 *못 쓰는* 필드 emit → 게이트가 "증거 없음"으로 fail, *행동* 판정 불가(캡스톤 #77 결정적 발견) | acceptance 요구 증거 필드 ↔ 하니스 출력 불일치; "트레이스 하니스 만들어라"가 열린 명세 | #78 evidence-contract(criteria서 필드 추출→하니스 작업지시서 주입→게이트 *결정적 필드존재* 강제; *행동* 판정은 적대 run-judge 그대로=분리 보존) | ✅ |
| **CLI 캡 즉사** | `--max-tokens` 주면 합성 전 `TypeError: run() got unexpected keyword` 즉사 | 캡이 파서·main·run_loop엔 있는데 *중간 래퍼 run() 시그니처* 누락; 단위테스트가 main→run 전체경로 미검증 | #76A run() 배선 + 전체경로 통합 테스트(재발 차단) | ✅ |
| **합성-전-실패 raw 에러** | state 생기기 전 죽은 run이 빨간 raw FileNotFoundError | #66은 합성 *중*(heartbeat 활성)만 graceful, 합성 *전* 실패 미처리 | #76B graceful "합성 전 실패" 패널 + 원 주문 | ✅ |
| **검증 역전(effort inversion)** | *검증기*가 검증 대상 전부보다 비쌈(캡스톤 #77: u7 하니스 13.21M > 앞 7유닛 합 12.25M, **데이터 확정**) | "트레이스 하니스" 열린 명세 → 빌더 과대 구현 (+ codex 고-input 재전송) | #78 계약 + #84 node 트레이스 + #86 stdout 위생 → **#95 실증 해소**: crowd-sim서 build+retry 25.9M(97%)=실 sim 빌드, 하니스(u7/u8 헤드리스 트레이스)는 가벼움(66~89% 독식 사라짐). 잔여 codex 고-input은 §3 | ✅ |
| **stale-status** | 죽은 run이 `running`+가짜 경과로 뜸 | state-status vs 실제 생존 미대조 | #75 heartbeat-age(2×idle)+launched-PID stale 표시 + `stopped_interrupted` | ✅ |
| **하니스가 게이트서 못 돎** | 빌더가 실브라우저 E2E(playwright/chromium) 선택 → 오프라인 게이트서 exit 1, 하니스가 전체 비용 66~89% 독식·미수렴(캡스톤 #83/#85) | "트레이스 하니스" 열린 명세 → 빌더가 게이트 환경(오프라인·브라우저 바이너리 없음)서 못 도는 하니스 고집 | #84 하니스를 *node 트레이스*로 유도(엔진 import+순수 JS/JSDOM, 실브라우저 회피) — 비용 −53~56%·governed escalate | ✅ |
| **하니스 stdout 노이즈 = 미파싱** | node 하니스가 *돌지만*(exit 0, 0.37s) stdout에 npm 배너·console.log 혼입 → 게이트 JSON.parse 실패 → 계약 fail(캡스톤 #85) | stdout이 결과 JSON 전용이 아님(로그·배너가 같이 stdout으로) | #86 stdout=단일 JSON-only·로그는 stderr·npm 배너 억제(`--silent`/직접 node) — 빌더-측 유도 | ✅ |
| **test-cmd 스택 불일치** | vitest 스캐폴드에 Jest 플래그(`--runInBand`·`--testNamePattern`) → 러너 크래시 → 미수렴(캡스톤 #87 snake u2) | 재합성 test cmd가 스캐폴드 러너(devDeps=vitest)와 불일치 | #88A test/빌드 cmd를 스캐폴드 러너에 맞게 유도(러너-특정 플래그 임의추가 금지) — 빌더-측 | ✅ |
| **합성 temp-race 크래시** | 합성 중 `OSError: Directory not empty: .omx/state` → run 크래시(transient, 캡스톤 #87) | oh-my-codex MCP의 .omx/state 비동기 쓰기 ↔ codex temp `TemporaryDirectory` rmtree cleanup 경합 | #88B `TemporaryDirectory(ignore_cleanup_errors=True)` 1줄(ALLOWED_SANDBOXES·실행 로직 불변) | ✅ |

## 2. 코어 차별점 (자기-게이밍 방어 — 가드가 아니라 *존재 이유*)

| 실패 모드 | 증상 | 가드 |
|---|---|---|
| **자기 채점 게이밍** | 빌더가 `pass:true` 도장, 기계 체크(exit 0) 통과인데 행동 미입증 | 적대 run-judge(#22) + CompositeGate(#15–17) — **model vs critic-model 분리**. 캡스톤 `sim:trace exit 0인데 gate fail`이 라이브 증거. (CRDAL: 자기 검증 안 됨·분리 검증자 됨 — ROADMAP §5) |

> **#78 보강**: 계약 체크는 *필드 존재*(결정적)만, *값/행동*은 적대 run-judge(LLM). 값 임계를 결정적 강제로 끌어올리면 *카운트 임계 자기채점* 실패모드가 되살아나므로 경계 유지(§4).

## 3. 열린 실패 모드 (미가드/진행 — 백로그)

| 실패 모드 | 증상 | 방향 |
|---|---|---|
| **고착(fixation)** | 같은 깨진 접근 반복 재시도 후에야 OR(캡스톤 #77 그리드락 재빌드 위험) | **#79 proactive anti-fixation(CRDAL)** — 같은-사유 재발 조기 감지 → 빌더 전용 *구조적 대안* nudge(OR/ceiling 前). *작성됨·검증 대기* |
| **빌더 행동 품질(그리드락)** | sim이 빌드·실행되나 혼잡서 에이전트 상호 차단(96~98% blocked, 캡스톤 #77) | **게이트는 정확히 잡음(가드 작동)** — 빌더 역량 레버: #79 anti-fixation + #32 충돌회피(RVO/flow-field) 스킬. 게이트 그리드락 fail→재빌드 루프 |
| **검증 역전 잔여(codex 고-input)** | 하니스 단일 build 13.16M *input*(agentic 전체맥락 재전송) | #78이 계약으로 좁혔으나 codex 측 컨텍스트 재전송은 director 밖 — 하니스 분해 더 잘게 / right-size 후속 |
| **바 비례성** | ac8 `sample≥200`·ac7 거의-전원-spawn이 데모치곤 공격적(비구속) | right-size: 임계를 stakes에 맞게 trim(부차, 캡스톤 #77) |
| ✅ **continue-from reuse-거부 → rebuild-all** *(#91 해소·#92 검증)* | 재합성이 criteria/분해를 매번 바꿔 #71 reuse 거부 → done 유닛 재빌드로 resume 절약 0 + plan 비대(반복 #81·kanban-r2/r3·snake-r2/r3) | **#91 순수 재개**가 부모 plan/criteria 보존(spec.yaml 로드·재합성 skip)→reuse 매칭으로 해소(#92 실루프: done 재빌드 0·통합 11.5M). 잔여는 신규 행 'OR 통합-대안 ↔ #91 비일관' |
| ✅ **하니스 키워드 과매칭** *(#99 해소·#100 검증)* | 스캐폴드/준비 유닛(desc에 "trace" 등)이 하니스로 오탐 → 트레이스 미생산인데 계약 부착 → 못 채워 fail(캡스톤 #89 snake u0) | **#99**: 하니스 탐지를 "키워드 언급"→"증거-생산(run/sim:trace 체크·evidence_fields/scenario_steps 보유)" 게이트로(준비/스캐폴드 제외). gate is_harness가 계약-구동이라 intake 분류만 수정. #100서 u5(DnD 구현) 오탐 0 입증 |
| **큰 plan budget 초과** | 다유닛+하니스 검증(per-unit self-check+재시도)이 통합 run-judge 前 전역 캡 소진(캡스톤 #87·#89 kanban 7유닛 >20M) | 캡 상향 또는 plan-trim·재시도 효율·하니스-특화 비용 ceiling |
| ✅ **OR 통합-대안 ↔ #91 비일관** *(#97 해소·#100 검증)* | 통합 gate 실패 시 #41/#52 OR-대안이 seeded-done 포함 *전체* 유닛 리셋 → #91 reuse 상실·병렬 머지충돌 재발 → escalate(캡스톤 #92 kanban-r4, u1 머지 미수렴) | **#97**: 통합 실패에 *연루된 유닛만* 리셋(실패 기준→소유 유닛 #26/#72 매핑·seeded-done 보존·폴백 전체 리셋) → #91 reuse와 정합. #100 kanban fresh 완주서 OR 미발동(전 유닛 first-try) |
| ✅ **하니스 시나리오 결함** *(#98 해소·#100 검증)* | 필드(#78)·종류(#84)·stdout(#86) 다 통과해도 *시나리오 로직*(무엇을 어떻게 구동하나)이 틀리면 run-judge가 정확히 fail(false-negative 아닌 정당 fail; #92 kanban ac3 DnD 같은-카드 미이동·ac5 생성카드 삭제후reload) | **#98**: 합성기가 evidence_fields와 함께 scenario_steps(기준 입증 흐름) 유도 + verification-harness 스킬 흔한-실수 회피(완전 흐름·같은 엔티티 전 상태·검사 前 보존). #100서 ac6 DnD·ac5 persistence 통합 pass로 입증 |
| ✅ **속도-절단 회피 = liveness hack → 최종 종결** *(#112 probe·#115 v2·#116·#119·#121·#124 RUN)* | #112 stress sweep: 고밀도서 stall이 *아니라* overlap — 동시 ~15체 초과 시 겹침 발생·급증(active 96서 에이전트당 매틱 ~2겹침). deadlock·stuck은 0 유지(교착 실패모드를 overlap 실패모드로 맞바꾼 liveness hack, 멈추는 대신 통과). #95(active=12)는 onset 바로 아래라 0겹침=저밀도 아티팩트 | **중밀도 해소(#115 v2 stop-not-pass·#116 실증)**: 빌더가 "충돌-free 없으면 정지(통과 금지)" 채택 → 24체·overlap 0·deadlock 0·100% 완주(run-judge 7/7). **단 열림**: tight-packing 근접 밀도(#112-onset, min_pair 작은) + 완전 ORCA half-plane/reciprocal-share 채택은 미정복(통과 트레이스 min_pair=209=분산). → 더 강한 밀도 계약(작은 월드/높은 concurrent)으로 probe(#118/#120). **#121 RUN 확증**: sustained tight-packing서 속도-절단 엔진 attempt-0 *완전 붕괴*(overlap_violations 18.4M·min_edge_gap -0.48·deadlock 1176틱·wall-crossing 10487) = liveness hack 입증(멈추는 대신 통과). OR 예약/슬롯 모델 유망하나 *머지 모드*로 미검증(→ §3 'OR-재빌드 의미충돌' 행). **✅ #124 최종 종결**: #123 머지수정 후 빌더가 **overlap-projection 엔진**으로 tight 근접 collision-free 달성(sustained tight-packing 1800틱 하 overlap_violations **0**·min_center 14.25≥임계 14·100% 완주·deadlock 0·wall-crossing 0). 충돌회피 천장 *미도달* — 바 상향 + 스킬 하 빌더가 진짜 collision-free까지 레벨업(속도-절단→stop-not-pass→예약/슬롯→overlap-projection) |
| ✅ **시나리오 밀도 커버리지 부족** *(#114 해소·#116 실증)* | 저밀도 검증 시나리오는 *밀도-의존* 실패(overlap)를 구동 못 해 적대 gate가 못 잡음 — gate는 결백, 시나리오 커버리지가 병목(#95가 저밀도라 #112 한계가 안 드러남) | **#114**: 합성기 scenario_steps + verification-harness 스킬이 sim/crowd 기준을 현실/혼잡 밀도 구동으로 유도(밀도-하 overlap/min_sep 측정). **#116 실증**: 빌더 하니스가 *spawned=1*(저밀도 아티팩트)로 내자 적대 run-judge가 "단일 고객으론 혼잡 입증 불가"로 거부→OR 재빌드→24체 구동 DONE. 시나리오 커버리지=gate 엄밀성 상한이 실루프 작동(ROADMAP §0) |
| ✅ **OR-재빌드 의미/빌드-수준 충돌** *(#121 발견·#123 해소·#124 실증)* | 유닛 *핵심 모델*을 재작성하는 OR 재빌드(#121 u1 예약/슬롯 흐름)가 *공유 인터페이스/계약*을 바꿔 이미 머지된 형제(u2 layout)와 *빌드 수준* 충돌. `conflict_files=[]` 빈 목록(텍스트 충돌 아님) 3회 → #48 통합-적응 재빌드가 "형제 보존" 요구하나 재작성이 공유 계약을 계속 변경 → 루프(tier medium→high→xhigh) → escalate(u3 충돌-코어 재빌드 안 됨·2차 통합 run-judge 미실행) | **#123**: 빈 conflict_files(=의미/빌드 신호) → 계약-소비 형제(scope-겹침/양방향 dep/빌드-에러 귀속) 리셋(`contract_consuming_siblings`) → 소비자+유닛 공동 재빌드(#97 연루-유닛을 *의미-계약 의존*까지 확장, reset-범위만). **#124 실증**: 의미충돌 1회 → 소비-형제 리셋 1회 → u1/u2/u3 공동 재빌드 → 전원 머지·머지 루프 0·escalate 0 → 2차 통합 run-judge 실행→근접 collision-free done(commit 2f8a1c6) |

## 4. 안티패턴 (하지 말 것)

- **exit-hook 세션 hijack 무한 강제 지속** — RWL 비판이 명시한 나쁜 패턴(= 로컬 CC 매직키워드 stop-hook 류). → haetae는 bounded 루프 + graceful stop + 사람 escalate.
- **자기 채점** — maker가 자기를 채점. → 분리된 checker(model≠critic-model, CRDAL 검증).
- **값 임계의 결정적 자기채점** — "카운트 ≥ N이면 pass" 류를 게이트가 직접 도장(원조 실패모드). → 결정적 체크는 *필드 존재*까지만, *값/행동*은 적대 run-judge(LLM)에 남김(#78 경계).
- **바 자동 완화로 stuck 유닛 통과** — anti-erosion 위반. → 사람 escalate(#68C), 바는 governed spec-change로만.
- **무제한 재시도/OR 비용** — → 유닛 누적 ceiling(#68C) + 전역 cap(#68B) + 조기 anti-fixation(#79).
- **judge/critic에 빌더 컨텍스트 주입** — 적대 분리 침식. → 부모 context·스킬·tier·능력 후보·disjoint feedback·evidence-contract·anti-fixation nudge는 *합성기/빌더·사람*만, judge/critic 무주입.
- **재사용으로 검증 우회** — done 유닛 재사용 시 바 바뀌었으면 도장 금지. → criteria 불변일 때만 재사용, 통합 gate 항상 실행(#71).

## 5. 새 실패 모드 추가 가이드
캡스톤/운영서 새 실패 발견 시: **증상 → 근본 원인 → (가드 WO 또는 백로그 방향) → 상태**로 위 표에 추가. 가드 전엔 §3(열린)에, 가드되면 §1로 이동(부분이면 🟡). 코어 자기-게이밍 류는 §2.
