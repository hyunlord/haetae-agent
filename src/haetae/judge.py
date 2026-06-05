"""LLM-as-judge — CheckRunner가 못 보는 주관·품질 기준을 적대적 LLM으로 판정.

CheckRunner는 exit-code만 본다. UI/문서 품질·"읽기 쉬운가" 같은 `judge` 타입 기준은
기계로 못 잰다. 그걸 **독립된 read-only LLM judge**가 평가한다.

설계 결정(director):
  - judge는 executor와 *다른 모델*(cross-provider decorrelation, best-effort 독립).
    그래서 client는 호출부에서 주입한다(CodexClient(judge_model) 등).
  - judge는 result 요약만 믿지 않는다 — self-report 합리화 위험. workdir의 실제 산출
    파일까지 읽혀 판정시킨다(합리적 용량 cap).
  - 적대적 프레이밍: prompts/judge.md가 "기준 미충족 이유를 찾아라, 명확·완전 충족일
    때만 pass" 라고 지시한다.
  - 여러 judge 기준을 *한 번에* 평가한다(느린 codex 호출 수 절감).

견고성: judge 출력이 깨지거나 일부 ac가 누락이면 그 기준은 skipped("judge 평가 불가")로
떨군다 — crash 금지. skipped는 상위 집계에서 ambiguous로 흘러 escalate된다.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from haetae.llm import LLMClient
from haetae.models import (
    AcceptanceCriterion,
    CheckReport,
    CheckType,
    ProjectSpec,
    RunEvidence,
)
from haetae.parsing import ParseError, parse_yaml_model

DEFAULT_JUDGE_PROMPT_PATH = "prompts/judge.md"
DEFAULT_RUN_JUDGE_PROMPT_PATH = "prompts/run_judge.md"

# 파일 수집 시 건너뛸 노이즈 디렉토리(경로 일부에 등장하면 제외).
_SKIP_DIRS = {
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".idea",
    ".tox",
    "dist",
    "build",
    ".egg-info",
}


# ──────────────────── judge 출력 파싱 모델(내부 전용) ────────────────────


class _JudgeVerdict(BaseModel):
    """judge가 기준 하나에 내린 판정. status는 pass|fail(그 외는 견고성 처리에서 skipped)."""

    ac_id: str
    status: str
    reason: str | None = None


class _JudgeOutput(BaseModel):
    """judge 응답 최상위 — verdicts 리스트 하나. parse_yaml_model이 요구하는 dict 형태."""

    verdicts: list[_JudgeVerdict] = Field(default_factory=list)


def _norm_status(s: str | None) -> str | None:
    """judge가 흘린 status 변종을 pass|fail로 정규화. 못 알아보면 None(→ skipped)."""
    v = (s or "").strip().lower()
    if v in ("pass", "passed", "ok", "yes", "true"):
        return "pass"
    if v in ("fail", "failed", "no", "false"):
        return "fail"
    return None


class LLMJudge:
    """workdir 산출물을 적대적 LLM에 실어 judge 타입 기준을 평가하는 러너.

    client:        read-only LLMClient(executor와 다른 모델 권장).
    workdir:       산출 파일을 수집할 루트.
    prompt_path:   적대적 루브릭 시스템 프롬프트.
    max_file_bytes: 파일 1개당 포함 상한(초과 파일은 통째로 제외).
    max_total_bytes: 전체 수집 바이트 상한(초과 시 그 지점에서 수집 중단).
    """

    def __init__(
        self,
        client: LLMClient,
        workdir: str | Path = ".",
        prompt_path: str | Path = DEFAULT_JUDGE_PROMPT_PATH,
        max_file_bytes: int = 64_000,
        max_total_bytes: int = 200_000,
        run_prompt_path: str | Path = DEFAULT_RUN_JUDGE_PROMPT_PATH,
    ):
        self.client = client
        self.workdir = str(workdir)
        self.prompt_path = prompt_path
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes
        self.run_prompt_path = run_prompt_path

    # ── 공개 API ──────────────────────────────────────────────────────
    def judge_criteria(
        self,
        judge_acs: list[AcceptanceCriterion],
        result: str,
        spec: ProjectSpec,
    ) -> list[CheckReport]:
        """judge 타입 기준들을 한 번의 LLM 호출로 평가해 CheckReport 리스트 반환.

        judge_acs가 비면 호출 0회로 빈 리스트. 출력이 깨지거나 일부 ac가 빠지면 그
        기준만 skipped("judge 평가 불가") — 절대 raise하지 않는다.
        """
        if not judge_acs:
            return []

        system = Path(self.prompt_path).read_text(encoding="utf-8")
        files = self._collect_files()
        user = self._build_user(judge_acs, result, files)

        raw = self.client.complete(system, user)

        by_id: dict[str, _JudgeVerdict] = {}
        try:
            parsed = parse_yaml_model(raw, _JudgeOutput)
        except ParseError:
            parsed = None  # 통째로 깨짐 → 전부 skipped로 떨군다(아래 루프).
        if parsed is not None:
            for v in parsed.verdicts:
                by_id[v.ac_id] = v

        reports: list[CheckReport] = []
        for ac in judge_acs:
            v = by_id.get(ac.id)
            status = _norm_status(v.status) if v is not None else None
            if status in ("pass", "fail"):
                reports.append(
                    CheckReport(
                        ac_id=ac.id,
                        check_type=CheckType.judge,
                        cmd=None,
                        status=status,
                        exit_code=None,
                        detail=(v.reason if v is not None else None),
                    )
                )
            else:
                reports.append(
                    CheckReport(
                        ac_id=ac.id,
                        check_type=CheckType.judge,
                        cmd=None,
                        status="skipped",
                        exit_code=None,
                        detail="judge 평가 불가",
                    )
                )
        return reports

    # ── run 증거 판정 (WO#22) ─────────────────────────────────────────
    def judge_run_criteria(
        self,
        run_items: list[tuple[AcceptanceCriterion, RunEvidence]],
        result: str,
        spec: ProjectSpec,
    ) -> list[CheckReport]:
        """run 타입 기준들을 *실행 증거*로 한 번의 LLM 호출로 판정해 CheckReport 반환.

        파일 대신 RunEvidence(부팅/exit/stderr/trace)를 직렬화해 run-judge 프롬프트로
        적대 판정한다. 출력이 깨지거나 일부 ac가 빠지면 그 기준만 skipped — 절대 raise 안 함.
        RunEvidence는 각 CheckReport.run_evidence에 실어 감사한다.
        """
        if not run_items:
            return []

        system = Path(self.run_prompt_path).read_text(encoding="utf-8")
        user = self._build_run_user(run_items, result)
        raw = self.client.complete(system, user)

        by_id: dict[str, _JudgeVerdict] = {}
        try:
            parsed = parse_yaml_model(raw, _JudgeOutput)
        except ParseError:
            parsed = None  # 통째로 깨짐 → 전부 skipped.
        if parsed is not None:
            for v in parsed.verdicts:
                by_id[v.ac_id] = v

        reports: list[CheckReport] = []
        for ac, ev in run_items:
            v = by_id.get(ac.id)
            status = _norm_status(v.status) if v is not None else None
            if status in ("pass", "fail"):
                reports.append(
                    CheckReport(
                        ac_id=ac.id,
                        check_type=CheckType.run,
                        cmd=ac.check.cmd,
                        status=status,
                        exit_code=ev.exit_code,
                        detail=(v.reason if v is not None else None),
                        run_evidence=ev,
                    )
                )
            else:
                reports.append(
                    CheckReport(
                        ac_id=ac.id,
                        check_type=CheckType.run,
                        cmd=ac.check.cmd,
                        status="skipped",
                        exit_code=ev.exit_code,
                        detail="run judge 평가 불가",
                        run_evidence=ev,
                    )
                )
        return reports

    @staticmethod
    def _build_run_user(
        run_items: list[tuple[AcceptanceCriterion, RunEvidence]],
        result: str,
    ) -> str:
        parts: list[str] = ["# 평가할 기준 (criteria) + 각 기준의 실행 증거 (run evidence)"]
        for ac, ev in run_items:
            parts.append(f"\n## {ac.id}: {ac.desc}")
            parts.append(f"- cmd: {ac.check.cmd}")
            parts.append(f"- booted(정상 종료): {ev.booted}")
            parts.append(f"- exit_code: {ev.exit_code}")
            parts.append(f"- timed_out: {ev.timed_out}")
            parts.append(f"- duration_s: {ev.duration_s:.2f}")
            if ev.reason:
                parts.append(f"- reason: {ev.reason}")
            parts.append("- stderr (tail):")
            parts.append(f"```\n{(ev.stderr_tail or '').strip() or '(없음)'}\n```")
            parts.append("- behavior trace (stdout):")
            parts.append(f"```\n{(ev.trace or '').strip() or '(없음)'}\n```")

        parts.append("\n# executor 결과 요약 (result, self-report — 참고용)")
        parts.append((result or "").strip() or "(없음)")
        return "\n".join(parts)

    # ── 파일 수집 ─────────────────────────────────────────────────────
    def _collect_files(self) -> list[tuple[str, str]]:
        """workdir의 텍스트 파일을 (상대경로, 내용)로 수집. 바이너리/대용량/노이즈 제외.

        per-file·total 두 cap을 적용하고, 안정적 순서를 위해 경로 정렬한다.
        """
        root = Path(self.workdir)
        if not root.is_dir():
            return []

        out: list[tuple[str, str]] = []
        total = 0
        for p in sorted(root.rglob("*")):
            if not p.is_file():
                continue
            if any(part in _SKIP_DIRS for part in p.parts):
                continue
            try:
                if p.stat().st_size > self.max_file_bytes:
                    continue
                data = p.read_bytes()
            except OSError:
                continue
            if b"\x00" in data:  # 휴리스틱: 널바이트 있으면 바이너리로 간주
                continue
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                continue
            if total + len(data) > self.max_total_bytes:
                break
            total += len(data)
            out.append((str(p.relative_to(root)), text))
        return out

    # ── 프롬프트 본문 ─────────────────────────────────────────────────
    @staticmethod
    def _build_user(
        judge_acs: list[AcceptanceCriterion],
        result: str,
        files: list[tuple[str, str]],
    ) -> str:
        parts: list[str] = ["# 평가할 기준 (criteria)"]
        for ac in judge_acs:
            parts.append(f"- {ac.id}: {ac.desc}")

        parts.append("\n# executor 결과 요약 (result, self-report — 참고용)")
        parts.append((result or "").strip() or "(없음)")

        parts.append("\n# 산출 파일 (output files — 판정의 1차 근거)")
        if files:
            for path, content in files:
                parts.append(f"\n## {path}\n```\n{content}\n```")
        else:
            parts.append("(수집된 텍스트 파일 없음)")

        return "\n".join(parts)
