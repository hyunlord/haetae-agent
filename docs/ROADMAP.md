# haetae-agent 로드맵

> 해태(獬豸) = 시비·선악을 판별하는 신수. 차별점 = **언제 done이 아닌지 아는 governed GATE.**
> autonomous director: 의뢰 하나 → governed spec → `synthesize → replan → dispatch(executor) → gate → replan` 루프 → done/escalate/stop.
>
> **최종 갱신: 2026-06-08 · WO#1–38 완료 · 381 tests · main @d5b17c9**

---

## 0. 설계 논제 (검증됨)

- **범용 LLM + agentic harness**로 충분 (특화 모델 불필요). executor는 pluggable(codex, 향후 Claude Code 등).
- 차별점 3축: **governed GATE**(다조건 종료 + 적대적 판정으로 자기합리화 차단) · **spec governance**(mutability gradient, anti-erosion) · **provider-agnostic**.
- 검증 사건: 빌드가 *자기 채점 스크립트(카운트 임계값)*로 `pass:true`를 찍었으나 **독립 run-judge가 "행동 증거 없음"으로 거부**(ac8). 기계 체크(exit 0)론 통과·run-judge론 실패 — 적대적 게이트의 존재 이유 증명.
- **수렴 검증**: Claude Code *Dynamic Workflows*(오케스트레이션 코드화·병렬 서브에이전트·적대적 교차검증·테스트=bar)와 Google *LEAP*(분해·verifier-guided·DAG·범용 LLM)가 같은 자리에. (Claude Code 모니터링은 터미널 진행 수준 — haetae는 영속 구조화 state로 더 풍부한 시각화 + 웹 제어.)

---

## 1. 완료 (WO#1–38, 381 tests)

