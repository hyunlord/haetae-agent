# haetae-agent 로드맵

> 해태(獬豸) = 시비·선악을 판별하는 신수. 차별점 = **언제 done이 아닌지 아는 governed GATE.**
> autonomous director: 의뢰 하나 → governed spec → `synthesize → replan → [분해 critic] → dispatch(executor) → gate → replan` 루프 → done/escalate/stop.
>
> **최종 갱신: 2026-06-08 · WO#1–49 · 477 tests · main @0042d17 · 핵심 로드맵 A·B·C·D·E 전부 완료**

---

## 0. 설계 논제 (검증됨)

- **범용 LLM + agentic harness**로 충분 (특화 모델 불필요). executor는 pluggable(codex, 향후 Claude Code 등).
- 차별점 3축: **governed GATE**(다조건 종료 + 적대적 판정으로 자기합리화 차단) · **spec governance**(mutability gradient, anti-erosion) · **provider-agnostic**.
- 검증 사건: 캡스톤 빌드가 *자기 채점 스크립트*로 `pass:true` 도장 → **독립 run-judge가 "행동 증거 없음"으로 거부**(ac8). 적대적 게이트의 존재 이유 그대로 증명.
- **수렴 검증**: Claude Code *Dynamic Workflows*(오케스트레이션 코드화·병렬·적대 교차검증·테스트=bar)와 Google *LEAP*(분해·verifier-guided·DAG·범용 LLM)가 같은 자리에 도달.

---

## 1. 완료 (WO#1–49, 477 tests)

| 영역 | WO | 내용 |
|---|---|---|
| 코어 | 1–11 | 데이터/모델, intake(synthesize), CodexExecutor, replan, 루프 드라이버, CheckRunner gate, HumanRelay |
| 신뢰성 | 12–14 | 합성기 하드닝, 루프 회복(재시도/escalate), autonomous executor(offline sandbox) |
| 적대적 게이트 | 15–17 | 적대 LLMJudge + CompositeGate, governed spec-change(mutability gradient), consolidation |
| 자기개선 | 18–20 | richer progress + non-fatal save, adversarial spec critic(soft→1회 재합성), best-effort |
| 병렬 | 21 | DAG scheduler + worktree 격리 + 보장 cleanup + 머지충돌→직렬화 |
| run-judge | 22 | `run` 체크 + run_harness + 동적 run-judge(행동 판정) + judge 부재 시 degrade |
| 네트워크 | 23 | 호스트 deps 설치(sandbox 불변, 해시캐시, non-fatal) |
| 캡스톤 준비 | 24–26 | run-기준 합성, 헤드룸, clean-install=gate 신호, per-unit acceptance criteria |
| 선제 스캐폴드 | 27 | director가 executor 전에 진짜 스택(React/Vite/TS) 생성+설치 → 스택 치환 차단 |
| 합성 하드닝 | 31 | 재합성 YAML 파싱 실패 시 에러-피드백 재시도(critic 강화 유실 방지) |
| 스킬 주입 (Phase B) | 32 | 로컬 스킬 레지스트리+키워드 매처 → 작업지시서 패턴 주입(**빌더 전용**, judge 무접촉, =LeanSearch). 시드: frontend-build·simulation-behavior |
| 계측 | 33–34 | 토큰/코스트(codex `--json`) → budget.spent + event별 cost, 단계 전이 + 라이브 activity |
| 문서 | 36·39 | docs/ROADMAP.md repo 단일출처 확립 |
| 웹 제어 (Phase E) | 37 | read-only 대시보드 → 제어 표면(`--allow-run` launch/stop/runs, 서브프로세스 격리, opt-in) |
| 폴리시 | 38 | codex reasoning-effort(minimal..xhigh) 레버 |
| 분해 critic (Phase C) | 40 | 매 replan마다 무진전 분해(goal 재진술·헛돎·실패반복) reject → 재계획. 독립 client·스킬 미주입(적대 분리), soft·bounded |
| OR노드+백트래킹 (Phase D) | 41 | 반복 gate 실패(유닛/통합) → 같은 거 재시도 아닌 **다른 접근**으로 갈아타고 백트래킹. **bar 불변**·bounded·decomp 검증. 소진→escalate(시도 이력 첨부) |
| graceful stop | 43 | SIGINT/KeyboardInterrupt 잡아 worktree 정리·state 저장·클린 exit(traceback 없음) |
| 스마트 폼 | 45 | provider가 launch 옵션 선언(effort 기본 medium·model 자동·codex config pre-fill·critic-model OFF 경고). 엔진-free 리프로 격리 유지 |
| 통합 적응 | 48 | 머지 충돌 시 *현재 머지된 main 위에서* 통합 적응 재빌드(stale 재생성 아님). bar 불변·bounded |
| 대시보드 v3~앱셸 | 42·44·46·47·49 | dense 라이브 리스트·DW식 요약 헤더·생애주기 phase 섹션+유닛 라운드 드릴다운·정리·**앱 셸(좌측 사이드바 RUNS + 새 run 모달 + 메인 위계)**. 전부 read-only 위성(엔진 무접촉) |

