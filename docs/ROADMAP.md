# haetae-agent 로드맵

> 해태(獬豸) = 시비·선악을 판별하는 신수. 차별점 = **언제 done이 아닌지 아는 governed GATE.**
> autonomous director: 의뢰 하나 → governed spec → `synthesize → replan → [분해 critic] → dispatch(executor) → gate → replan` 루프 → done/escalate/stop.
>
> **최종 갱신: 2026-06-21 · WO#1–153 · 1138 tests · main @8d12689 · 🧪 로컬-모델 executor arc(#133–153, §8) — 약한 로컬 Qwen3.6 빌더 + 풀 파이프라인이 실제 JS 게임(snake)의 단일-책임 검증 유닛 *5/6 수렴* = thesis 실질 실증(오케스트레이션 > 모델 강도 · 적대 gate 전 arc 정직 · 검증역전 0); 남은 floor = integration(wire + 풀-행동 트레이스) · 직전: 🎮 Mario 첫 완주(#129) · 🎯 crowd-sim 캘리브레이션 종결(#124) · OMC 차용 4종 전부 ✅(§7)**

---

## 0. 설계 논제 (검증됨)

- **범용 LLM + agentic harness**로 충분 (특화 모델 불필요). executor는 pluggable(codex, 향후 Claude Code 등).
- 차별점 3축: **governed GATE**(다조건 종료 + 적대적 판정으로 자기합리화 차단) · **spec governance**(mutability gradient, anti-erosion) · **provider-agnostic**.
- 검증 사건: 캡스톤 빌드가 *자기 채점 스크립트*로 `pass:true` 도장 → **독립 run-judge가 "행동 증거 없음"으로 거부**(ac8). 적대적 게이트의 존재 이유 그대로 증명.
- **수렴 검증**: Claude Code *Dynamic Workflows* · Google *LEAP* · **Loop Engineering**(2026-06 frontier 용어화 — prompt→context→harness→loop 사다리)이 같은 자리에 도달. Loop Engineering 상세·차용 후보는 **§5**.
- **시나리오 커버리지 = gate 엄밀성의 상한** *(WO#112 교훈)*: 적대 gate는 *시나리오가 어려운 조건을 구동할 때만* 그 실패를 잡는다. #95 crowd-sim이 통과한 건 gate가 틀려서가 아니라 *run 시나리오가 저밀도*였기 때문(overlap onset ~15체 아래의 active=12). → sim 하니스 시나리오는 현실 밀도/스트레스를 구동해야 한다(#98 시나리오 계약의 자연 확장). 북극성(복잡 태스크)엔 결정적 — **검증 시나리오가 약하면 적대 gate도 가짜 done을 못 거른다**(gate는 결백, 커버리지가 병목). **보강(#124 end-to-end 입증)**: 게다가 *메트릭 일관성*(#120 단위)·*계약 게임불가*(#120 sustained)·*의미충돌 머지 정합*(#123)이 함께여야 적대 gate가 하드 태스크서 신뢰할 verdict를 낸다 — #124가 tight 근접 collision-free done으로 전부 한 런에서 입증(inversion 0).

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

---

## 6. 하니스 검증 사슬 + 길 B 완주 검증 (WO#77–100)

### 배경: 검증 역전 (verification inversion)
crowd-sim 캡스톤이 반복적으로 통합 run-judge에 *닿지 못함* — 매번 **검증 하니스 유닛**이 비용을 독식(crowd-sim u7 13M, kanban u6 14.9M, snake u4 21.6M = 전체의 66~89%). 게이트는 건전했으나(가짜 done 거부), 하니스 자체가 (a) 틀린 증거를 내거나 (b) 게이트 환경서 못 돌거나 (c) 노이즈로 미파싱이라, *검증기가 검증 안 됨* = hollow verification.

### 4레버로 하니스를 검증기답게 (전부 실루프 검증됨)
- **#78 evidence-contract**: acceptance 기준에서 요구 증거 필드를 추출해 하니스 작업지시서에 주입 + 게이트서 결정적 필드존재 체크. (단 초기엔 산문 criteria에 brittle — #82-A가 해소.)
- **#82 하니스 자기검증**: (A) 합성기가 run 기준에 구조화 `evidence_fields` 명시 → 계약 항상 부착(산문 brittle 해소). (B) 하니스 *per-unit 게이트*가 sim:trace를 clean-install서 *실제 실행* → 깨진/틀린 하니스를 통합 기다릴 것 없이 조기 fail. **결정적 체크(필드존재·exit)만 — 값/행동 판정은 적대 run-judge(LLM) 그대로 = 분리 보존.**
- **#84 하니스 종류 유도**: 하니스를 **게이트 오프라인 환경서 도는 가벼운 node 트레이스**(엔진 import + 순수 JS/JSDOM + JSON emit)로 유도, 실브라우저 E2E(playwright/chromium) 회피. 검증 역전의 근본 — 빌더가 게이트서 못 도는 브라우저 하니스 고집하던 것. 비용 −53~56%, governed escalate(캡 태우기 탈출).
- **#86 stdout JSON 위생**: 하니스가 stdout에 *단일 유효 JSON만*(로그는 stderr, npm 배너 억제). node 하니스가 *돌지만* stdout 노이즈로 미파싱되던 마지막 병목.
- 원칙: **검증 하니스는 게이트의 오프라인 clean-install 환경서 runnable이어야 한다** + 모든 하니스 유도는 *빌더-측*(apply_builder 채널) — 게이트·적대 run-judge·바 불변.

### #88 루프 위생
(A) test/빌드 cmd를 스캐폴드 러너에 맞게 유도(vitest에 Jest `--runInBand` 금지 — #86 stdout 위생의 test판, 빌더-측). (B) codex.py `TemporaryDirectory(ignore_cleanup_errors=True)` 1줄 — oh-my-codex의 .omx/state 비동기 쓰기 ↔ rmtree 경합 합성 크래시 차단(ALLOWED_SANDBOXES·실행 로직 불변).

### 길 B — 완주 검증 캡스톤 (전 사슬 최초·복수 완주)
crowd-sim은 빌더 역량 벽(군중 충돌회피·그리드락)이라 *완주 테스트론* 부적합 → codex 완주 가능 중간 과제로 **order→합성→빌드→통합 run-judge→done 전 사슬을 처음으로 검증**.
- **md-editor ✅ done** (14.7M) — haetae 최초 전 사슬 완주. u6 하니스 생애에서 #82-A 계약부착→#82-B self-check 누락 적발→#79 anti-fixation nudge→빌더 피벗→pass가 *연쇄 합성*으로 작동.
- **snake ✅ done** (7.6M) — 2번째 완주. 통합 run-judge가 *실제 행동을 유효 증거로* 판정(충돌 게임오버·점수·속도증가·high_score 영속 + 34필드). logic-render 분리된 실 Canvas 게임 산출.
- **kanban ⚠️ budget** — 7유닛+하니스 > 20M캡, 통합 前 소진. fix 실패 아닌 plan-size/budget.
- **결론**: haetae 전 사슬 건전성 = *복수 과제서 입증*. 누적 하니스-검증 레버가 fresh서 끝까지 작동.

### #91 순수 재개 + #92 실루프 검증
continue-from 순수 재개(같은 order)가 부모 plan/criteria를 *보존*(spec.yaml 로드, 재합성 skip) → #71 reuse 매칭. #92서 실루프 검증: 재합성 0·done 유닛 재빌드 0·미완만 재빌드·**통합 11.5M 도달(#89의 21.2M 대비 반값)**·anti-erosion by construction(부모 criteria byte-보존). 비용 효율 재개 입증.
- kanban: 통합 도달했으나 run-judge가 하니스 *시나리오* 결함(ac3 DnD 미이동·ac5 persistence 삭제후reload)을 정확히 fail — 5/7+빌드+11테스트 통과. **gate가 하니스 자기 시나리오 버그까지 잡는 가차없는 정확성** = "언제 done이 아닌지 안다" 명제 최강 형태.
- 길 B 결산: **2/3 완주(md+snake) + 효율 재개 검증 + gate 가차없음**. 명제 입증 완료. kanban 3/3은 완성도(폴리시)로 분류.

### crowd-sim 북극성 첫 완주 (WO#94 스킬 + #95 RUN)
crowd-sim이 *지금껏 한 번도 못 닿던* 통합 run-judge에 **처음 도달 + 10/10 통과 → done**(26.7M/30M, fresh --auto). 이번 세션 전 아크의 페이오프:
- **#94 충돌회피 스킬 결정적 입증**: 빌더가 속도기반 연속회피(swept-circle, velocity 채택·naive 칸-점유 블로킹 0) + 연속 스폰 + 큐 형성 채택. **그리드락 완전 반전**: overlap 34만→0 · stuck 90~97%→0 · 이동 8%→**28/28 전원 enter→queue→checkout→exit 완주**(통합 ac7 [run] 증거).
- **검증역전 탈출 실증**: build+retry 25.9M(97%)가 실 sim 빌드. 하니스(u7/u8 헤드리스 트레이스)는 가벼운 node로 흡수 — #81/#87의 "하니스 66~89% 독식"이 사라짐. **#78~#86 하니스 사슬 + #91 효율 재개가 예산을 실 빌드로 해방한 게 실증.**
- **disjoint 분해(#72)**: 8유닛(layout·agent·navigation·checkout/queue·collision-avoidance·canvas-render·헤드리스트레이스·렌더트레이스), 로직-렌더 분리가 분해에 반영. 머지충돌 2건 #48로 해소, 통합 OR 불필요(첫 시도 통과). #82-B·#78 작동.
- **명제 확인**: gate가 거친 동선을 거르는 정확성 보존하면서, *빌더가 그 바를 처음으로 정직하게 넘김*(0겹침·0교착·렌더 트레이스 증거 위 적대 판정 pass).
- **한계(과대해석 금지)**: 빌더 회피는 *속도-절단*이지 완전 RVO/ORCA 상호 사이드스텝 아님. spawn_rate=4·28 에이전트 *중간 부하*서 통과 — 더 무거운 부하/좁은 통로 stress는 미검증("첫 완주 + 한계 미검증"). stress run으로 한계 probe가 후속.

### crowd-sim stress 발견 — 속도-절단의 한계 (WO#112 진단)
#112 부하 sweep(엔진 무수정·트레이스, spawn/active/radius — 통로폭은 layout 고정이라 미노출, spawn_rate는 클램프라 active·radius가 실제 혼잡 레버)으로 #95를 캘리브레이션:
- **#94 교착 제거는 견고**: deadlock 0·stuck 0·완주율 98~100%가 240체·active 96·radius 0.30까지 유지. #81/#87의 34만 교착(=#94 타깃 실패모드) 재발 0.
- **그러나 속도-절단은 collision-free 아님**: 동시 ~15체 초과 시 overlap 발생·급증(active 96서 에이전트당 매틱 ~2겹침, radius↑ 7.5× 증폭). 비상호적 속도장애물의 약점 — *교착 실패모드를 overlap 실패모드로 맞바꾼 liveness hack*(멈추는 대신 서로 통과).
- **#95 재캘리브레이션**: #95는 active=12 = overlap onset(~15) 바로 아래 → **overlap=0은 저밀도 아티팩트, "저밀도 첫 완주"**(견고한 완주 아님). #95 caveat 데이터 확증.
- **다음 후보**: 완전 RVO/ORCA(상호) 스킬(#94 v2) / flow-field 의무화 / 더 강한 분해.

### crowd-sim 견고 완주 재실행 — gate 정직성 실증 (WO#114·#115 스킬 + #116 RUN)
#114(밀도 시나리오 계약) + #115(#94 v2 stop-not-pass)를 얹어 fresh 재실행 → **중밀도 견고 완주 + 적대 gate의 결정적 정직성 실증**(done, 19.38M, 5유닛).
- **★gate가 가짜 done 차단★**: 1차 통합서 빌더 하니스가 overlap=0 보고했으나 *spawned=1*(저밀도 아티팩트=#95/#112). 적대 run-judge 거부("단일 고객으론 혼잡·비겹침 입증 불가") → #97 OR 리셋(연루 3·seeded 2 보존·바 불변) → 재빌드 → 2차 DONE 7/7(spawned 24·completed 24·100%·overlap 0·deadlock 0·stuck 0). **"언제 done이 아닌지 아는 governed GATE"가 밀도-stress 하 실루프 작동** — #112→#114 시나리오-커버리지 아크의 페이오프.
- **#95 승급**: 저밀도 첫 완주(active 12·onset 아래 아티팩트) → **중밀도 견고 완주**(24체·overlap 0·deadlock 0·100%, run-judge 7/7).
- **v2 부분 채택**: 빌더 u3 = stop-not-pass(충돌-free 없으면 정지·통과 금지, #115 핵심) 채택 → 다중-에이전트 collision-free 달성. 단 완전 ORCA half-plane/reciprocal-share/교착-해소 비대칭은 미채택(이 밀도선 stop으로 충분, 교착 미발현).
- **정직 caveat**: 통과 트레이스 min_pair_distance=209(대형 월드·24체 *분산*) = tight-packing 아님 → "중밀도 분산 견고"지 #112-onset 근접 밀도는 미정복. stop-not-pass 교착 리스크도 분산이라 미검증.
- **검증역전 0**: 실 빌드 96%(build 8.65M + OR 재빌드 10.04M=정직-실패 비용).

### tight-density v2 — #120 계약 검증 + OR-재빌드 의미충돌 (WO#121 RUN)
#120(정정 메트릭 + sustained 밀도)으로 tight-density 재실행 → **#120 결정적 검증** + crowd-sim verdict는 *새 머지 실패모드*로 미도달(충돌회피 실패 아님; escalated, 34.6M/40M·codex gpt-5.5).
- **#120(B) 게임불가 입증**: attempt-0서 sustained_peak_density 1433·active_avg 168.6·proximity_ticks 1465 → #119의 밀도 metering-down(44→5) 회피 불가. bounded 공간이 sustained packing 강제.
- **#120(A) 메트릭 일관 + 검증역전 0**: min_edge_gap=-0.48(<0)=진짜 겹침이 overlap_violations 18.4M·min_center 0과 일관 → 실제 겹치는 엔진 *정확히 fail*(#119의 0.64 거짓음성≠진짜 음수). 검증 기계 밀도-엄밀성 증명.
- **근접 verdict 미도달**: OR 재빌드가 u1을 예약/슬롯 흐름모델(구조적 collision-free, 유망)로 전환했으나 u1↔머지된 u2(layout) *의미/빌드 충돌*(conflict_files=[] 빈 목록 3회·tier medium→high→xhigh)로 escalate → u3(충돌 코어) 재빌드 안 됨 → 예약 엔진의 sustained 근접 행동 미검증.
- crowd-sim 캘리브레이션: 저밀도→중밀도까지 종결, **근접은 머지 모드 수정 후 재실행 필요**(충돌회피 자체는 아직 미판정).

### crowd-sim 캘리브레이션 종결 — tight 근접 collision-free (WO#123 머지수정 + #124 RUN) 🎯
#123(OR-재빌드 의미충돌 수정) 후 tight-density 재실행 → **근접 verdict 도달 + collision-free + done**. crowd-sim 아크 완전 종결.
- **#123 실효**: 의미충돌 1회 → 계약-소비 형제 리셋 1회(u3 충돌코어 + 소비자 u1/u2 공동) → 공동 재빌드 → 전원 머지. #121 머지 루프 재발 0(3회 escalate → 1회 리셋 done). escalate 0.
- **근접 collision-free done**: 2차 통합 run-judge가 sustained tight-packing(1800틱) 하 PASS — overlap 693→**0**, min_center 13.82→**14.25(≥14 임계)**, 완주 26→**100/100**, 밀도 1390(고밀도 유지)·교착 0·벽관통 0·흐름순서 위반 240→0. 빌더 **overlap-projection 엔진**(OR 대안)이 근접 유지하며 collision-free 달성. 통합 6/6.
- **★gate 정직성 end-to-end★**: 전 아크서 verification inversion 0 — 1차 (overlap 693 ∧ min_center 13.82<14) 일관 fail → 2차 (overlap 0 ∧ min_center 14.25≥14) 일관 pass. 거짓음성·거짓양성 0. 1-고객 아티팩트(#116)·메트릭버그(#119)·머지루프(#121) 매번 잡거나 고침 → 최종 통과는 진짜 벌어들인 것.
- **빌더 레벨업**: 속도-절단(liveness hack #94/#116) → stop-not-pass(근접 붕괴 #119) → 예약/슬롯(머지 막힘 #121) → overlap-projection(근접 collision-free #124). 바 상향 + 스킬이 빌더를 진짜 collision-free까지 끌어올림.
- **사다리 종결**: 저밀도(#95) → 중밀도-분산(#116, min_pair 209) → tight 근접 collision-free done(#124). #112 overlap-onset 정복.
- 비용: 자연 done(40M output cap 내). 총 ~124M(입력 = codex 컨텍스트 재전송 지배, #78 알려진 패턴 — 효율 백로그).

### Mario 챕터 — 스케일 first-contact (WO#126 RUN) 🎮
중복잡도 오리지널 플랫포머(IP-클론 금지)로 스케일 첫 probe → **머신이 스케일을 견딤**(벽 아님). codex 크레딧 외부 소진으로 2차 통합 게이트 직전 정지(haetae 50M output cap 미도달·cap=0·#91 `--continue-from` 재개 가능) — 결정적 verdict 보류, 단 3대 스케일 질문은 다 답.
- **Q1 분해@스케일 ✅**: 7유닛 disjoint(#72)·병렬 burst 작동·#40 분해 critic 정상(engine·rules·content·render+HUD·input·app + 헤드리스 engine-trace·브라우저 harness). burst가 u1+u2 동시→u3/u4/u6 동시로 스케일서 작동.
- **Q2 통합@스케일 ✅**: 6 클린 머지·1 텍스트충돌(`package.json`→#48 통합-적응 재빌드 해소)·의미충돌(#123) 0·1 정직 통합-실패→#97 통합-OR(5 impl 리셋·**2 보존**[스캐폴드 u0 + 하니스 u6]·#91 reuse)·**escalate 0**. #97 스코핑이 교과서적 — 하니스 *계약*은 고정, *구현*만 재빌드.
- **Q3 게임플레이 하니스 ✅ REAL(골드스탠더드, hollow 아님)**: 4 trace 표면(engine·browser-render·browser-playable·browser-failure-loop)·42필드/20스텝·anti-placeholder 조항(스캐폴드 값은 placeholder, 실증거는 u1~u6 강제). **독립 실증**(director 머신 `trace:engine` JSON): 점프아크(y384→peak306→복귀, 중력)·좌우이동·platforms_crossed 3·코인 0→1·스톰프 처치 1·옆접촉 데미지 3→2·낙하 데미지 3→2→1→0(틱 11/22/33)·깃발 클리어(clear_reached)·게임오버·HUD life [3,2,0]·canvas_non_empty_pixels 518400·keydown/up 4. crowd-sim 초기 hollow와 정반대 — 요구 행동 전부 실검증.
- 산출물: `npm run build` exit 0(`tsc --noEmit && vite build`, 13.41kB JS 번들·14 모듈)·35 TS 파일(engine/rules/content/render/hud/input/app + 11 trace 스크립트·browser-cdp 포함) — **진짜 컴파일·플레이 가능 Vite 앱**(일반 머신선 dev 서버가 127.0.0.1로 서빙→브라우저 플레이; 샌드박스만 차단).
- **2 갭**: **(1 소프트, 해소)** 증거계약이 `deps=[]` **스캐폴드 u0**에 부착 → 엔진(u1) 없이 충족 불가 → 토대유닛 5 dispatch 낭비(OR-대안이 흡수·자가교정). **(2 새, "플랫포머 onset")** 브라우저-하니스(headless Chrome/CDP)가 127.0.0.1 서버 띄우려다 샌드박스 loopback listen **EPERM** 차단 → `trace:browser-render` 통합 fail(게임 hollow 아님). **fix = server-less 검증**(in-sandbox engine-trace를 행동 권위로 + render는 file://·data-URI/best-effort), **ALLOWED_SANDBOXES 불변**(안전 불변 = loopback 허용 안 함, #84 강화).
- **핵심**: 스케일은 벽이 아니다 — 분해·통합·하니스 다 견딤. 다음 캘리브레이션 타깃 = **갭#2(server-less 하니스)**, crowd-sim 아크식 *막힘→진단→수정→재실행*.

### Mario 챕터 첫 깨끗한 완주 — #128 실효 (WO#128 fix + #129 RUN) 🎯
#128(증거계약 dep-배치 + server-less 게임플레이 검증) 후 플랫포머 fresh 재실행 → **통합 8/8 done**. #126이 크레딧 소진으로 못 받은 verdict를 깨끗이 받음.
- **#128(A) ✅ dep-배치**: 계약 u5(생산자)에만·스캐폴드 유닛 0·disp=5/pass=5(유닛당 1)·토대 낭비 0(vs #126 u0=5). #126 실패모드 구조적 소멸.
- **#128(B) ✅ server-less**: EPERM/loopback/127.0.0.1 전부 0(런 전체·#126서 서버 띄운 u4 포함). 하니스 "서버 없는 node 헤드리스 트레이스"·dev no --host. 통합이 server-less engine-trace 증거로 8/8 통과. **ALLOWED_SANDBOXES 강화**(loopback 안 함).
- **검증 깊이 유지·inversion 0**: 8/8 = 실제 플레이스루(이동·코인 2·스톰프·옆접촉 데미지·라이프 3→2→1→0·게임오버·게임오버 후 입력무시·canvas 124800px·HUD). server-less ≠ hollow.
- **스케일·비용**: 5유닛·충돌 0·OR 0·escalate 0(머신 발동 불필요). ~17M(vs #126 ~124M·7× 저렴) — #128이 스캐폴드 낭비 + 브라우저-EPERM OR 처닝 제거 → 자연 done. 산출물 npm build exit 0(12.24kB·플레이 가능).
- 아크: crowd-sim 종결(#124) → 스케일 first-contact(#126·2갭) → #128 fix → #129 깨끗한 done 🎯. **스케일은 벽 아님 + server-less 검증 실효 입증.**

### 길 B 3/3 완주 + 검증/루프 정련 (WO#97–100)
**길 B 완전 3/3**: md-editor + snake + **kanban** 전 사슬 완주. kanban은 #89 budget·#92 시나리오 결함으로 막혔다가, 2번 클러스터(#97/#98/#99) 적용 후 fresh 첫 완주(통합 8/8, 17.66M, 6유닛 전부 first-try, 헛재시도 0).
- **#97 OR 통합-대안 연루-유닛 한정 리셋**: #41/#52 OR가 seeded-done 포함 전체 리셋하던 #92 비일관 수정 — 연루 유닛만 리셋, #91 seeded-done 보존(reuse 유지·머지충돌 재발 방지). 실패 기준→소유 유닛 매핑(#26/#72), 폴백 전체 리셋.
- **#98 하니스 시나리오 계약**(#78 필드계약의 시나리오판): 합성기가 evidence_fields와 함께 scenario_steps(기준 입증 흐름) 유도 + 스킬이 올바른 시나리오 구성(완전 흐름·같은 엔티티 전 상태·검사 前 보존). #100서 입증 — ac6 DnD·ac5 persistence 시나리오 결함(#92) 해소, 통합 pass.
- **#99 하니스 탐지 정교화**: "키워드 언급" → "증거-생산(run/sim:trace 체크·evidence_fields/scenario_steps 보유)" 게이트. 준비/스캐폴드 유닛(#89 snake u0) 과매칭 제거. #100서 u5(DnD 구현) 오탐 0 입증. gate is_harness가 계약-구동이라 intake 분류만 수정.
- **클러스터 효과**: 거짓 음성·헛재시도 제거 → kanban이 #89(21.2M·미완) 대비 *더 싸게(17.66M) 완주*. 셋 다 빌더-측/탐지-분류만(적대 run-judge·gate 판정 무접촉 일관).
- **결산**: 검증 기계 완결 — 하니스 사슬(필드#78·종류#84·stdout#86·시나리오#98·탐지#99) + 효율 재개(#91·#97) + 적대 분리 보존. 길 B 3/3 + crowd-sim 북극성 첫 완주.

### 남은 갭 (운영/후속 — 검증 인프라 결함 아님)
1. ✅ **continue-from 재사용 거부 → rebuild-all — #91 해소(#92 실루프 검증)**: 순수 재개가 부모 plan/criteria를 보존(spec.yaml 로드·재합성 skip) → #71 reuse 매칭·done 재빌드 0. (구 증상: 재합성이 criteria/분해를 매번 바꿔 reuse 거부 → rebuild-all + plan 비대, 반복 #81·r2·r3.) **잔여도 해소(#97/#100)**: 통합 실패 시 #41/#52 OR-대안이 *연루 유닛만* 리셋·seeded-done 보존 → #100 kanban fresh 완주서 OR 미발동(전 유닛 first-try).
2. **큰 plan budget**: 7유닛+하니스 검증이 20M 초과(kanban). 캡 상향 또는 plan-trim/재시도 효율.
3. ✅ **하니스 키워드 과매칭 — #99 해소(#100 검증)**: 탐지를 증거-생산 게이트로 정교화(준비/스캐폴드 제외) → #100서 u5(DnD 구현) 오탐 0. (구 증상: desc "trace" 언급 준비 유닛이 오탐→계약 부착→미생산 fail, 캡스톤 #89 snake u0.)
4. **병렬 fresh 빌드 회피**: 동시 npm install이 cache 경합 → fresh 캡스톤 순차.

---

## 7. OMC 차용 후보 (수렴 분석 — LEAP·Dynamic-Workflows에 이어 3번째 외부 수렴)

OMC(CC-플러그인 오케스트레이터)와 haetae가 또 같은 자리 수렴(병렬+검증루프+영속상태+티어라우팅+스킬주입). haetae에 없거나 약한 4종:
1. ✅ **Disjoint 병렬 burst (WO#110)** — ready 유닛 중 *scope 입증 disjoint*(#72 — 서로 file-scope 겹침 0·의존 없음) 집합은 `--max-parallel-burst`(≥--max-parallel)까지 동시 실행 허용 — 보수적 cap의 주 이유인 머지충돌 리스크가 disjoint면 부재하므로 cap을 *자원 한계*에만 묶는다(머신에 맞게 설정). 비-disjoint/scope-미선언은 --max-parallel 한정. **충돌 backstop(#21 serialize-on-conflict·#48 충돌적응 재빌드) 불변** — disjoint 판정이 틀려도 안전망. **opt-in 기본 보수적**(burst 미지정=0 → eff=max_parallel → 기존 동작 byte-identical). 스케줄링만(scheduler.is_disjoint_from 순수 술어 + loop dispatch cap) — gate/judge/run-judge/바/codex/state 스키마/ALLOWED_SANDBOXES 불변. 단 병렬 npm 경합 등 격리 비용은 사용자가 자원 cap으로 조절(#89서 실측).
2. ✅ **스킬 3층 멘탈모델 (WO#111)** — 실행층/강화층/보장층 분리를 docs/ARCHITECTURE.md §5로 명시 결정화. haetae가 매 WO에서 *이미 코드로 강제*한 분리("빌더-측만·judge 무접촉")를 명시 프레이밍으로 박음: 실행=executor(codex·offline) / 강화=스킬·스캐폴드·계약·학습스킬(빌더-측 `apply_builder`, **바 무접촉**) / 보장=gate·적대 run-judge·구조체크(독립·적대, **유일한 판정 주체**). 핵심 원칙=**보장층 독립성**(강화가 보장 건드리면 자기합리화→가짜 done; haetae "governed GATE" 차별점의 뿌리) + 자기개선 안전망(강화 표류해도 독립 보장층이 나쁜 산출 통과 불가). docs만(코드/테스트 0).
3. ✅ **control/data-plane + artifact descriptor (WO#102 phase 1·WO#109 phase 2)** — ArtifactDescriptor 인프라(path·content_hash·size·kind·retention·summary) + bounded-handoff(8KB). 큰 trace를 data-plane(`<run-dir>/artifacts/`)로, state.yaml은 descriptor 참조. **판정 불변**(오프로드는 직렬화에서만·in-memory full trace 보존·gate.py 무접촉) · state 96%↓(41KB→1.5KB) · back-compat. #55 sidecar 일반화. **phase 2(WO#109): 측정-우선 → 정직 no-op** — 실 run 20건 post-#102 측정 결과 trace가 지배적이었고, 그 뒤 8KB 임계를 넘는 단일 write-once 블롭 0건(최대 result 3.3KB), WO 1순위 `prompt`는 state에 미존재(짧은 `work_order_ref` 참조뿐). event=append-only 타임라인·cost=권위/dashboard-live이며 둘 다 임계 미만 → 인라인 유지. 새 오프로드 코드 무추가(speculative 금지), 측정+readiness만 박제(docs/STATE_SIZE_PHASE2.md·tests). artifacts.py는 이미 kind-범용 = 미래 블롭 재사용 가능.
4. ✅ **스킬 자동 학습 learner (WO#103)** — 완주 캡스톤서 재사용 *패턴* 후보를 staging(`skills/_candidates/`) 추출 + provenance. **F.1 거버넌스: 자동채택 0**(load_skills가 `_`접두 구조적 제외·사람 `--approve` 전 미편입·미주입) · 빌더-측만(judge 무수신)·**독립 적대 gate가 backstop**(나쁜 학습 스킬도 나쁜 산출 통과 불가 = 자기학습 표류 안전망) · lint(자가채점/바완화/구현덤프 차단)·IP 원본. #32(seeded)의 거버넌스형 자동화.

**차별점 유지**: OMC verifier=같은 시스템 opus 에이전트 체크리스트 / haetae gate=적대·독립 + 검증기 자체 검증(#78~#86 사슬). "정직한 실패" 축 우위는 보존하며 차용.

---

## 8. 로컬-모델 executor arc (#133–#153) — thesis 실질 실증

> **약한 로컬 모델을 *빌더*로, 강한 codex를 *judge/critic*으로.** 전 파이프라인(분해 · 적대 gate · self-test 피드백 · OR · 통합 적응)이 모델 강도를 보강하는가? — 실제 JS 게임에서 **단일-책임 검증 유닛 5/6 수렴 = thesis 실질 실증.** GB10 로컬 추론 사다리(#133–#136) → `--executor local`(#137) → lift 측정(#138) → 빌더-측 정련(#139–#153).

### thesis
적당히 작은 *최신* 로컬 모델 + 모든 기법(분해 · 적대 gate · self-test 피드백 · OR · 통합 적응) = **검증된 결과**. **오케스트레이션 > 모델 강도.** §0의 "범용 LLM + agentic harness면 충분 · executor는 pluggable"을 *약한 빌더* 극단으로 민 것 = provider-agnostic(§0 3축)의 라이브 실증. 외부 수렴(2026): 모델은 구조화 하니스 안에서 현저히 더 잘 작동(§5 Loop Engineering · CRDAL 분리-검증자와 같은 자리).

### 빌더 구성 (DGX Spark GB10, #133–#137)
- **모델**: Qwen3.6-35B-A3B Q4_K_XL + MTP(`--spec-type draft-mtp --spec-draft-n-max 3`, ~1.36×) + **thinking-off**. llama.cpp(84de01a, GB10 sm_121) idle **~55 t/s**.
- **교훈**: MoE 희소성 ≠ 속도(GB10 反證) · MTP가 실효 속도 레버 · thinking-on은 hang. GPU 추론은 ollama/SGLang 아닌 **llama.cpp**서 풀림(#136 — sm_121 스택 미성숙 게이트 해소).
- **분리**: judge/critic = 강한 **codex**(불변 · 적대 분리). 빌더만 로컬 — `providers/local_agent.py LocalAgentExecutor`(#137, OpenAI 엔드포인트 · stdlib만 · `complete()` 부재라 judge_client 불가 = 구조적 분리).

### lift 결과 — 약한 빌더가 진짜 코드를 수렴
- **실제 JS 게임(snake) 단일-책임 유닛 5/6 수렴**: 약한 로컬 빌더 → 실코드 → builder-side self-test green → **codex 행동 gate → pass**. **per-unit lift 라이브 실증.**
- ttt(#138): u1 수렴 · u2(minimax)는 *인공 전수-무적 바*(critic-강화 v2)서 막힘 — 빌더 역량 아닌 *바 난이도*.

### floor 사다리 (각 막힘 → fix, 매번 위로)
| floor (막힘) | fix |
|---|---|
| collection-error #138 (테스트가 자기 impl에 없는 API import) | builder-side smoke `collect-only` #139 |
| smoke ↔ gate discovery 불일치 #140 | gate-discovery 정렬 #141 |
| 정답성 / 루프-수렴 #143 | 정밀 self-test 피드백(assertion-detail → 타겟 수정) #144 |
| JS 미커버(Python만) #145 | self-test JS/vitest 확장 #146 |
| 분해 입도 — 엔진 한 덩어리 #147 | 단일-책임 disjoint-scope 분할 #148 |
| truncation → 스텁(로드된 박스) #149 | 스트리밍 idle-timeout(#54 원칙) #150 |
| 유닛 밀집 u3(detection + state 혼재) #151 | distinct-KIND 분할 재균형 #152 |
| 유닛 밀집 u4 — KIND-탐지 라이브 돌파 #153 | **→ 남은: integration floor** |

### ★ 적대 분리 라이브 실증 ★
**self-test green = 필요조건이지 충분조건 아님.** 빌더가 *자기 테스트*를 green 내도 gate의 *행동 바*가 진짜 바 — #145 u2 · #147 · #151서 입증(self-test 통과 산출이 행동 gate서 정직하게 fail). 빌더-측 보조(smoke/self-test: #139·#144·#146) · director-측 계획(decomp-critic 입도: #148·#152)이 **적대 gate(행동 run-judge · hollow #98 · 통합)를 타락 안 시킴** — 전 arc **검증역전 0**. §7-2 보장층 독립성의 *약-빌더 극단* 입증.

### 남은 floor = integration
wire + **풀-행동 트레이스**(전체 행동 사슬을 *한 플레이스루*로 실증)가 **어떤 단일 유닛보다 어렵다.** 통합 유닛이 over-bundled(파사드 + 어댑터 + 트레이스 = 3 KIND). 다음 sub-arc:
- **(a)** decomp-critic가 통합-급 유닛에 *구조적 재분해*(현재 in-place 재계획 부족 — #152 KIND-분할의 통합판).
- **(b)** 전용 트레이스-하니스 유닛 + 풀-사슬 `scenario_steps`(#113 "시나리오 커버리지 = gate 엄밀성 상한" 원칙).
- **(c)** 비용(아래).

### integration sub-arc (#155–#158) — wire 돌파 + gate 핵심 가치 재입증
- **진전**: 약 Qwen3.6 빌더가 snake에서 행동 유닛 4/4 lift(반복) + **wire 통합 수렴**(#156 wire floor 돌파, OR 대안). #155(wire|트레이스 분리)·#157(검증-트레이스 비-split + 트레이스-하니스 스캐폴드) 구조적으로 작동.
- **🔑 핵심 발견 — 빌드-pass ≠ 런타임-작동**: #158서 통합 게임이 빌드는 되나 런타임 크래시(엔진이 모듈 메서드를 static/instance 계약 불일치로 호출). 단위테스트(고립)·integration 빌드-체크 둘 다 놓침 — *통합 엔진을 실제 실행하는 유일한 유닛=행동 트레이스*가 정직히 fail → escalate. **안 도는 게임은 full-done 못 받음**(검증역전 0·#98/#81 hollow-green 라이브). = 파이프라인 존재 이유 재입증.
- **floor 사다리(integration)**: over-bundled 통합 유닛(#153) → wire|트레이스 분리(#155) → 풀-사슬 트레이스 역량 초과 + #152↔#113 긴장(#156) → 검증-트레이스 비-split + 스캐폴드(#157) → **wire 수렴, 트레이스-fill 막힘(#158: 빌더 import 추측 + 컨텍스트 절단 + 통합 런타임 계약 버그)**.
- **남은 integration 프론티어(다음 sub-project)**:
  - (B) **통합 런타임 gate 갭**: integration ac가 빌드-only → 런타임 계약 버그 미포착. *통합 유닛에 런타임-smoke(엔진 인스턴스화 + 1-tick 실행)* 추가 → 트레이스 전에 계약 버그 포착(바 강화, 완화 아님).
  - (A) **트레이스 scaffold import 정밀화 + 컨텍스트 확대**: 스캐폴드가 import placeholder를 정확한 엔진-facade 계약으로 미리 채워 빌더 추측 제거 + llama-server 컨텍스트 8192→확대(절단 ~60회 해소).
  - tier-bump은 후순위(계약/절단이 1차, 역량 2차).
- **비용**: 트레이스 재시도가 비쌈(#158 ~2.18M, u7 단독 206K — 매 실패가 codex run-judge 1콜). 통합-급 유닛이 비용 핫스팟.
- **적대 분리/무결성**: 전 sub-arc 검증역전 0·#113 바 불완화·서버리스(#128)·judge=codex 불변. 빌더-측(self-test)·director-측(decomp/scaffold) 보강이 적대 gate를 타락 안 시킴.

### integration sub-arc 결론 (#160–#163) — DOM-경계 역량 ceiling
- **#160–#161 성과**: facade 계약 + runtime-smoke로 *빌드-passes-but-crashes* 통합 계약버그(Food.generate static/instance 류)를 통합서 포착 → 빌더가 수정 → **게임이 실제로 빌드+작동**(독립검증: 먹이/점수/충돌/game-over/헤드리스 bootstrap 정확). #158 명확히 넘어섬.
- **#162–#163 — floor가 닫히지 않고 *옮겨감***: #162가 트레이스 하니스에 검증된 헤드리스 어댑터(installHeadlessDOM)+정확 import 스캐폴드 → #163서 *같은* 약빌더 혼동(jsdom 끌어옴·헤드리스-테스트 vs 브라우저-앱 DOM)이 *통합으로 이동*(app.js/main.js에 jsdom→build+smoke 깨짐; 어댑터 제공됐으나 빌더 0회 사용). piecemeal 수정 = floor 이동이지 폐쇄 아님.
- **🔑 발견 — 약빌더 × DOM-경계 역량 ceiling**: 약 Qwen3.6 빌더는 *비-DOM 행동 로직*을 안정 수렴(매 런 6/6 단일-책임 유닛)하나, *DOM-경계*(브라우저-앱 조립 + 헤드리스 검증 인프라: 브라우저-DOM vs 헤드리스-테스트 구분, jsdom 회피)에 지속적 ceiling. DOM-닿는 유닛마다 같은 혼동 재출현(트레이스→통합). 궤적 점근-아님(고칠 버그가 아니라 raw 역량 한계).
- **thesis 결론(경계지어 입증)**: *오케스트레이션 > 모델 강도* 는 정밀히 경계지어 성립 — 파이프라인이 약빌더로 (a)역량 내(행동 로직, + runtime-smoke로 통합 계약버그까지) *검증된-좋은 결과*를 내게 하고, (b)역량 밖(DOM-경계)은 *정직하게 게이트*(절대 가짜 done 0). 5+런 검증역전 0·#113 바 불완화·적대 분리·서버리스 전 구간 유지. = 안 도는/미검증 게임에 도장 0이 파이프라인 핵심 가치의 결정적 입증.
- **남은 옵션(서브프로젝트 재개 시)**: 포괄적 DOM-steering(app 진입점=실DOM·jsdom 금지 + 모든 헤드리스 하니스=installHeadlessDOM, synthesizer/scaffold 가이드). 단 *옮겨가는 floor* 특성상 강한 스캐폴드를 약빌더 특정 약점에 쌓는 것이라 thesis 깨끗함과 trade-off — 또는 강한 tier 빌더로 DOM-경계만 격상. 정직 baseline = ceiling 발견 자체가 완결된 결과.
- **비용 메모**: 통합/트레이스 재시도가 비용 핫스팟(#161 3.13M·#163 1.76M, codex judge/critic 지배). 행동 로직은 cheap+안정.

### 파이프라인 강화 arc (#165–#168) — A/B/C 라이브 검증 + 경계 정밀화
- **A disjoint-scope (#165)** ✓ 라이브: 헤드리스 로그라이크 런서 합성이 11유닛에 배타 소유 파일(∩=∅) + 깨끗한 DAG + facade 계약(#160) 부여, 병렬 disjoint 디스패치, **전 런 머지충돌 1건**(snake 통합 벽 대비 현저히 청결). 단서: scope 배정하나 약빌더가 항상 준수는 안 함(OR-재빌드가 scope 벗어남 → #48이 처리).
- **B research (#166)** ✓ 라이브: RESEARCH stage 발화(복잡도 게이트 통과) → ResearchBrief(후보 10유닛) → 합성기가 조사된 disjoint 경계+facade 계약 채택. --research opt-in 작동.
- **C lineage (#167)**: 첫 런 parent null(정확) — 다-런 체인+대시보드 트리는 mechanism+test 검증, *라이브 다-런 미자극*(fix→continue 시 자극).
- **🔑 경계 정밀화 (#163 → #168)**: ceiling은 *DOM-경계만이 아니다*. 약빌더는 단순 행동 유닛(types/rng/map/이동/아이템 = 클린 수렴)은 안정 수렴하나, **알고리즘적으로 어려운 유닛(BFS 추적 AI·death-sweep 전투)서 행동-로직-복잡도 floor**. 비-DOM은 더 멀리 가나(DOM 혼동 0) 복잡 알고리즘도 floor. → **역량 경계 = DOM-경계 ∪ 복잡-알고리즘**.
- **thesis 결론 (양쪽 경계 매핑)**: *오케스트레이션 > 모델 강도* 는 경계지어 성립 — 파이프라인이 약빌더로 역량 내(단순 행동 로직, disjoint 다유닛 조립, runtime-smoke 통합 계약버그)에선 검증된-좋은 결과를 내게 하고, 역량 밖(DOM-경계 ∪ 복잡-알고리즘)은 정직하게 게이트. 전 arc 검증역전 0·#113 불완화·적대 분리·서버리스 유지. A/B/C가 통합 표면을 청결히 함(머지충돌 1건)을 라이브 입증.
- **남은 옵션**: 복잡 유닛에 (a) 능력-인지 라우팅(어려운 유닛→강빌더 tier), (b) 패턴 주입(#32 레지스트리에 BFS/turn-combat 패턴), (c) C lineage 라이브 검증용 fix→continue. 정직 baseline = 경계 매핑 자체가 완결 결과.

### 비용 구조
- 약한 빌더 **쌈**(~10K/유닛) · 강한 codex judge/critic **비쌈**(풀 런 ~1.4M; #138 실측 codex judge ~461K ≫ 로컬 빌더 ~10K).
- codex replan **~5분/콜 = 벽시계 병목**(토큰 아닌 *시간*) → 효율 백로그(§3)와 연결.
