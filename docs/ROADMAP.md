# haetae-agent 로드맵

> 해태(獬豸) = 시비·선악을 판별하는 신수. 차별점 = **언제 done이 아닌지 아는 governed GATE.**
> autonomous director: 의뢰 하나 → governed spec → `synthesize → replan → [분해 critic] → dispatch(executor) → gate → replan` 루프 → done/escalate/stop.
>
> **최종 갱신: 2026-06-10 · WO#1–62 · 661 tests · main @b608d3a · 핵심 로드맵 A–E 완료 · 통합 벽 3종 방어 · 신뢰성/가시성 · 사용자 ①②③ · 능력 획득 F.1+F.2(인터넷 발견)**

---

## 0. 설계 논제 (검증됨)

- **범용 LLM + agentic harness**로 충분 (특화 모델 불필요). executor는 pluggable(codex, 향후 Claude Code 등).
- 차별점 3축: **governed GATE**(다조건 종료 + 적대적 판정으로 자기합리화 차단) · **spec governance**(mutability gradient, anti-erosion) · **provider-agnostic**.
- 검증 사건: 캡스톤 빌드가 *자기 채점 스크립트*로 `pass:true` 도장 → **독립 run-judge가 "행동 증거 없음"으로 거부**(ac8). 적대적 게이트의 존재 이유 그대로 증명.
- **수렴 검증**: Claude Code *Dynamic Workflows*(오케스트레이션 코드화·병렬·적대 교차검증·테스트=bar)와 Google *LEAP*(분해·verifier-guided·DAG·범용 LLM)가 같은 자리에 도달.

---