**현재 상태**: 의뢰 하나로 *실행되는 React/Canvas 앱*이 나옴. 적대 게이트가 거친 동선·머지충돌·통합 크래시를 *가짜 done으로 안 덮고 정직하게 escalate*. graceful stop·통합 적응 재빌드·앱 셸 대시보드까지. **남은 프런티어: 통합(integration)이 일관된 벽** — 유닛은 매번 개별 gate 통과하나, *엮는 데서* 막힘(머지충돌[#48이 적응]·통합 크래시·거친 동선). 동선 품질은 아직 *미관측*(통합 벽 너머라 못 봄).

---

## 2. 로드맵 (Phase A–E, LEAP 분석 반영) — **전부 완료**

- **A — 웹 대시보드** ✅ #28 v1 + #35 v2 라이브 + v3~앱셸 #42/#44/#46/#47/#49 + 스마트폼 #45. read-only state.yaml 뷰(엔진 위험 0): 좌측 사이드바 RUNS·요약 헤더·생애주기 스텝퍼·유닛 드릴다운·코스트·작업로그 tail·SSE 라이브.
- **B — 스킬/지식 검색 주입** ✅ #32 (= LEAP LeanSearch). 로컬 레지스트리·키워드 매칭·빌더 전용(적대 분리 유지).
- **C — 분해 critic at replan** ✅ #40 (= LEAP LLM 리뷰어). ablation상 제거 시 8 rollout에도 실패하던 무진전 분해 차단.
- **D — OR 노드 + 백트래킹** ✅ #41 (= LEAP AND-OR DAG). 반복 실패→대안 전략·백트래킹, bar 불변·bounded.
- **E — 웹에서 run 실행/제어** ✅ #37. read-only 뷰 → 제어 표면(`--allow-run`, opt-in 가드).

---

## 3. 백로그 (우선순위 ~순)

- **통합 유닛 직렬화** — 통합 유닛(대시보드/진입점)이 *자기가 엮는 유닛들에 의존*하게 해서 맨 뒤 직렬로 → 애초에 머지 충돌 안 나게. #48(충돌 적응)의 근본 보완.
- **통합 main-reset 백트래킹** — #41 통합 OR이 main을 git-reset 안 하고 현재 main 위 재dispatch. 진짜 되감기는 worktree.py reset 프리미티브 필요.
- **툴/MCP/스킬 검색 → POC 샌드박스 → 승인 게이트(사람) 채택** (B.2 확장) — 발견·POC 자동, *채택은 사람 승인*(안전≠기능). 네트워크 획득·코드실행 티어는 HumanRelay로 승인. provenance 추적.
- **per-source 비용 세분** — event.cost가 mixed(orchestration+executor+judge) → by_source 뭉뚱그려짐. source별 sub-cost 분리(데이터 모델 변경). total ≥ Σby_source 정직 표기 중.
- **stale-status 대조** — 죽은 run이 `running`+가짜 경과로 뜸. state-status vs 실제 프로세스 생존 대조 → "stale" 표시. + **stopped_interrupted** 전용 상태(현재 stopped_stuck 재사용, "막힘"↔"사용자 중단" 미구분).
- deps-요청 채널(B.2): executor가 필요 dep 선언 → director 선설치.
- 스킬 매칭 word-boundary/의미·LLM 매칭(현재 substring).
- DAG memoization / 검증 컴포넌트 재사용 · direct-first then decompose · pnpm/yarn/container 샌드박스 · judge==executor 독립성 경고 · infer-but-confirm(모호 주문 보강하되 확인).
- **행동 품질 경계**: "자연스러우냐"는 비전/Playwright OUT → 부분적으로 사람 눈 영역. run-judge는 "입증 안 됨"까지 자동.

---

## 4. 운영 메모

- **프로토콜**: director(Claude)가 work order를 .md로 작성 → 사람이 CC에 전달 → CC 구현/commit/push → director가 repo clone+pytest로 *직접* 검증.
- **검증**: `PYTHONPATH=src python3 -m pytest -q` (또는 `uv run pytest -q`). 현재 **477 passed, 2 skipped**(codex opt-in).
- **캡스톤 (웹)**: 대시보드 `--allow-run`으로 띄우고 `+ 새 run` 폼. **reasoning-effort=medium 권장**(xhigh는 토큰 ~10배·반복엔 과함, 핀포인트만). **critic-model 필수**(비면 분해 critic·OR노드 OFF). max-parallel>1(OR은 병렬 경로).
- **캡스톤 (CLI)**: `uv run python -m haetae.run --order "..." --workdir ... --state-path ... --executor codex --critic-model gpt-5.5 --max-parallel 4 --run-timeout 120 --reasoning-effort medium --scaffold`. (paste: 백슬래시 줄바꿈 금지, 실값, state-path는 mkdir 금지.)
- **대시보드**: `uv run python -m haetae.dashboard [--allow-run] --runs-dir <dir> --port 8000` → `localhost:8000`(SSE 라이브). 제어는 `--allow-run` opt-in.
- **비용**: tokens 항상 집계. usd는 `--pricing` 주입 시(구독 codex는 usd 본질 N/A — tokens가 신호). No Fake Metrics.
- **안전 불변**: `providers/codex.py ALLOWED_SANDBOXES=("read-only","workspace-write")` — executor에 네트워크 안 줌. deps/스캐폴드는 host에서만. 대시보드는 read-only 위성(엔진 무접촉, 무import 가드).
