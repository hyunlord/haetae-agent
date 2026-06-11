# haetae-agent 로드맵

> 해태(獬豸) = 시비·선악을 판별하는 신수. 차별점 = **언제 done이 아닌지 아는 governed GATE.**
> autonomous director: 의뢰 하나 → governed spec → `synthesize → replan → [분해 critic] → dispatch(executor) → gate → replan` 루프 → done/escalate/stop.
>
> **최종 갱신: 2026-06-11 · WO#1–68 · 744 tests · main @734b42f · 핵심 A–E 완료 · autopilot(제로-config) · 비용 거버넌스 · Loop Engineering 수렴 검증(§5)**

---

## 0. 설계 논제 (검증됨)

- **범용 LLM + agentic harness**로 충분 (특화 모델 불필요). executor는 pluggable(codex, 향후 Claude Code 등).
- 차별점 3축: **governed GATE**(다조건 종료 + 적대적 판정으로 자기합리화 차단) · **spec governance**(mutability gradient, anti-erosion) · **provider-agnostic**.
- 검증 사건: 캡스톤 빌드가 *자기 채점 스크립트*로 `pass:true` 도장 → **독립 run-judge가 "행동 증거 없음"으로 거부**(ac8). 적대적 게이트의 존재 이유 그대로 증명.
- **수렴 검증**: Claude Code *Dynamic Workflows* · Google *LEAP* · **Loop Engineering**(2026-06 frontier 용어화 — prompt→context→harness→loop 사다리)이 같은 자리에 도달. Loop Engineering 상세·차용 후보는 **§5**.

---

