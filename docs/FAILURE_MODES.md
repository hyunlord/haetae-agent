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
| **검증 역전(effort inversion)** | *검증기*가 검증 대상 전부보다 비쌈(캡스톤 #77: u7 하니스 13.21M > 앞 7유닛 합 12.25M, **데이터 확정**) | "트레이스 하니스" 열린 명세 → 빌더 과대 구현 (+ codex 고-input 재전송) | #78 계약으로 하니스 *부분* 좁힘(잔여 codex 고-input은 §3) | 🟡 부분 |
| **stale-status** | 죽은 run이 `running`+가짜 경과로 뜸 | state-status vs 실제 생존 미대조 | #75 heartbeat-age(2×idle)+launched-PID stale 표시 + `stopped_interrupted` | ✅ |

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
