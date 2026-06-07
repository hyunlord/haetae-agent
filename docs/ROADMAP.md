# haetae-agent 로드맵

> 해태(獬豸) = 시비·선악을 판별하는 신수. 차별점 = **언제 done이 아닌지 아는 governed GATE.**
> autonomous director: 의뢰 하나 → governed spec → `synthesize → replan → dispatch(executor) → gate → replan` 루프 → done/escalate/stop.
>
> **최종 갱신: 2026-06-08 · WO#1–35 완료 · 341 tests · main @510e14b**

---

## 0. 설계 논제 (검증됨)

- **범용 LLM + agentic harness**로 충분 (특화 모델 불필요). executor는 pluggable(codex, 향후 Claude Code 등).
- 차별점 3축: **governed GATE**(다조건 종료 + 적대적 판정으로 자기합리화 차단) · **spec governance**(mutability gradient, anti-erosion) · **provider-agnostic**.
- 검증 사건: 캡스톤에서 빌드가 *자기 채점 스크립트(카운트 임계값)*로 `pass:true` 도장을 찍었으나, **독립 run-judge가 "행동 증거 없음"으로 거부**(ac8). 같은 명령이 기계 체크(exit 0)론 통과·run-judge론 실패 — 적대적 게이트의 존재 이유가 그대로 증명됨.
- **수렴 검증**: Claude Code *Dynamic Workflows*(오케스트레이션 코드화·병렬 서브에이전트·적대적 교차검증·테스트=bar)와 Google *LEAP*(분해·verifier-guided·DAG·범용 LLM)가 같은 자리에 도달. (단, Claude Code 모니터링은 터미널 진행 수준 — haetae는 영속 구조화 state로 더 풍부한 시각화.)

---

## 1. 완료 (WO#1–35, 341 tests)