## 1. 완료 (WO#1–68, 744 tests)

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
| 스킬 주입 (Phase B) | 32 | 로컬 스킬 레지스트리+키워드 매처 → 작업지시서 패턴 주입(**빌더 전용**, judge 무접촉, =LeanSearch) |
| 계측 | 33–34 | 토큰/코스트(codex `--json`) → budget.spent + event별 cost, 단계 전이 + 라이브 activity |
| 문서 | 36·39·50·56·60·63·69 | docs/ROADMAP.md repo 단일출처 확립·갱신 |
| 웹 제어 (Phase E) | 37 | read-only 대시보드 → 제어 표면(`--allow-run` launch/stop/runs, 서브프로세스 격리, opt-in) |
| 폴리시 | 38 | codex reasoning-effort(minimal..xhigh) 레버 |
| 분해 critic (Phase C) | 40 | 매 replan마다 무진전 분해 reject → 재계획. 독립 client·스킬 미주입(적대 분리), soft·bounded |
| OR노드+백트래킹 (Phase D) | 41 | 반복 gate 실패 → **다른 접근**으로 갈아타고 백트래킹. **bar 불변**·bounded·소진→escalate |
| graceful stop | 43 | SIGINT 잡아 worktree 정리·state 저장·클린 exit(traceback 없음) |
| 스마트 폼 | 45 | provider가 launch 옵션 선언(effort 기본 medium·model 자동·config pre-fill·critic OFF 경고). 엔진-free 리프 |
| **통합 벽: 치료** | 48 | 머지 충돌 시 *현재 머지된 main 위에서* 통합 적응 재빌드. bar 불변·bounded |
| **통합 벽: 예방** | 51 | 합성이 통합 유닛이 *자기가 엮는 유닛에 의존*하게 추론 → 맨 뒤 빌드 → 루트 머지충돌 회피. criteria 불변 |
| **통합 벽: 되감기** | 52 | 통합 OR 대안 사이 checkpoint + main을 깨끗한 all-merged로 git-reset(run workdir·기록 ref만). #41 통합 OR 되감기 |
| **능력 획득 (Phase F.1)** | 53 | governed 능력 획득: 큐레이션 발견 + POC + **사람 승인** + provenance. auto-adopt 없음·opt-in·sandbox 불변 |
| **능력 발견 (Phase F.2)** | 61·62 | F.1 위 *인터넷 발견*(discovery-only): npm 의미 검색 + pypi + 멀티 레지스트리. 사람-게이트 escalation. **실행 0·자동 채택 없음·네트워크 격리·opt-in** |
| **신뢰성: idle-timeout** | 54 | 모든 codex 호출에 *무진행* timeout(`--json` 스트림 "N초 침묵"만 잼 → kill). 필수=재시도→escalate / judge stall=degrade. **진행 중 긴 호출은 안 죽임** |
| **가시성: 하트비트** | 55 | 라이브 현재활동 사이드카 `heartbeat.json`(state 무접촉). in-flight codex(director-side 포함) → 대시보드 배너(경과·최근이벤트초·액션). idle amber/red. 순수 텔레메트리 |
| **주문 뷰 (사용자①)** | 57 | run 히스토리서 *원 주문 전문* 표면화(합성 goal과 구분). meta order client-join. read-only |
| **이어가기/계보 (사용자②a)** | 58 | `--continue-from`: 부모 최종 main 시딩 + scaffold 스킵 + 증분 합성 + 계보 트리. **spec.yaml 사이드카**. **judge에 부모 context 무주입(적대 분리)**·anti-erosion·스키마 불변 |
| **disjoint-scope (사용자③)** | 59 | 병렬 형제가 다른 파일 소유하게 선제 유도. optional `scope` + bounded nudge(**bar 불변 가드**). decomp critic 무변경 |
| **반응형 tier 사다리** | 64 | 유닛이 싼 (model,effort) tier 시작 → gate실패/충돌마다 한 tier↑(첫 시도=probe), cap. optional 시작 힌트. **빌더만 라우팅(judge/critic 모델 불변=적대 분리)·bar 불변(anti-erosion)·단일 tier 폴백(back-compat)** |
| **제로-config auto** | 65 | `--auto`: order만으로 운영 knob 자동 해석(tier 사다리·critic·scaffold·parallel/timeout). 명시 플래그 우선. **거버넌스 게이트(능력 채택·네트워크·bar) 자동 미활성(사람 게이트)**. STAGE_AUTO_CONFIG로 노출 |
| **대시보드 관측성** | 66 | 합성 단계 가시성(빨간 에러 제거)·live(heartbeat)↔확정(state) 화해(라이브 배지)·sticky 활동 배너·DAG 엣지 focus+context·SSE 렌더 안정(스크롤 보존). read-only |
| **라이브 트랜스크립트** | 67 | 유닛+director-side 단계 클릭 → 모델 *받은 입력*+*실시간 출력 tail* 인스펙터(=comprehension debt 대응). #54 스트림 위 텔레메트리, bounded(입력4k/출력8k/30콜)·best-effort·read-only. codex.py는 on_output만(판정 무접촉) |
| **비용 거버넌스** | 68 | (A) 크레딧/usage-limit→**graceful stop**(크래시 0·#58 재개) (B) `--max-tokens` 전역 cap (C) 유닛 누적 수렴 ceiling→**사람 escalate**(돈 그만, **바 자동 미완화=anti-erosion**). #41/#43/#54/#58 재사용 |
| 대시보드 v3~앱셸 | 42·44·46·47·49 | dense 라이브 리스트·DW식 요약·phase 섹션+드릴다운·앱 셸(사이드바 RUNS+새 run 모달). 전부 read-only 위성 |

**현재 상태**: 의뢰 하나로 *실행되는 React/Canvas 앱*. 적대 게이트가 거친 동선·머지충돌을 *가짜 done으로 안 덮고 escalate*. 통합 벽 3종 방어(예방#51·치료#48·되감기#52) + 신뢰성/가시성(#54/#55) + 사용자 ①②③(#57/#58/#59) + 능력 F.2(#61/#62) + **autopilot(제로-config #45/#64/#65)** + **관측성/트랜스크립트(#66/#67)** + **비용 거버넌스(#68)**.
**retail-crowd-sim 캡스톤(medium·8유닛)**: #54가 hang 방지(작동), 적대 gate가 u6(헤드리스 sim:trace+run-judge)서 `sim:trace exit 0인데 gate fail`로 가짜 pass 거부(**thesis 라이브**) — 단 **크레딧 소진으로 1/8서 크래시**(품질 아닌 비용). → #68로 graceful stop+cap+유닛 escalate. **3종 실패 모드(hang→비용→크레딧) 전부 가드.** **동선 품질은 여전히 미관측** — 다음 run(#68 cap + #58 재개 + #67로 u6 진단)의 일.

---

## 2. 로드맵 (Phase A–E 완료, F 진행 · 사용자 ①②③ 완료 · autopilot 진행)

- **A — 웹 대시보드** ✅ #28/#35/v3~앱셸 + 스마트폼 #45 + 하트비트 #55 + 주문/계보 #57/#58 + 관측성 #66 + 트랜스크립트 #67. read-only.
- **B — 스킬 주입** ✅ #32 (=LeanSearch). 빌더 전용.
- **C — 분해 critic** ✅ #40. 무진전 분해 차단.
- **D — OR + 백트래킹** ✅ #41 + 통합 되감기 #52. bar 불변.
- **E — 웹 run 제어** ✅ #37. `--allow-run` opt-in.
- **F — governed 능력 획득** ⏳ F.1 #53 + F.2 #61/#62(discovery-only). **F.1b**(라이브 POC)·**F.3**(강격리) 보류 — 채택은 사람 승인.
- **autopilot (제로-config)** ⏳ #45 스마트폼 + #64 tier 사다리 + #65 `--auto` = *order만 던지면 운영 knob 자동, 거버넌스 게이트는 사람*. **멀티-provider 추상화**(local/codex/gemini 비용·성능 자동 픽)는 큰 epic(provider별 sandbox/스트림/비용 어댑터) — 캡스톤 뒤.
- **사용자 ①②③** — ① #57 · ②a #58(②b 보류) · ③ #59.

---

## 3. 백로그 (우선순위 ~순)

- **캡스톤 재실행 (#68 적용 후)** — `--max-tokens` cap + `--unit-attempt-budget` + `--continue-from`(u1 재사용) → **동선 품질 첫 관측** + **#67 트랜스크립트로 u6 '바 과함 vs sim 깨짐' fork 진단** → right-sizing 방향 확정. (north star 직전 관문.)
- **right-size rigor / 유닛 수렴 triage** — 검증 rigor·분해를 태스크 stakes에 맞게(direct-first then decompose + scaled verification bar). 교훈: 캡스톤서 *검증기(u6)가 sim보다 비쌈*(검증 역전). §5 차용 후보와 연결.
- **Loop Engineering 차용 (§5)** — ① proactive anti-fixation(CRDAL: fixation 조기 감지→대안 능동 nudge, u6 재시도 낭비 직격) · ② L1→L2→L3 신뢰 단계(보고→보조→무인, autopilot 시간축) · ③ failure-modes 카탈로그 + 태스크 유형별 비용 태깅.
- **②b — 깊은 증분** — 검증 유닛 *명시적 재사용*(현재 context 권고만), done 시드·delta DAG.
- **disjoint 전용 feedback 채널** — ③ nudge가 criteria-강화 채널 공유 → bar-불변 가드와 상충(reject로 no-op 위험). "scope/deps만" 전용 채널 분리.
- **능력 F.1b/F.3 + F.2 정제** — F.1b(라이브 POC)·F.3(강격리). F.2 후속: pypi 의미 검색(libraries.io)·관련도 임계.
- **stale-status 대조** · **CLI run 주문 영속화** · **per-source 비용 세분** · **환경 노이즈 정리**(.omx) · deps-요청 채널(B.2) · 스킬 word-boundary/의미 매칭 · DAG memoization · pnpm/container 샌드박스 · judge==executor 독립성 경고.
- **north star (사용자 ③ 최종)**: *매우 복잡한 의뢰 끝까지 완성*(예: 고품질 Mario류). 조각 — 리서치(F.2→F.1b/F.3) + 끝까지(bounded 상한↑) + 통합 벽이 천장 + IP(원작 고품질, 복제 X).
- **행동 품질 경계**: "자연스러우냐"는 비전/Playwright OUT(부분적 사람 눈). run-judge는 "입증 안 됨"까지 자동.

---

## 4. 운영 메모

- **프로토콜**: director(Claude)가 work order .md 작성 → 사람이 CC 전달 → CC 구현/push → director가 repo clone+pytest로 *직접* 검증.
- **검증**: `PYTHONPATH=src python3 -m pytest -q`. 현재 **744 passed, 4 skipped**(codex IT 2 + cap-search IT 2, opt-in).
- **캡스톤 (웹)**: 대시보드 `--allow-run` + `+ 새 run` 폼(**auto 모드 기본** — order만). 이어가기는 run "이어가기" 버튼. critic-model 자동(독립성 경고).
- **캡스톤 (CLI, 제로-config)**: `uv run python -m haetae.run --order "..." --workdir ... --state-path ... --executor codex --auto [--max-tokens N] [--unit-attempt-budget K] [--continue-from <부모>]`. `--auto`가 tier 사다리·critic·scaffold·skills·parallel/timeout 자동. (paste: 백슬래시 줄바꿈 금지, 실값, state-path mkdir 금지.)
- **비용 가드(#68)**: `--max-tokens`(전역 cap) · `--unit-attempt-budget`[`--unit-token-budget`](유닛 누적 수렴 ceiling→사람 escalate, **바 자동 미완화**). 크레딧 소진=graceful stop(`stopped_credit`)→`--continue-from` 재개.
- **이어가기**: `--continue-from`이 부모 최종 main 시딩 + scaffold 스킵 + 증분 context. spec=`spec.yaml` 사이드카.
- **능력 발견(F.2)**: `--capability-search [npm,pypi]`(opt-in·`--capabilities` 전제·discovery-only). 채택은 allowlist(사람).
- **신뢰성**: idle-timeout(#54, 기본 300s 무음→kill). `--codex-idle-timeout`로 조정.
- **대시보드**: `uv run python -m haetae.dashboard [--allow-run] --runs-dir <dir> --port 8000` → SSE 라이브. **하트비트 배너**(#55)·**주문/계보**(#57/#58)·**관측성**(#66: 합성 가시성·라이브 배지·sticky·DAG focus)·**트랜스크립트 인스펙터**(#67: 유닛/단계 입출력). 제어 `--allow-run` opt-in.
- **비용**: tokens 항상 집계. usd는 `--pricing` 시(구독 codex usd N/A). No Fake Metrics.
- **안전 불변**: `providers/codex.py ALLOWED_SANDBOXES=("read-only","workspace-write")` — executor 네트워크 0. deps/스캐폴드/시딩/능력 검색은 host(director). 대시보드 read-only 위성. 적대 분리: 부모 context·스킬·disjoint feedback·능력 후보·tier는 *합성기/빌더·사람*만, judge/critic 무주입.

---

## 5. 수렴 검증 + 차용 후보 — Loop Engineering (2026-06)

> "Loop Engineering" = prompt→context→harness→**loop** 추상화 사다리 한 층 위. "에이전트에 프롬프트 치는 *너 자신*을, 그걸 대신하는 시스템으로 대체"(Addy Osmani, 2026-06-07; Peter Steinberger·Boris Cherny/Anthropic). **용어가 생기기 전에 haetae가 이미 만들던 것.**

### 수렴 검증 (LEAP·DW에 이은 외부 증거)
- **5 프리미티브 + 메모리 전부 보유**: Automations↔루프 드라이버 · Worktrees↔#21 · Skills↔#32 · Plugins/MCP↔F.1/F.2(단 governed) · Sub-agents(maker≠checker)↔**model vs critic-model** · State↔state.yaml/#58.
- **프런티어가 "루프의 미해결 난제"로 꼽은 게 정확히 haetae 코어**: ① 검증("done은 *주장*이지 *증명* 아님")=적대 GATE+run-judge(u6 `exit 0인데 gate fail`=라이브 증거) · ② 토큰 비용=#68 · ③ comprehension debt=#67. 셋 다 이미 건드림. 검증 축에선 출시 제품(`/goal` 단일 판정)보다 앞섬.
- **학술 엄밀 검증 (arxiv 2603.24768 CRDAL)**: 에이전트 *자기* 메타인지 모니터(Self-Regulation)는 유의미 개선 없음, *별도* 에이전트 Co-Regulation은 개선(계산비용 큰 증가 없이). **"자기 검증 안 됨, 분리된 검증자 됨"이 데이터로 입증** = model/critic 적대 분리의 정당화.
- **Ralph Wiggum Loop 계보**: "비결정적 세계에서 결정적으로 나쁘다"(LLM 오류 전제·반복 brute-force). 약점="정지 기준 부실하면 의도치 않게 충족(게이밍)" → haetae 적대 gate가 정조준. RWL(executive)+Open Spec(legislative) 상보 = haetae는 둘+gate 통합.

### 차용 후보 (우선순위)
1. **proactive anti-fixation (CRDAL)** — 현 OR(#41)은 *반응형*(재시도 소진 후). CRDAL는 fixation 조기 감지→대안 탐색 *능동* nudge. u6가 같은 깨진 접근 4회 재시도한 낭비 직격. #68 C·right-size rigor의 능동 버전, critic-model에 얹힘. **(1순위)**
2. **L1→L2→L3 신뢰 단계 (Greyling)** — 보고 전용→보조 수정→무인. 태스크 유형별 신뢰 tier(새 유형 제안만, 신뢰 쌓이면 무인) = autopilot+거버넌스의 시간축. **(1순위)**
3. **failure-modes 카탈로그 + 태스크 유형별 비용 태깅** — haetae 자체 발견(hang→비용→크레딧) 문서화 + 주문 유형별 사전 비용 등급 → right-size triage 입력. **(2순위)**
4. **(보류/분기) Automations(스케줄 발견)** — haetae는 order 1개 완주 루프지 *지속 발견* 아님. 다른 제품 모양 — 채울지 별도 결정.

> 안티패턴 확인: "exit hook으로 세션 hijack해 무한 강제 지속"은 RWL 비판이 명시한 나쁜 패턴(= 로컬 CC 매직키워드 stop-hook 류). haetae는 graceful stop·bounded·사람 escalate로 회피.
> 참고: addyosmani.com/blog/loop-engineering · github.com/cobusgreyling/loop-engineering · arxiv 2603.24768(CRDAL) · arxiv 2509.06216(Agentic Loop Engineering).