## 1. 완료 (WO#1–62, 661 tests)

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
| 문서 | 36·39·50·56·60·63 | docs/ROADMAP.md repo 단일출처 확립·갱신 |
| 웹 제어 (Phase E) | 37 | read-only 대시보드 → 제어 표면(`--allow-run` launch/stop/runs, 서브프로세스 격리, opt-in) |
| 폴리시 | 38 | codex reasoning-effort(minimal..xhigh) 레버 |
| 분해 critic (Phase C) | 40 | 매 replan마다 무진전 분해(goal 재진술·헛돎·실패반복) reject → 재계획. 독립 client·스킬 미주입(적대 분리), soft·bounded |
| OR노드+백트래킹 (Phase D) | 41 | 반복 gate 실패(유닛/통합) → 같은 거 재시도 아닌 **다른 접근**으로 갈아타고 백트래킹. **bar 불변**·bounded·decomp 검증. 소진→escalate(시도 이력 첨부) |
| graceful stop | 43 | SIGINT/KeyboardInterrupt 잡아 worktree 정리·state 저장·클린 exit(traceback 없음) |
| 스마트 폼 | 45 | provider가 launch 옵션 선언(effort 기본 medium·model 자동·codex config pre-fill·critic-model OFF 경고). 엔진-free 리프로 격리 유지 |
| **통합 벽: 치료** | 48 | 머지 충돌 시 *현재 머지된 main 위에서* 통합 적응 재빌드(stale 재생성 아님). bar 불변·bounded |
| **통합 벽: 예방** | 51 | 합성이 통합 유닛(대시보드/진입점)이 *자기가 엮는 유닛에 의존*하게 추론 → 맨 뒤 빌드 → 루트 머지충돌 애초에 회피. bounded nudge, criteria 불변 |
| **통합 벽: 되감기** | 52 | 통합 OR 대안 사이에 checkpoint + main을 깨끗한 all-merged 상태로 git-reset(run workdir·기록 ref만, bounded, best-effort fallback). #41 통합 OR의 진짜 되감기 |
| **능력 획득 (Phase F.1)** | 53 | governed 능력 획득 토대: 큐레이션 발견 + POC + **사람 승인(escalate)** + provenance. auto-adopt 없음·opt-in·executor sandbox 불변 |
| **능력 발견 (Phase F.2)** | 61·62 | F.1 거버넌스 위 *인터넷 발견*(discovery-only): npm 키워드/설명 **의미 검색**(real search API) + pypi 이름 조회 + 멀티 레지스트리(`npm,pypi`). 원격 후보가 description/keywords/relevance와 함께 **사람-게이트 escalation**에 surface. **실행 0(메타데이터 POC, ok=None)·자동 채택 없음(allowlist 불변)·네트워크 격리(`capability_search.py`에만, `capability.py` network-free)·director-side·opt-in** |
| **신뢰성: idle-timeout** | 54 | 모든 codex 호출에 *무진행* timeout(총-시간 cap 아님). `--json` 이벤트 스트리밍 읽기 → "마지막 이벤트 이후 N초 침묵"만 잼 → 멈춘 프로세스그룹 kill. 필수=재시도→escalate / best-effort=degrade. **진행 중인 긴 호출은 안 죽임**, judge stall=degrade(가짜 pass 아님) |
| **가시성: 하트비트** | 55 | 라이브 현재활동 사이드카 `heartbeat.json`(state.yaml 무접촉). #54 스트림에서 in-flight codex 호출(director-side 포함) emit → 대시보드 라이브 배너(종류·유닛·경과·최근이벤트초·액션). idle 차오르면 amber/red(kill 전 멈춤 가시). 순수 텔레메트리·best-effort |
| **주문 뷰 (사용자①)** | 57 | run 히스토리에서 *원 주문 전문*을 상세 헤더(합성 goal과 구분)+사이드바 hover로 표면화. `/api/runs`의 meta order client-join(새 엔드포인트 0). read-only·엔진 무접촉 |
| **이어가기/계보 (사용자②a)** | 58 | `--continue-from`: 부모 최종 main 시딩 + scaffold 스킵 + 부모 spec/완료를 `context`로 증분 합성 + 계보(parent_run_id) 트리. **spec.yaml 사이드카 영속**(토대·대시보드 goal 보강). **judge에 부모 context 무주입(적대 분리)**·anti-erosion·state.yaml 스키마 불변 |
| **disjoint-scope 유도 (사용자③)** | 59 | 병렬 형제 유닛이 서로 다른 파일 소유하게 선제 유도(#51 형제). optional `scope` 필드 + bounded nudge(**bar 불변 가드**). decomp critic 무변경(progress-only 유지). 효과 측정은 캡스톤 의존 |
| 대시보드 v3~앱셸 | 42·44·46·47·49 | dense 라이브 리스트·DW식 요약 헤더·생애주기 phase 섹션+유닛 라운드 드릴다운·정리·**앱 셸(좌측 사이드바 RUNS + 새 run 모달 + 메인 위계)**. 전부 read-only 위성(엔진 무접촉) |

**현재 상태**: 의뢰 하나로 *실행되는 React/Canvas 앱*이 나옴. 적대 게이트가 거친 동선·머지충돌·통합 크래시를 *가짜 done으로 안 덮고 정직하게 escalate*. **통합 벽 3종 방어**(예방#51·치료#48·되감기#52) + **신뢰성**(#54)·**가시성**(#55) + **사용자 ①②③**(#57/#58/#59) + **능력 발견 F.2**(#61/#62 인터넷 발견, discovery-only). medium 캡스톤서 **#48 실전 작동 확인** 후 u5 replan codex hang → #54/#55로 fix. **단, 일괄 변경(#48/#54/#55/#58/#59)은 유닛 레벨 검증만 — *실제 동선*에서의 작동·동선 품질은 여전히 미관측**(통합 검증 도달 = 다음 캡스톤의 일).

---

## 2. 로드맵 (Phase A–E 완료, F 진행 · 사용자 ①②③ 완료)

- **A — 웹 대시보드** ✅ #28 v1 + #35 v2 + v3~앱셸 #42/#44/#46/#47/#49 + 스마트폼 #45 + 하트비트 배너 #55 + 주문 뷰/계보 트리 #57/#58. read-only(엔진 위험 0).
- **B — 스킬/지식 검색 주입** ✅ #32 (= LEAP LeanSearch). 빌더 전용(적대 분리).
- **C — 분해 critic at replan** ✅ #40. 무진전 분해 차단(progress-only, #59에서도 무변경 유지).
- **D — OR 노드 + 백트래킹** ✅ #41 + 통합 되감기 #52. bar 불변·bounded.
- **E — 웹에서 run 실행/제어** ✅ #37. `--allow-run` opt-in.
- **F — governed 능력 획득** ⏳ F.1 토대 #53 + **F.2 인터넷 발견 #61/#62**(discovery-only·opt-in·실행 0·사람 게이트). **F.1b**(라이브 POC)·**F.3**(강격리) 보류 — 채택은 항상 사람 승인.
- **사용자 ①②③** — ① 주문 뷰 ✅#57 · ②a 이어가기/계보 ✅#58(②b 깊은 증분 보류) · ③ disjoint-scope ✅#59(측정 캡스톤 의존).

---

## 3. 백로그 (우선순위 ~순)

- **신뢰성·가시성 확인 캡스톤** — #54/#55/#58/#59 적용 상태로 medium 재실행 → 통합 검증 도달 + **동선 품질 첫 관측** + 일괄 변경 end-to-end 검증. (north star 직전 관문.)
- **②b — 깊은 증분(이어가기)** — 검증된 유닛의 *명시적 재사용*(현재 #58은 context "다시 짓지 마라" 권고만), done 유닛을 새 state에 done으로 시드해 스케줄러 skip, delta DAG. 통합 벽 재사용 로직 동반.
- **disjoint 전용 feedback 채널** — ③(#59) nudge가 #20/#51의 "criteria 강화" preamble 채널을 공유 → bar-불변 가드와 상충해 *nudge가 reject로 no-op되기 쉬움*. "scope/deps만 고쳐라" 전용 채널 분리.
- **능력 획득 F.1b/F.3 + F.2 정제** — F.1b(라이브 POC·격리 안에서 실제 import/install)·F.3(강격리·container/VM). F.2 후속: pypi 의미 검색(서드파티 인덱스 例 libraries.io — 공식 JSON 검색 API 부재) · 관련도 임계 필터/정렬(현재 relevance 노출만). 채택은 사람 승인 유지.
- **stale-status 대조** — 죽은 run이 `running`+가짜 경과로 뜸. state-status vs 실제 프로세스 생존 대조 → "stale" 표시(#55 last-event-age가 부분 완화). + **stopped_interrupted** 전용 상태(현재 stopped_stuck 재사용).
- **CLI run 주문 영속화** — `haetae.run`(CLI)은 meta.json 미생성 → CLI run은 #57 주문 뷰서 폴백 안내만. CLI도 order 사이드카 남기면 완전 커버.
- **per-source 비용 세분** — event.cost가 mixed → by_source 뭉뚱그려짐. source별 sub-cost 분리(데이터 모델 변경). total ≥ Σby_source 정직 표기 중.
- **환경 노이즈 정리** — oh-my-codex 툴링(`.omx/logs/*`·`.omx/state/*`)이 run workdir에 로그 흘려 머지 충돌 유발. worktree gitignore/정리.
- deps-요청 채널(B.2): executor가 필요 dep 선언 → director 선설치. 스킬 매칭 word-boundary/의미·LLM 매칭(현재 substring). DAG memoization·검증 컴포넌트 재사용·direct-first then decompose·pnpm/yarn/container 샌드박스·judge==executor 독립성 경고·infer-but-confirm.
- **north star (사용자 ③ 최종)**: *매우 복잡한 의뢰를 끝까지 완성*(예: 고품질 Mario류). 조각 — 리서치 단계(F.2 발견 완료 → F.1b/F.3로 채택·검증) + 끝까지(루프·게이트 bounded 상한 ↑) + 통합 벽이 현재 천장 + IP(원작 고품질, 복제 X). 여러 WO 여정.
- **행동 품질 경계**: "자연스러우냐"는 비전/Playwright OUT → 부분적으로 사람 눈 영역. run-judge는 "입증 안 됨"까지 자동.

---

## 4. 운영 메모

- **프로토콜**: director(Claude)가 work order를 .md로 작성 → 사람이 CC에 전달 → CC 구현/commit/push → director가 repo clone+pytest로 *직접* 검증.
- **검증**: `PYTHONPATH=src python3 -m pytest -q` (또는 `uv run pytest -q`). 현재 **661 passed, 4 skipped**(codex IT 2 + cap-search IT 2, 전부 opt-in).
- **캡스톤 (웹)**: 대시보드 `--allow-run`으로 띄우고 `+ 새 run` 폼(이어가기는 run의 "이어가기" 버튼 → 부모 컨텍스트 배너). **reasoning-effort=medium 권장**(xhigh는 토큰 ~10배). **critic-model 필수**(비면 분해 critic·OR OFF). max-parallel>1.
- **캡스톤 (CLI)**: `uv run python -m haetae.run --order "..." --workdir ... --state-path ... --executor codex --critic-model gpt-5.5 --max-parallel 4 --run-timeout 120 --reasoning-effort medium --scaffold [--codex-idle-timeout 300] [--continue-from <부모 run-id|state-dir>]`. (paste: 백슬래시 줄바꿈 금지, 실값, state-path는 mkdir 금지.)
- **이어가기**: `--continue-from`이 부모 최종 main 시딩 + scaffold 스킵 + 부모 spec/완료를 증분 context로 주입. spec은 `spec.yaml` 사이드카(state.yaml 옆)로 영속.
- **능력 발견(F.2)**: `--capability-search [npm,pypi]`(opt-in·`--capabilities` 전제·director-side·discovery-only). 원격 후보는 실행 0·escalation으로만, 채택은 allowlist(사람). bare면 기본 `npm,pypi`.
- **신뢰성**: 모든 codex 호출에 idle-timeout(#54, 기본 300s 무음 → kill). 진행 중인 긴 호출은 안 죽음. `--codex-idle-timeout`로 조정.
- **대시보드**: `uv run python -m haetae.dashboard [--allow-run] --runs-dir <dir> --port 8000` → `localhost:8000`(SSE 라이브). **라이브 하트비트 배너**(#55)·**원 주문/계보 트리**(#57/#58). 제어는 `--allow-run` opt-in.
- **비용**: tokens 항상 집계. usd는 `--pricing` 주입 시(구독 codex는 usd 본질 N/A — tokens가 신호). No Fake Metrics.
- **안전 불변**: `providers/codex.py ALLOWED_SANDBOXES=("read-only","workspace-write")` — executor에 네트워크 안 줌. deps/스캐폴드/이어가기 시딩/능력 검색은 host(director)에서만. 대시보드는 read-only 위성. 적대 분리: 부모 context·스킬·disjoint feedback·능력 후보는 *합성기/빌더·사람*만, judge/critic 무주입.
