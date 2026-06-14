# haetae-agent 로드맵

> 해태(獬豸) = 시비·선악을 판별하는 신수. 차별점 = **언제 done이 아닌지 아는 governed GATE.**
> autonomous director: 의뢰 하나 → governed spec → `synthesize → replan → [분해 critic] → dispatch(executor) → gate → replan` 루프 → done/escalate/stop.
>
> **최종 갱신: 2026-06-14 · WO#1–109 · 996 tests · main @4721e17 · 길 B 3/3 · crowd-sim 북극성 첫 완주 · OMC #3(phase 1·2)·#4**

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
1. **Disjoint 병렬 burst** [후보] — scope disjoint 입증 시 #72 위에 더 공격적 병렬(유닛 내/독립 수정). 단 병렬 npm 경합 등 격리 비용 주의(#89서 실측).
2. **스킬 3층 멘탈모델** [후보] — 실행층/강화층/보장층 분리. haetae의 gate=보장층을 명시 레이어로 개념화 → 조합·재사용 사고 또렷.
3. ✅ **control/data-plane + artifact descriptor (WO#102 phase 1·WO#109 phase 2)** — ArtifactDescriptor 인프라(path·content_hash·size·kind·retention·summary) + bounded-handoff(8KB). 큰 trace를 data-plane(`<run-dir>/artifacts/`)로, state.yaml은 descriptor 참조. **판정 불변**(오프로드는 직렬화에서만·in-memory full trace 보존·gate.py 무접촉) · state 96%↓(41KB→1.5KB) · back-compat. #55 sidecar 일반화. **phase 2(WO#109): 측정-우선 → 정직 no-op** — 실 run 20건 post-#102 측정 결과 trace가 지배적이었고, 그 뒤 8KB 임계를 넘는 단일 write-once 블롭 0건(최대 result 3.3KB), WO 1순위 `prompt`는 state에 미존재(짧은 `work_order_ref` 참조뿐). event=append-only 타임라인·cost=권위/dashboard-live이며 둘 다 임계 미만 → 인라인 유지. 새 오프로드 코드 무추가(speculative 금지), 측정+readiness만 박제(docs/STATE_SIZE_PHASE2.md·tests). artifacts.py는 이미 kind-범용 = 미래 블롭 재사용 가능.
4. ✅ **스킬 자동 학습 learner (WO#103)** — 완주 캡스톤서 재사용 *패턴* 후보를 staging(`skills/_candidates/`) 추출 + provenance. **F.1 거버넌스: 자동채택 0**(load_skills가 `_`접두 구조적 제외·사람 `--approve` 전 미편입·미주입) · 빌더-측만(judge 무수신)·**독립 적대 gate가 backstop**(나쁜 학습 스킬도 나쁜 산출 통과 불가 = 자기학습 표류 안전망) · lint(자가채점/바완화/구현덤프 차단)·IP 원본. #32(seeded)의 거버넌스형 자동화.

**차별점 유지**: OMC verifier=같은 시스템 opus 에이전트 체크리스트 / haetae gate=적대·독립 + 검증기 자체 검증(#78~#86 사슬). "정직한 실패" 축 우위는 보존하며 차용.
