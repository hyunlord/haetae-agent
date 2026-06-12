# haetae 실패 모드 카탈로그 (Failure Modes)

> 운영 playbook — haetae가 *실제로 발견한* 실패 모드 + 증상 + 근본 원인 + 가드(WO) + 상태. 새 실패는 여기 추가.
> Loop Engineering(ROADMAP §5)의 failure-modes 담론과 교차 — haetae는 *발견을 가드로 체계화*하는 게 차별점.

## 1. 가드된 실패 모드

| 실패 모드 | 증상 | 근본 원인 | 가드(WO) | 상태 |
|---|---|---|---|---|
| **사일런트 hang** | 대시보드 멈춤·수 시간 무이벤트 | codex 호출 무한 대기, idle timeout 없음 | #54 idle-timeout(무음 N초→kill→재시도/escalate) | ✅ |
| **비용 폭주** | 토큰 무제동 증가(캡스톤 44.8M) | 전역 예산 상한 없음 | #68B `--max-tokens` 전역 cap | ✅ |
| **크레딧 소진 크래시** | usage-limit→traceback→run 실패, done 상태 messy | usage-limit 미분류·uncaught 전파 | #68A graceful stop(`stopped_credit`)+#58 재개 | ✅ |
| **유닛 미수렴(돈 빨대)** | 한 유닛 토큰이 압도(u6 6.4M)·거기서 stall | 재시도+OR+통합OR *층 누적*에 ceiling 없음 | #68C 유닛 누적 ceiling→사람 escalate(**바 자동 미완화**) | ✅ |
| **통합 벽** | 유닛 개별 DONE인데 통합 gate 실패(머지충돌/크래시) | 통합 유닛이 엮는 유닛 미의존·stale 재빌드·되감기 없음 | #51 예방 + #48 치료 + #52 되감기(3종) | ✅ |
| **disjoint no-op** | 형제 유닛 파일 겹침→머지충돌, 방지 nudge 무효 | nudge가 criteria-강화 채널 공유→bar-가드에 collateral reject | #59 nudge + #72 전용 scope-only 채널 | ✅ |
| **합성 블라인드/state 지연** | 합성 중 빨간 에러·live↔state 모순 | state는 단계 경계 갱신, 합성 전 state 부재 | #66 관측성(차분한 합성 패널·라이브 배지·sticky) | ✅ |
| **이어가기 재빌드 낭비** | resume가 done 유닛 재빌드(u1 2.5M 재소모) | continue-from이 코드만 시딩·done 상태 미전달 | #71 깊은 증분(done 재사용·delta DAG, anti-erosion) | ✅ |
| **비용 불투명** | 44.8M이 어디 갔는지 안 보임 | event.cost가 mixed | #70 unit×tier×source×kind 분해(Σ=total 정합) | ✅ |

## 2. 코어 차별점 (자기-게이밍 방어 — 가드가 아니라 *존재 이유*)

| 실패 모드 | 증상 | 가드 |
|---|---|---|
| **자기 채점 게이밍** | 빌더가 `pass:true` 도장, 기계 체크(exit 0) 통과인데 행동 미입증 | 적대 run-judge(#22) + CompositeGate(#15–17) — **model vs critic-model 분리**. 캡스톤 `sim:trace exit 0인데 gate fail`이 라이브 증거. (CRDAL: 자기 검증 안 됨·분리 검증자 됨 — ROADMAP §5) |

## 3. 열린 실패 모드 (미가드 — 백로그)

| 실패 모드 | 증상 | 방향 |
|---|---|---|
| **검증 역전(effort inversion)** | sim보다 *검증기(u6)* 빌드에 더 많은 토큰 | **right-size rigor** — 검증 rigor·분해를 태스크 stakes에 맞게(direct-first then decompose + scaled bar). u6 fork(#67 트랜스크립트) 진단 후 spec |
| **고착(fixation)** | 같은 깨진 접근 반복 재시도 후에야 OR | **proactive anti-fixation(CRDAL)** — fixation 조기 감지→대안 능동 nudge. #68C 누적 ceiling의 능동 버전 |
| **stale-status** | 죽은 run이 `running`+가짜 경과로 뜸 | state-status vs 실제 프로세스 생존 대조 → "stale" 표시(#55 last-event-age가 부분 완화) |

## 4. 안티패턴 (하지 말 것)

- **exit-hook 세션 hijack 무한 강제 지속** — RWL 비판이 명시한 나쁜 패턴(= 로컬 CC 매직키워드 stop-hook 류). → haetae는 bounded 루프 + graceful stop + 사람 escalate.
- **자기 채점** — maker가 자기를 채점. → 분리된 checker(model≠critic-model, CRDAL 검증).
- **바 자동 완화로 stuck 유닛 통과** — anti-erosion 위반. → 사람 escalate(#68C), 바는 governed spec-change로만.
- **무제한 재시도/OR 비용** — → 유닛 누적 ceiling(#68C) + 전역 cap(#68B).
- **judge/critic에 빌더 컨텍스트 주입** — 적대 분리 침식. → 부모 context·스킬·tier·능력 후보·disjoint feedback은 *합성기/빌더·사람*만, judge/critic 무주입.
- **재사용으로 검증 우회** — done 유닛 재사용 시 바 바뀌었으면 도장 금지. → criteria 불변일 때만 재사용, 통합 gate 항상 실행(#71).

## 5. 새 실패 모드 추가 가이드
캡스톤/운영서 새 실패 발견 시: **증상 → 근본 원인 → (가드 WO 또는 백로그 방향) → 상태**로 위 표에 추가. 가드 전엔 §3(열린)에, 가드되면 §1로 이동. 코어 자기-게이밍 류는 §2.
