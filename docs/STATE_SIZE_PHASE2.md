# state.yaml 크기 측정 (OMC #3 phase 2)

> WO#109 · 측정일 2026-06-14 · 대상: 실제 완주 run 20건(`~/haetae-scratch/runs/*`) · 측정 시점: **post-#102**(trace 오프로드 적용 후)

#102가 ArtifactDescriptor + bounded-handoff(8KB)를 `RunEvidence.trace`에 적용해 state를 96%↓(41KB→1.5KB)
시켰다. phase 2의 질문은 단 하나: **trace를 뺀 뒤 state.yaml에서 무엇이 임계(8KB)를 넘는 *write-once 큰
블롭*으로 남아 있나?** — 넘는 게 있으면 같은 descriptor 패턴을 적용하고, 없으면 정직히 그렇게 보고한다
(fake work 금지).

## 1. 측정 방법

`_save_state`와 *동일한* 직렬화 경로로 잰다: `state.model_dump(by_alias, mode="json")` →
`offload_state_artifacts(...)`(post-#102 = 큰 trace 오프로드) → `yaml.safe_dump(...)`. 그 dict를
(a) top-level 필드별 바이트 기여, (b) 개별 문자열 leaf 크기로 분해했다.

## 2. 측정 결과 — 대표 run 필드별 기여 (post-#102)

### kanban-3of3 (총 35.5KB → 트레이스 모두 <8KB라 오프로드 영향 없음)
| 필드 | 바이트 | 비고 |
|---|---|---|
| **events** | 29,374 | **81%** — append-only 타임라인(아래 §4) |
| cost_parts | 3,519 | #70 레저(24 leaf) — <8KB·권위·dashboard-live |
| spec_critique | 1,381 | |
| transitions | 1,258 | append-only(22) |
| 나머지 합 | ~1,100 | budget·plan·spec_ref·빈 리스트들 |

### crowdsim-ns (79.8KB → **42.6KB** post-#102; 19KB trace 2건이 빠짐)
| 필드 | 바이트 | 비고 |
|---|---|---|
| **events** | 32,201 | **76%** — append-only 타임라인 |
| cost_parts | 5,124 | #70 레저(35 leaf) — <8KB |
| spec_critique | 2,396 | |
| transitions | 1,926 | append-only(34) |
| 나머지 합 | ~900 | |

### 개별 write-once 문자열 leaf — 전체 20 run 스캔 (post-#102)
- **임계(8KB) 초과 write-once 문자열 블롭: 0건.**
- 최대 `result` = **3,253 B** (cap2b), 최대 `learnings` = **42 B**.
- 남은 가장 큰 leaf들은 전부 `RunEvidence.trace`(최대 5,049 B)인데 *이미 #102 메커니즘 대상*이며 임계 미만이라 인라인 유지된 것뿐. trace 외 필드 중 8KB를 넘는 단일 블롭은 없다.

## 3. 결론 — phase 2 = 측정-기반 **적용 no-op (정직 보고)**

1. **trace가 지배적이었다.** #102가 그 단일 큰 블롭을 이미 제거했다. 그 뒤 남은 것 중 8KB 임계를 넘는
   *단일 write-once 블롭*은 20개 실 run 어디에도 없다.
2. **WO가 1순위로 지목한 `prompt`(work order 텍스트) 필드는 state.yaml에 존재하지 않는다.** `Event`에는
   짧은 참조 `work_order_ref: str | None`(예: `"resume(parent-done)"`, `"reuse_of=…"`, ~20–50 B)만
   기록되고, 빌더에 주입되는 *전체* work order 프롬프트 텍스트는 애초에 지속되지 않는다. → descriptor를
   걸 대상 자체가 없다.
3. 따라서 `loop.py`/`artifacts.py`에 **새 오프로드 코드를 추가하지 않는다**(YAGNI — 트리거될 블롭이
   없는 코드는 speculative). artifacts.py 인프라는 이미 kind-범용(`write_artifact(kind=…)`)이라, *미래에*
   어떤 write-once 블롭이 8KB를 넘으면 재사용만으로 적용 가능하다(이 readiness는 테스트로 박제 — §5).

## 4. 비대상(정직히 명시 — WO와 동일)

- **events 타임라인**: post-#102 최대 기여 필드지만 **append-only 리스트**(`state.events.append(ev)`,
  loop.py)다. 단일 hashed-artifact descriptor에 부적합 — 매 이벤트마다 전체를 재기록해야 해 비효율(WO가
  명시한 제외 사유 그대로). 게다가 그 안의 개별 leaf(`result` ~1.6KB, `learnings` ~수십 B, 작은 trace,
  checks/cost)는 전부 임계 미만이다. dashboard가 이벤트별로 인라인 읽는다. → **인라인 유지.** 별도
  append-log 사이드카는 이번 범위 밖(후속 후보).
- **cost 레저(#70 `cost_parts`)**: budget.spent의 권위 원천이자 dashboard가 source×tier×kind로 라이브
  드릴다운하는 대상. 측정상 3.5–5.1KB(임계 미만). → **인라인 유지.**

## 5. 안전·검증

- 코드 무변경(loop·artifacts·gate·judge·run-judge·#82-B·codex·ALLOWED_SANDBOXES 전부 불변). 추가는
  이 문서 + 측정을 박제한 테스트(`tests/test_state_size_phase2.py`)뿐 → **판정 무관·back-compat·무회귀.**
- 테스트가 박제하는 것: (a) 대표 State를 #102 경로로 직렬화 시 events가 지배 필드이되 trace 외 단일
  write-once 문자열 leaf가 8KB를 넘지 않음, (b) `State` 스키마에 `prompt` 필드 부재(`work_order_ref`만),
  (c) artifacts 인프라의 kind-범용 round-trip(`prompt`/`result`/`cost` kind에도 동형) = phase-2 readiness.
