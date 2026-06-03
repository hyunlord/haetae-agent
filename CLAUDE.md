# CLAUDE.md — haetae-agent 작업 규약

이 파일은 이 레포에서 일하는 코딩 에이전트(Claude Code / Codex)를 위한 규약이다.

## 지금 우리가 있는 단계

부트스트랩. **데몬·CI·패키징·멀티 provider·skill 레지스트리를 미리 만들지 마라.**
지금 목표는 단 하나: director의 *명세 합성기(spec synthesizer)*를 동작하게 만드는 것.
얇게, 한 조각씩.

## 역할 분담 (dogfooding)

haetae가 자동화할 루프를 지금은 사람이 손으로 돈다:

- **Director** = 사람 + Claude(웹). 계획하고, 의뢰서(work order)를 쓰고, 결과를 평가하고, 다음을 결정.
- **Executor** = Claude Code / Codex. 의뢰서를 받아 구현하고, 결과를 텍스트/산출물로 정리해 돌려준다.

즉 너(executor)는 director가 준 work order 범위만 수행하고, 결과를 명확히 보고한다.
범위를 벗어난 결정(방향 전환·스코프 확장)은 director에게 돌려라.

## 언어

- 문서·주석·사람이 읽는 텍스트: **한국어**.
- 코드 식별자·스키마 키·enum 값: **영어**.

## 검증

- 자동 평가 파이프라인(HARNESS pipeline)은 아직 만들지 않는다.
- 단, 작업마다 `check`(테스트/린트/빌드 명령)는 명시하고 손으로 돌려 검증한다.

## 미결 (아직 정하지 않음 — 임의로 확정하지 마라)

- 구현 언어/런타임 (Python 유력하나 미확정)
- provider-agnostic LLM 레이어 구체 선택
- 상태 저장 포맷의 세부