| 영역 | WO | 내용 |
|---|---|---|
| 코어 | 1–11 | 데이터/모델, intake(synthesize), CodexExecutor, replan, 루프 드라이버, CheckRunner gate, HumanRelay |
| 신뢰성 | 12–14 | 합성기 하드닝, 루프 회복(재시도/escalate), autonomous executor(offline sandbox) |
| 적대적 게이트 | 15–17 | 적대 LLMJudge + CompositeGate, governed spec-change(mutability gradient), consolidation |
| 자기개선 | 18–20 | richer progress + non-fatal save, **adversarial spec critic**(soft→1회 재합성), critic best-effort |
| 병렬 | 21 | DAG scheduler + worktree 격리 + 보장 cleanup + 머지충돌→직렬화 |
| run-judge | 22 | `run` 체크 + run_harness + 동적 run-judge(행동 판정) + judge 부재 시 degrade |
| 네트워크 | 23 | **호스트 deps 설치**(sandbox 불변, 해시캐시, non-fatal) |
| 캡스톤 준비 | 24–25 | run-기준+trace 진입점 합성 유도, 헤드룸, plan-state 현실 반영, clean-install 실패=gate 신호 |
| per-unit | 26 | **유닛별 acceptance criteria**(per-unit gate는 자기 기준만, 통합 gate는 전체) |
| 선제 스캐폴드 | 27 | **director가 executor 전에 진짜 스택(React/Vite/TS) 생성+설치** → 스택 치환 차단 |
| 대시보드 | 28, 35 | read-only 웹 대시보드(#28) → **v2 라이브(#35: 단계 activity·에이전트 상태·타임라인·코스트 패널·SSE)** |
| 합성 하드닝 | 31 | 재합성 YAML 파싱 실패 시 **에러-피드백 재시도** — critic 강화책 유실 방지 |
| 스킬 주입 | 32 | 스킬 레지스트리+매처가 유닛 작업지시서에 패턴(SKILL.md) 주입(**빌더 전용**, =LeanSearch). 시드: frontend-build·simulation-behavior |
| 계측 | 33–34 | 토큰/코스트(orchestration+executor[codex `--json`]+judge/run-judge) → `budget.spent`+event `cost`, 단계 전이+라이브 activity |
| 웹 제어 | 37 | **웹에서 run 실행/정지 + runs 목록**(Phase E v1). 서브프로세스 격리(엔진 무import), 제어 **opt-in `--allow-run`**(없으면 read-only), localhost·no-shell·옵션 화이트리스트·경로 서버생성 |
| 폴리시 | 38 | SSE 끊김 traceback 억제(터미널 클린) + **codex reasoning-effort 옵션**(minimal..xhigh, `-c model_reasoning_effort`; 폼+CLI; 미설정=codex 기본) |

**현재 상태**: 의뢰 하나로 *실제 실행되는 React/Canvas 앱*. gate는 거친 emergent 동선을 *가짜 done으로 안 덮고 정직하게 escalate*. **터미널 0번 — 웹 폼에서 옵션 채우고 실행 → governed 루프가 단계·에이전트별로 도는 걸 라이브 관전(SSE) + 토큰/코스트 + run-judge 증거**까지 한 화면에. 품질 사다리: 못-나아감 → 가짜-빌드-적발 → 진짜-빌드-거친-동선(현재 frontier).

---

## 2. 로드맵 (우선순위, LEAP 분석 반영)

### Phase A — 웹 대시보드 ✅ 완료 (WO#28 + v2 #35)
### Phase B — 스킬/지식 검색 주입 ✅ 완료 (WO#32, = LEAP LeanSearch). deps-요청 채널은 Phase B.2 보류.
### Phase E — 웹에서 run 실행/제어 ✅ 완료 (WO#37 v1: launch/stop/runs, 제어 opt-in). *(로드맵 순서상 뒤지만 사용성 우선으로 먼저 구현.)*

### Phase C — 분해 critic at replan (다음, = LEAP LLM 리뷰어)
- LEAP 리뷰어가 *매 분해마다* "진전 route인가" 판정. **ablation: 제거 시 8 rollout에도 실패**(형식상 OK인데 goal 재진술하는 무진전 분해를 잡음). haetae는 spec critic이 *spec 1회*만.
- replan이 내놓는 매 work order가 "유닛을 단순화/진전하나, 전체 spec 재진술/헛도나"를 판정 → 약한 분해 reject·재replan(bounded, soft). 캡스톤서 본 u1 과부하·헛돎 직격.

### Phase D — OR 노드 + 백트래킹 (= LEAP AND-OR DAG)
- 유닛/통합 반복 실패 시 *다른 분해/접근*으로 갈아타고 백트래킹("bounded replan-to-fix"의 진짜 버전). 가장 크고 구조적 → 마지막.

---

## 3. 백로그 (낮음)

- **터스 주문 → infer-but-confirm**: 간단 주문을 합성기가 풍부한 spec으로 추론하되, 그 spec을 사람이 *확인/수정* 후 진행(silent 추측 금지). 사용성↑, 후순위.
- **per-source 비용 세분**: event.cost가 mixed로 저장돼 by_source가 mixed 버킷으로 묶임. event당 source별 sub-cost 저장하면 정확 분해(데이터 모델 변경). total(권위) ≥ Σby_source는 정직 분리 표기 중.
- 브레인 합성기/judge에도 reasoning-effort 레버(현재 executor만).
- deps-요청 채널 (Phase B.2). 스킬 매칭 word-boundary/의미 매칭(현재 substring).
- DAG memoization. direct-first then decompose. pnpm/yarn/uv + container/VM sandbox.
- judge/critic 모델 == executor 모델일 때 독립성 경고.
- **행동 품질 경계**: "자연스러우냐"는 비전/Playwright OUT → 부분적으로 사람 눈 영역.

---

## 4. 운영 메모

- **프로토콜**: director(Claude)가 work order .md 작성 → 사람이 CC 전달 → CC 구현/commit/push → director가 repo clone+pytest로 *직접* 검증.
- **검증**: `PYTHONPATH=src python3 -m pytest -q`. 현재 381 passed, 2 skipped(codex opt-in).
- **캡스톤(CLI)**: `uv run python -m haetae.run --order "..." --workdir ... --state-path ... --executor codex --critic-model gpt-5.5 --max-parallel 4 --run-timeout 120 --scaffold [--reasoning-effort xhigh]`.
- **대시보드/웹 제어**: `uv run python -m haetae.dashboard --allow-run --runs-dir <dir> --port 8000` → 웹 폼으로 실행/정지/관전(SSE). `--allow-run` 없으면 read-only(launch 403).
- **비용**: tokens 항상. usd는 `--pricing` 주입 시(구독 codex는 usd N/A — tokens가 신호). **reasoning-effort xhigh = 토큰 3-5배**(품질↔비용 레버). No Fake Metrics.
- **안전 불변**: `providers/codex.py ALLOWED_SANDBOXES=("read-only","workspace-write")` — executor에 네트워크 안 줌. 웹 제어는 opt-in·localhost·서브프로세스 격리.
