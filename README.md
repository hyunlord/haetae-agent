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