| 영역 | WO | 내용 |
|---|---|---|
| 코어 | 1–11 | 데이터/모델, intake(synthesize), CodexExecutor, replan, 루프 드라이버, CheckRunner gate, HumanRelay |
| 신뢰성 | 12–14 | 합성기 하드닝, 루프 회복(재시도/escalate), autonomous executor(offline sandbox) |
| 적대적 게이트 | 15–17 | 적대 LLMJudge + CompositeGate, governed spec-change(mutability gradient), consolidation 문서 |
| 자기개선 | 18–20 | richer progress + non-fatal save, **adversarial spec critic**(soft→1회 재합성), critic best-effort |
| 병렬 | 21 | DAG scheduler + worktree 격리 + 보장 cleanup + 머지충돌→직렬화 |
| run-judge | 22 | `run` 체크 + run_harness + 동적 run-judge(행동 판정) + judge 부재 시 degrade |
| 네트워크 | 23 | **호스트 deps 설치**(sandbox 불변, 해시캐시, non-fatal, node_modules gitignore) |
| 캡스톤 준비 | 24–25 | run-기준+trace 진입점 합성 유도, 헤드룸, plan-state 현실 반영, clean-install 실패=gate 신호 |
| per-unit | 26 | **유닛별 acceptance criteria**(per-unit gate는 자기 기준만, 통합 gate는 전체) — 기반 유닛 escalate 버그 수정 |
| 선제 스캐폴드 | 27 | **director가 executor 작업 전에 진짜 스택(React/Vite/TS) 생성+설치** → 스택 치환 차단, sandbox는 계속 offline |
| 대시보드 | 28, 35 | read-only 웹 대시보드(#28: 유닛 DAG·blocking·gate 체크·run-judge 증거) → **v2 라이브(#35: 단계 activity·에이전트별 상태·단계 타임라인·코스트 패널·SSE)** — 엔진 무접촉 read-only |
| 합성 하드닝 | 31 | 재합성 YAML 파싱 실패 시 **에러-피드백 재시도** — critic 강화책이 파싱 에러로 유실되던 것 방지 |
| 스킬 주입 | 32 | 스킬 레지스트리+매처가 유닛 작업지시서에 패턴(SKILL.md) 주입(**빌더 전용**, judge 무접촉, =LeanSearch). 시드: frontend-build·simulation-behavior |
| 계측 | 33–34 | 토큰/코스트(orchestration+executor[codex `--json`]+judge/run-judge) → `budget.spent` + event별 `cost`, **단계 전이 + 라이브 activity** 기록 |

**현재 상태**: 의뢰 하나로 *실제 실행되는 React/Canvas 앱*이 나옴(`npm run dev` 작동). gate는 거친 emergent 동선(벽 쌓임·버스트 스폰·큐 미형성)을 *가짜 done으로 안 덮고 정직하게 escalate*. 세 캡스톤이 품질 사다리를 올라감: 못-나아감 → 가짜-빌드-적발 → 진짜-빌드-거친-동선. run의 DAG·단계·에이전트 활동·gate 판정·run-judge 증거·토큰/코스트는 이제 **dashboard v2로 라이브 시각화**(SSE).

---

## 2. 로드맵 (우선순위, LEAP 분석 반영)

### Phase A — 웹 대시보드 ✅ 완료 (WO#28 + v2 WO#35)
read-only state.yaml 뷰: 유닛 DAG·blocking·단계 activity·에이전트별 라이브·검증 패널·단계 타임라인·코스트 패널(orchestration/executor/judge)·SSE 라이브. 엔진 위험 0.

### Phase B — 스킬/지식 검색 주입 ✅ 완료 (WO#32, = LEAP LeanSearch)
스킬 레지스트리+매처가 유닛 작업지시서에 패턴 주입(빌더 전용 → 적대적 분리 유지). deps-요청 채널은 **Phase B.2 보류**.

### Phase C — 분해 critic at replan (다음, = LEAP LLM 리뷰어)
- LEAP 리뷰어가 *매 분해마다* "진전 route인가" 판정. **ablation: 제거 시 8 rollout에도 실패**(무진전 분해를 잡음). haetae는 spec critic이 *spec 1회*만.
- replan이 내놓는 매 work order가 "유닛을 단순화/진전하나, 전체 spec 재진술/헛도나"를 판정 → 약한 분해 reject·재계획.

### Phase D — OR 노드 + 백트래킹 (= LEAP AND-OR DAG)
- LEAP은 OR 노드로 *대안 전략*, 실패 branch 백트래킹. haetae는 반복 실패 시 *같은 분해 재시도*하거나 escalate.
- 유닛/통합 반복 실패 시 *다른 분해/접근*으로 갈아타고 백트래킹. "bounded replan-to-fix"의 진짜 버전. 가장 크고 구조적 → 마지막.

### Phase E — 웹에서 run 실행/제어 (나중, 신중)
대시보드를 read-only 뷰 → *제어 표면*으로(웹에서 run 시작/중단). read-only 속성이 바뀌므로 launch 엔드포인트 가드 필요. 별도 결정.

---

## 3. 백로그 (낮음)

- **per-source 비용 세분** — 현재 event.cost가 mixed(orchestration+executor+judge 합산)로 저장돼 dashboard by_source가 mixed 버킷으로 묶임. event당 source별 sub-cost를 따로 저장하면 정확 분해(데이터 모델 변경 동반). total(budget, 권위) ≥ Σby_source는 정직하게 분리 표기 중.
- deps-요청 채널 (Phase B.2): executor가 필요 dep 선언 → director 선설치.
- 스킬 매칭 word-boundary (현재 substring) · 의미/LLM 매칭(v2).
- DAG memoization / 검증된 컴포넌트 명시적 재사용.
- direct-first then decompose (단순 의뢰 효율).
- pnpm/yarn/uv 설치 경로, 실제 repo용 container/VM sandbox.
- judge/critic 모델 == executor 모델일 때 독립성 경고.
- **행동 품질 경계**: "자연스러우냐"는 비전/Playwright OUT → 부분적으로 사람 눈 영역. run-judge는 "입증 안 됨"까지 자동.

---

## 4. 운영 메모

- **프로토콜**: director(Claude)가 work order를 .md로 작성 → 사람이 CC에 전달 → CC 구현/commit/push → director가 repo clone+pytest로 *직접* 검증.
- **검증**: `PYTHONPATH=src python3 -m pytest -q` (또는 `uv run pytest -q`). 현재 341 passed, 2 skipped(codex opt-in).
- **캡스톤 재실행**: 통짜 한 줄 `uv run python -m haetae.run --order "..." --workdir ... --state-path ... --executor codex --critic-model gpt-5.5 --max-parallel 4 --run-timeout 120 --scaffold` (스킬 기본 on). (paste: 백슬래시 줄바꿈 금지, 실값, state-path는 mkdir 금지.)
- **대시보드**: `uv run python -m haetae.dashboard --state-path <state.yaml> [--spec-path ...] --port 8000` → `localhost:8000` (SSE 라이브). *라이브로 보려면* run과 동시에 띄운다.
- **비용**: tokens는 항상 집계. usd는 `--pricing` 주입 시 계산(구독 codex는 usd 본질적 N/A — tokens가 신호). No Fake Metrics.
- **안전 불변**: `providers/codex.py ALLOWED_SANDBOXES=("read-only","workspace-write")` — executor에 네트워크 안 줌. deps/스캐폴드는 host에서만.
