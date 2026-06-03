"""수동 데모 — 실제 codex로 주문을 ProjectSpec으로 합성해 YAML로 출력.

실제 codex CLI가 설치/인증돼 있어야 한다. cwd가 repo 루트라고 가정하고
기본 prompt 경로(prompts/synthesizer.md)를 쓴다.

    uv run python -m haetae.demo --order "WorldSim에 전투 시스템 추가해"
    uv run python -m haetae.demo --order "..." --model gpt-5.3-codex
"""

from __future__ import annotations

import argparse
import sys

import yaml

from haetae.intake import SynthesisError, synthesize
from haetae.providers.codex import CodexClient, CodexError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="haetae.demo", description="order -> ProjectSpec (codex)")
    parser.add_argument("--order", required=True, help="주문 원문")
    parser.add_argument("--model", default=None, help="codex 모델 override (미지정 시 codex 기본)")
    parser.add_argument("--context", default=None, help="project_context (선택)")
    args = parser.parse_args(argv)

    client = CodexClient(model=args.model)
    try:
        spec = synthesize(args.order, client, context=args.context)
    except (SynthesisError, CodexError) as e:
        print(f"[합성 실패] {e}", file=sys.stderr)
        return 1

    dumped = yaml.safe_dump(
        spec.model_dump(by_alias=True, mode="json"),
        allow_unicode=True,
        sort_keys=False,
    )
    print(dumped)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
