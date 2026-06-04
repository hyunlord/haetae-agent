# 해태 · haetae-agent

> 주문 하나로 검증된 완성까지 — **멈출 줄 아는** 자율 프로젝트 러너.

해태(獬豸)는 옳고 그름을 가려내는 짐승이다. 이 프로젝트의 본질도 같다:
자율 에이전트가 끝없이 일하게 만드는 건 쉽다. 어려운 건 **언제 멈출지 판단하는 것**이다.

## 한 줄 정의

`order → spec → plan → dispatch(executor) → gate → decide → loop`
를, *검증된 완성*에 도달하거나 사람에게 escalate할 때까지 스스로 반복하는 local-first director 데몬.

## 포지셔닝

OpenClaw류 범용 에이전트가 "손"이라면, haetae는 **멈출 줄 아는 손**이다.
차별점은 actuator가 아니라 **gate** — "이 정도면 됐다"를 빡세게, 여러 조건으로, 자기채점 없이 판정하는 부분.

## 레이어 (요약)

- **Director** — 주문을 검증 가능한 spec으로 합성하고, 작업을 계획하고, 결과를 보고 다음을 결정. (직접 구현 = 핵심 IP)
- **Gate (HARNESS)** — 객관 검증 + 독립 판정자 + 다중 정지조건. (직접 구현 = wedge)
- **Executors** — Codex / Gemini / claude -p / 로컬 LLM / AutoDev 등 pluggable 어댑터.
- **Runtime kernel** — heartbeat 루프, event-sourced state(디스크), 샌드박스, provider-agnostic LLM 라우터, skill, budget.

## 상태

부트스트랩 단계. 데몬·CI·패키징은 아직 없음.
현재는 director 로직(특히 명세 합성기)을 사람이 손으로 루프를 돌리며(dogfooding) 만들고 있다.

## ⚠️ 자율 executor 안전 한계 (CodexExecutor)

`--executor codex`(opt-in)는 LLM이 만든 work order를 **쓰기 권한으로** 실행한다.
방어선은 두 겹뿐이다: (1) 가장 좁은 쓰기 sandbox `workspace-write`(danger-full-access는
코드 화이트리스트가 차단), (2) 실행 범위를 `--workdir`로 한정(`-C`).
이건 프로세스 수준 격리라 **충분하지 않다.** 지금은 버리는 scratch 폴더
(예: `~/haetae-test/...`)에만 써라. 진짜 repo에 물리려면 컨테이너/VM 격리가
필요하며 그건 후속 hardening이다.
