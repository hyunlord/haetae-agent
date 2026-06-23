"""CLI 엔트리 — `python -m haetae.run --order "..."`.

첫 실제 end-to-end: brain=CodexClient, gate=CheckRunner, executor=HumanRelayExecutor.
배선 로직은 run()으로 빼서 테스트 가능하게 두고, __main__은 인자 파싱 + 호출만 한다.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from haetae.executors import (
    CodexExecutor,
    HumanRelayExecutor,
    LocalAgentExecutor,
    Tier,
    builder_selftest,
    builder_smoke,
    tier_label,
)
from haetae.providers.launch_options import read_codex_config
from haetae.gate import CompositeGate
from haetae.heartbeat import HeartbeatWriter
from haetae.transcript import TranscriptWriter
from haetae.llm import CodexClient, LocalJudgeClient
from haetae.loop import Executor, Gate, run_loop
from haetae.llm import LLMClient
from haetae.metering import MeteredClient
from haetae.models import JudgeProfile, PlanState, ProjectSpec, State

# 기본 스킬 디렉토리 = 이 repo의 skills/ (src/haetae/run.py → parents[2] = repo 루트).
_DEFAULT_SKILLS_DIR = str(Path(__file__).resolve().parents[2] / "skills")
# 큐레이션 능력 레지스트리(WO#53 F.1) = 이 repo의 capabilities/. opt-in(--capabilities)일 때만 사용.
_DEFAULT_CAPABILITIES_DIR = str(Path(__file__).resolve().parents[2] / "capabilities")


def run(
    order: str,
    *,
    client: LLMClient,
    executor: Executor,
    gate: Gate,
    critic_client: LLMClient | None = None,
    max_iters: int = 30,
    decomp_critic: bool = True,
    decomp_retries: int = 1,
    or_alternatives: int = 1,
    max_tokens: int | None = None,
    unit_attempt_budget: int | None = None,
    unit_token_budget: int | None = None,
    state_path: str | Path | None = None,
    prompt_dir: str | Path | None = None,
    progress: Callable[[str], None] | None = None,
    max_parallel: int = 1,
    max_parallel_burst: int = 0,
    workdir: str | Path | None = None,
    executor_factory: Callable | None = None,
    gate_factory: Callable | None = None,
    unit_retries: int = 3,  # WO#108-C: 2→3 상향 — 어려운 유닛에 escalate 전 한 번 더 여유.
    tier_ladder: list[Tier] | None = None,
    auto_config_note: str | None = None,
    scaffold_client: LLMClient | None = None,
    install_deps: bool = True,
    skills_dir: str | Path | None = None,
    pricing: dict | None = None,
    capabilities_on: bool = False,
    capability_registry_path: str | Path | None = None,
    capability_allowlist: list[str] | None = None,
    capability_searcher=None,
    heartbeat=None,
    synth_context: str | None = None,
    resume_spec: ProjectSpec | None = None,
    resume_state: State | None = None,
    seeded: bool = False,
    reuse_manifest: dict | None = None,
    reuse: bool = True,
    judge_profile=None,
    shadow_sink=None,
) -> State:
    """주입된 brain/executor/gate로 루프를 한 번 완주하고 최종 State를 반환한다."""
    return run_loop(
        order,
        client,
        executor,
        gate,
        critic_client=critic_client,
        heartbeat=heartbeat,
        synth_context=synth_context,
        resume_spec=resume_spec,            # WO#91: 순수 재개 시 부모 spec(재합성 skip)
        resume_state=resume_state,          # WO#91: 부모 plan-state(done 시드/미완 재빌드)
        seeded=seeded,
        reuse_manifest=reuse_manifest,
        reuse=reuse,
        judge_profile=judge_profile,   # WO#171-C: 약-judge 정직 표기(read-only 메타)
        shadow_sink=shadow_sink,       # WO#171-shadow: 검증역전 누적기(opt-in, 적용 0)
        capabilities_on=capabilities_on,
        capability_registry_path=capability_registry_path,
        capability_allowlist=capability_allowlist,
        capability_searcher=capability_searcher,
        max_iters=max_iters,
        decomp_critic=decomp_critic,
        decomp_retries=decomp_retries,
        or_alternatives=or_alternatives,
        max_tokens=max_tokens,                      # WO#68 (B) — main()이 넘기는데 래퍼가 빠뜨렸던 구멍
        unit_attempt_budget=unit_attempt_budget,    # WO#68 (C)
        unit_token_budget=unit_token_budget,        # WO#68 (C, 토큰 기준)
        state_path=state_path,
        prompt_dir=prompt_dir,
        progress=progress,
        max_parallel=max_parallel,
        max_parallel_burst=max_parallel_burst,  # WO#110: disjoint burst(0=보수적 기본)
        workdir=workdir,
        executor_factory=executor_factory,
        gate_factory=gate_factory,
        unit_retries=unit_retries,
        tier_ladder=tier_ladder,
        auto_config_note=auto_config_note,
        scaffold_client=scaffold_client,
        install_deps=install_deps,
        skills_dir=skills_dir,
        pricing=pricing,
    )


def _load_pricing(path: str | None) -> dict | None:
    """가격표 JSON을 best-effort 로드: {model: [input_per_mtok, output_per_mtok]}.

    경로 없음 → None(usd 미계산). 로드/형식 실패도 None(계측이 run을 막지 않는다).
    값은 (input, output) 튜플로 정규화한다.
    """
    if not path:
        return None
    try:
        import json

        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        out: dict[str, tuple[float, float]] = {}
        for model, rate in raw.items():
            if isinstance(rate, (list, tuple)) and len(rate) == 2:
                out[model] = (float(rate[0]), float(rate[1]))
        return out or None
    except Exception as e:  # noqa: BLE001 — 가격표 로드 실패는 계측만 비활성(run 진행)
        print(f"… 가격표 로드 실패({path}): {e} — usd 미계산으로 진행", file=sys.stderr)
        return None


def parse_tier_ladder(
    spec: str | None, default_model: str | None, default_effort: str | None
) -> list[Tier]:
    """`--tier-ladder` 문자열을 Tier 사다리로 파싱한다(WO#64).

    형식: "model:effort,model:effort,..." (effort 생략 가능 → None). 예:
      "gpt-5-mini:medium,gpt-5.5:high,gpt-5.5:xhigh".
    미지정(None/빈)이면 **단일 tier**[(--model, --reasoning-effort)] = 기존 동작 그대로
    (후방호환 — 661 무회귀). 빈 칸/빈 모델("":effort, ":high")은 model=None으로 둔다.
    """
    s = (spec or "").strip()
    if not s:
        return [Tier(model=default_model, reasoning_effort=default_effort)]
    ladder: list[Tier] = []
    for raw in s.split(","):
        item = raw.strip()
        if not item:
            continue
        model, _, effort = item.partition(":")
        ladder.append(Tier(model=(model.strip() or None), reasoning_effort=(effort.strip() or None)))
    # 전부 빈 칸이면 단일 tier 폴백(파싱 실패가 빈 사다리=cap 오류로 안 새게).
    return ladder or [Tier(model=default_model, reasoning_effort=default_effort)]


# ──────────────────── WO#65: 제로-config auto 모드 (운영 knob 자동 해석) ────────────────────


@dataclass
class AutoConfig:
    """`--auto`가 해석한 *운영 knob* 묶음 + 투명성 노출 텍스트.

    **운영 knob만** 담는다(model·effort 사다리·critic·scaffold·parallel 등 — 가역·저비용).
    거버넌스 게이트(능력 채택 allowlist·capability-search 네트워크·executor 네트워크·bar)는
    여기 *전혀* 없다 — 그건 사람이 명시 opt-in해야 한다(자동 미활성). summary/warnings는
    사람이 "무엇이 자동 선택됐는지" 보도록 이벤트/state로 노출된다.
    """

    tier_ladder: list[Tier]
    critic_model: str | None          # 해석된 critic 모델(None=codex 기본 — critic은 여전히 ON)
    critic_on: bool                   # auto는 항상 True(critic 절대 OFF 아님 — decomp critic/OR 살림)
    critic_independent: bool          # 빌더 model과 분리됐는지(단일 provider면 보통 False → 경고)
    warnings: list[str] = field(default_factory=list)
    summary: str = ""


def resolve_auto_config(args, config_path: str | Path | None = None) -> AutoConfig:
    """`--auto`: *미설정* 운영 옵션을 sensible 기본으로 채운다. 명시 플래그가 auto를 이긴다.

    출처/규칙:
      - tier 사다리(#64): `--tier-ladder` 명시 > `--reasoning-effort` 명시(단일 tier) >
        자동 effort 사다리(medium→high→xhigh). 빌더 model = `--model` 또는 codex config
        pre-fill(#45, 엔진-free read). 멀티-provider epic에서 모델별 사다리로 확장.
      - critic-model: `--critic-model` 명시 우선. 미지정이면 단일 codex 세계라 빌더 model로
        설정(critic ON 유지) + **독립성 경고**(조용한 독립성 붕괴 금지). critic은 절대 OFF 아님.
      - scaffold/skills/parallel/timeout/iters: argparse 기본이 이미 sensible(auto가 새로
        바꿀 것 없음) — summary에 가시화만.
    **거버넌스(능력 채택·네트워크·bar)는 절대 자동으로 안 켠다** — 명시 opt-in 전용.
    launch_options는 엔진-free 메타데이터만 — 사다리 조립(executors.Tier)은 여기(run.py)서.
    """
    cfg = read_codex_config(config_path)  # best-effort, 엔진-free, 실패 시 {}
    explicit_model = (args.model or "").strip() if args.model else ""
    builder_model = explicit_model or cfg.get("model")  # None=codex 기본(최신) 자동
    warnings: list[str] = []

    # 1) tier 사다리 — 명시 우선(오버라이드), 아니면 자동 effort 사다리.
    if args.tier_ladder:
        ladder = parse_tier_ladder(args.tier_ladder, builder_model, args.reasoning_effort)
    elif args.reasoning_effort:
        ladder = [Tier(builder_model, args.reasoning_effort)]  # 명시 effort = 단일 tier
    else:
        ladder = [Tier(builder_model, e) for e in ("medium", "high", "xhigh")]

    # 2) critic — 절대 OFF 아님. 명시 우선; 미지정이면 빌더 model(단일 provider) + 독립성 경고.
    critic_model = args.critic_model if args.critic_model else builder_model
    critic_independent = bool(critic_model and builder_model and critic_model != builder_model)
    if not critic_independent:
        warnings.append(
            "critic-model이 빌더 model과 동일(단일 provider) — 적대 독립성 약화. "
            "멀티-provider에서 분리 가능."
        )

    # 3) 투명성 summary — 사람이 자동 선택을 한눈에.
    ladder_str = ",".join(tier_label(t) for t in ladder)
    critic_str = (critic_model or "codex-default") + (
        "(indep)" if critic_independent else "(non-indep)"
    )
    summary = (
        f"auto-config: ladder=[{ladder_str}] · critic={critic_str} · "
        f"scaffold={'on' if args.scaffold else 'off'} · parallel={args.max_parallel} · "
        f"skills={'on' if args.skills else 'off'} · "
        f"governance=manual(capabilities/network 자동 미활성)"
    )
    return AutoConfig(
        tier_ladder=ladder,
        critic_model=critic_model,
        critic_on=True,
        critic_independent=critic_independent,
        warnings=warnings,
        summary=summary,
    )


# ──────────────── WO#171: 완전-로컬 자급 모드 라우팅 (codex 경로 보존·fully-local 프리셋) ────────────────
#
# 새 thesis(강 모델 0): brain·builder·judge·critic을 전부 약 로컬 모델로 굴릴 수 있게 *역할별 실행자*를
# 라우팅한다. **기본값은 전부 codex/human = 기존 동작 보존(1246 무회귀)** — codex 경로(CodexExecutor·
# ALLOWED_SANDBOXES)는 *코드로 보존*되고 opt-out다. `--fully-local`은 새 thesis의 한-플래그 진입점
# (네 역할 전부 local). shadow는 opt-in(기본 None). 순수 함수라 테스트가 배선을 *값으로* 단언한다.


@dataclass
class ExecutorWiring:
    """역할별 실행자 + shadow 해석(--fully-local 프리셋 반영, WO#171). 명시 플래그가 프리셋을 이긴다.

    brain:        합성/replan/scaffold LLM 실행자("codex"|"local").
    builder:      유닛 빌더 executor("human"|"codex"|"local").
    judge:        gate judge/run-judge 실행자("codex"|"local").
    critic:       spec/decomp critic 실행자("codex"|"local").
    shadow_judge: shadow 비교 judge("codex"|None — None=shadow OFF=100% 로컬·codex 흔적 0).
    """

    brain: str
    builder: str
    judge: str
    critic: str
    shadow_judge: str | None

    @property
    def fully_local(self) -> bool:
        return (
            self.brain == "local" and self.builder == "local"
            and self.judge == "local" and self.critic == "local"
        )

    @property
    def uses_codex(self) -> bool:
        """이 배선이 codex를 *하나라도* 쓰나(shadow 포함). shadow OFF·완전-로컬이면 False(흔적 0)."""
        return (
            "codex" in (self.brain, self.builder, self.judge, self.critic)
            or self.shadow_judge == "codex"
        )


def resolve_executor_wiring(args) -> ExecutorWiring:
    """역할별 실행자를 해석한다(WO#171). **명시 플래그 > --fully-local(local) > 기본(codex/human)**.

    --fully-local은 brain/judge/critic을 local로, builder(--executor)도 (명시 안 했으면) local로 민다.
    기본값(미지정·fully-local OFF)은 전부 codex/human = 기존 동작 그대로(1246 무회귀·codex 경로 보존).
    `--executor codex`처럼 명시한 builder는 fully-local에서도 존중(human은 fully-local서 local로 승격 —
    완전-로컬은 사람 릴레이를 원치 않으므로). 순수 함수(테스트가 값으로 단언).
    """
    fl = bool(getattr(args, "fully_local", False))
    brain = args.brain_executor or ("local" if fl else "codex")
    judge = args.judge_executor or ("local" if fl else "codex")
    critic = args.critic_executor or ("local" if fl else "codex")
    builder = args.executor
    if fl and builder == "human":
        builder = "local"  # 완전-로컬: 미지정(기본 human) → local(사람 릴레이 비활성)
    return ExecutorWiring(
        brain=brain, builder=builder, judge=judge, critic=critic,
        shadow_judge=args.shadow_judge,
    )


def _make_role_client(
    kind: str,
    *,
    role: str,
    model: str | None,
    local_endpoint: str,
    local_model: str,
    idle_timeout: float | None,
    max_duration: float | None,
    stall_retries: int,
    local_timeout: float,
    heartbeat=None,
    transcript=None,
    default_call_kind: str | None = None,
) -> LLMClient:
    """역할(brain/judge/critic)의 LLMClient를 실행자 종류로 만든다(WO#171): codex→CodexClient, local→LocalJudgeClient.

    **codex 경로 보존**: kind=="codex"면 기존 CodexClient 그대로(불변). kind=="local"이면 약-judge
    클라이언트(LocalJudgeClient) — *같은 LLMClient judge 슬롯*에 꽂힌다(판정 로직 불변, CodexClient 동격).
    빌더(executor)는 이 함수가 아니라 LocalAgentExecutor/CodexExecutor 경로로 만들어진다(빌더≠judge 분리).
    """
    if kind == "local":
        return LocalJudgeClient(
            endpoint=local_endpoint, model=local_model, role=role,
            timeout=local_timeout, heartbeat=heartbeat, transcript=transcript,
            default_call_kind=default_call_kind or role,
        )
    return CodexClient(
        model=model, idle_timeout=idle_timeout, max_duration=max_duration,
        stall_retries=stall_retries, heartbeat=heartbeat, transcript=transcript,
        default_call_kind=default_call_kind,
    )


def _judge_profile_note(wiring: ExecutorWiring) -> str:
    """약-judge 런 정직 한 줄(WO#171-C). weak_judge면 적대성 이전(인스턴스 분리+기계 게이트)을 명시."""
    if wiring.judge == "local":
        base = (
            "약-judge 런(judge=local): 강 독립 judge와 무결성 보장 다름 — 적대성=빌더≠judge "
            "인스턴스 분리 + 기계적 게이트(결정적 사실 주력). 바 불완화·gate 판정 로직 불변."
        )
    else:
        base = "강-judge 런(judge=codex): 독립 모델 적대 판정."
    if wiring.shadow_judge:
        base += f" shadow={wiring.shadow_judge}(약=적용·강=기록만, 검증역전 측정·적용 0)."
    return base


# ──────────────────── WO#58: 이어가기(②a) — 부모 해석·시딩·계보 ────────────────────


class ContinuationError(RuntimeError):
    """이어가기 부모 해석/시딩 실패. **명시적 요청이므로 조용한 폴백 아님** — 명확히 멈춘다."""


def resolve_parent_dir(continue_from: str, runs_dir: str | Path = "runs") -> Path:
    """`--continue-from` 값을 부모 run 디렉터리로 해석.

    값이 (a) state.yaml을 가진 디렉터리면 그대로, (b) 아니면 runs_dir/<run-id>로 시도.
    둘 다 아니면 ContinuationError(명확한 에러). 부모는 명시적 입력이라 폴백 없음.
    """
    cand = Path(continue_from)
    if (cand / "state.yaml").exists():
        return cand
    by_id = Path(runs_dir) / continue_from
    if (by_id / "state.yaml").exists():
        return by_id
    raise ContinuationError(
        f"부모 run을 찾을 수 없음: {continue_from!r} "
        f"(디렉터리도 runs-dir({runs_dir})/<run-id>도 state.yaml 없음)"
    )


def _git_tracked_files(repo: Path) -> list[str] | None:
    """부모 workdir이 git repo면 *추적 파일*(node_modules 등 gitignore 제외) 목록, 아니면 None."""
    try:
        top = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=30,
        )
        if top.returncode != 0:
            return None
        ls = subprocess.run(
            ["git", "-C", str(repo), "ls-files"],
            capture_output=True, text=True, timeout=60,
        )
        if ls.returncode != 0:
            return None
        return [ln for ln in ls.stdout.splitlines() if ln.strip()]
    except (OSError, subprocess.SubprocessError):
        return None


# git 미사용 폴백 복사 시 제외할 노이즈(시딩하면 안 되는 것들).
_SEED_EXCLUDE = {".git", "node_modules", ".venv", "venv", "__pycache__",
                 ".pytest_cache", "dist", "build", ".haetae-worktrees"}


def seed_workdir_from_parent(parent_work: Path, new_work: Path) -> int:
    """부모 최종 트리를 새 workdir에 시딩(추적 파일만, node_modules 등 제외). 복사한 파일 수 반환.

    1순위: git ls-files(부모 main HEAD의 *committed/추적* 파일) — gitignore 자동 존중.
    폴백: git 아니면 _SEED_EXCLUDE 제외 재귀 복사. 새 worktree refs/히스토리는 안 끌고 옴
    (ensure_repo가 새 baseline 커밋을 판다). 시딩 실패는 ContinuationError(이어가기 핵심).
    """
    import shutil

    parent_work = Path(parent_work)
    new_work = Path(new_work)
    if not parent_work.is_dir():
        raise ContinuationError(f"부모 workdir 없음: {parent_work}")
    new_work.mkdir(parents=True, exist_ok=True)

    tracked = _git_tracked_files(parent_work)
    copied = 0
    if tracked is not None:
        for rel in tracked:
            src = parent_work / rel
            if not src.is_file():
                continue  # 삭제 대기/서브모듈 등은 스킵
            dst = new_work / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied += 1
        return copied
    # 폴백: 비-git 부모 — 노이즈 제외 재귀 복사.
    for src in parent_work.rglob("*"):
        if any(part in _SEED_EXCLUDE for part in src.relative_to(parent_work).parts):
            continue
        if src.is_file():
            dst = new_work / src.relative_to(parent_work)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied += 1
    return copied


def _resolve_fix_ref(fix_ref_arg: str | None) -> str | None:
    """부모→자식 사이 적용된 fix 참조 해소(WO#167): 인자 우선, 없으면 현재 HEAD commit(짧은 해시).

    best-effort — git 없음/repo 아님/실패 시 None(계보 기록은 run을 막지 않는다). director-side 메타.
    """
    # blank 인자(None/""/공백)는 '미지정'으로 보고 HEAD로 폴백 — 일관(arg OR HEAD).
    arg = (fix_ref_arg or "").strip()
    if arg:
        return arg
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        ref = (out.stdout or "").strip()
        return ref or None
    except Exception:  # noqa: BLE001 — git 부재/실패는 무시(fix_ref 미기록)
        return None


def _write_lineage(
    state_path: str | Path, parent_run_id: str, fix_ref: str | None = None
) -> None:
    """계보(parent_run_id + fix_ref)를 state.yaml 옆 lineage.json 사이드카에 best-effort 기록(WO#167).

    WO#167: 기존 `{parent_run_id}`에 fix_ref(부모→자식 사이 적용된 commit/WO)를 더한다 — 다-런 arc
    (런→fix→이어가기) 추적용 *read-only 메타*. parent_run_id 키 보존(구 read·기존 테스트 무영향).
    verdict는 안 적는다(각 run의 state.status가 단일 출처 — 대시보드 트리가 거기서 읽음). 판정 아님.
    """
    try:
        import json
        from haetae.models import Lineage
        p = Path(state_path).parent / "lineage.json"
        rec = Lineage(parent_run_id=parent_run_id, fix_ref=fix_ref).model_dump(mode="json")
        p.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001 — 계보 기록 실패가 run을 죽이지 않는다(best-effort 영속화)
        pass


def _write_cli_meta(
    state_path: str | Path, order: str, *, status: str = "running",
    workdir: str | Path | None = None,
) -> None:
    """CLI run의 원 주문을 state.yaml 옆 meta.json 사이드카에 best-effort 기록 (WO#75).

    웹 런처(RunManager)와 *동일 형식*({id, order, started_at, status}) → 대시보드 `/api/runs`
    (meta.json 스캔)·#57 주문 뷰가 CLI run도 커버한다. **이미 meta.json이 있으면 안 덮는다** —
    런처가 spawn한 경우 옵션/argv/계보가 든 더 풍부한 meta를 잃지 않기 위함(추가형·비파괴).

    WO#108-B: workdir(코드 위치)를 *절대경로*로 기록 → 이어가기(continue-from) 시드가 부모
    코드 위치를 자동 해소한다(부모가 별도 --workdir여도 수동 symlink 불필요). 추가형 키.
    """
    try:
        import json
        from datetime import datetime, timezone
        d = Path(state_path).parent
        p = d / "meta.json"
        if p.exists():
            return  # 런처가 이미 더 풍부한 meta를 씀 — 클로버 금지
        started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        rec = {"id": d.name, "order": order, "started_at": started_at, "status": status}
        if workdir is not None:
            rec["workdir"] = str(Path(workdir).resolve())  # 절대경로(다른 cwd서 재개해도 유효)
        p.write_text(
            json.dumps(rec, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001 — 사이드카 기록 실패가 run을 죽이지 않는다(best-effort)
        pass


def _resolve_seed_src(parent_dir: Path) -> Path:
    """이어가기 시드의 *부모 코드 위치*를 자동 해소 (WO#108-B). 수동 symlink 불필요.

    후보를 순서대로: (1) 부모 meta.json의 `workdir`(#75·#108-B, 존재하면) →
    (2) `<run-dir>/work` → (3) `<run-dir>`. 첫 *존재하는 디렉토리*를 반환. 아무것도 없으면
    `<run-dir>`(seed_workdir_from_parent가 디렉토리 부재 시 명확 ContinuationError를 낸다 — 크래시 0).
    """
    try:
        import json
        meta = json.loads((parent_dir / "meta.json").read_text(encoding="utf-8"))
        wd = meta.get("workdir")
        if wd and Path(wd).is_dir():
            return Path(wd)
    except Exception:  # noqa: BLE001 — meta 없음/깨짐 → 후보 폴백(best-effort)
        pass
    work = parent_dir / "work"
    if work.is_dir():
        return work
    return parent_dir


def build_reuse_manifest(parent_spec, parent_state) -> dict:
    """부모 *검증된 done 유닛*의 바 지문 manifest (WO#71 ②b 깊은 증분).

    검증 가능 done만 — 상태 done + (코드가 시딩된 main에 존재; 시딩은 전체 workdir 복사라 충족).
    각 항목 parent_unit_id → {criteria, scope}(unit_bar_signature). 새 run의 reuse_of 유닛이
    이 manifest와 *직접 대조*돼 criteria·scope 불변일 때만 재사용된다(anti-erosion 가드).
    부모 spec/state 없으면(옛 부모·crashed) 빈 dict → 재사용 0(전부 정상 빌드, graceful).
    """
    from haetae.intake import unit_bar_signature

    if parent_spec is None or parent_state is None:
        return {}
    done = {p.unit for p in parent_state.plan if p.state == PlanState.done}
    return {uid: unit_bar_signature(parent_spec, uid) for uid in done}


def _load_parent_spec(parent_dir: Path) -> ProjectSpec | None:
    """부모 spec.yaml 사이드카 로드(없거나 깨졌으면 None — graceful degrade)."""
    sp = parent_dir / "spec.yaml"
    if not sp.exists():
        return None
    try:
        return ProjectSpec.from_yaml(sp)
    except Exception:  # noqa: BLE001 — 깨진 사이드카는 degrade(None)
        return None


def _load_parent_state(parent_dir: Path) -> State | None:
    """부모 state.yaml 로드(없거나 깨졌으면 None — graceful degrade)."""
    try:
        return State.from_yaml(parent_dir / "state.yaml")
    except Exception:  # noqa: BLE001
        return None


def _load_parent_order(parent_dir: Path) -> str | None:
    """부모 meta.json의 원 주문(order) 로드(없으면 None). 런처/CLI가 verbatim으로 쓴 값."""
    try:
        import json
        meta = json.loads((parent_dir / "meta.json").read_text(encoding="utf-8"))
        return meta.get("order")
    except Exception:  # noqa: BLE001
        return None


def load_continuation(continue_from: str, runs_dir: str | Path, new_workdir: str | Path):
    """부모 해석 + 새 workdir 시딩 + 증분 context + 재사용 manifest 구성.

    반환 (synth_context, parent_dir, seeded_n, reuse_manifest). 부모 못 찾음/시딩 실패는
    ContinuationError(명확). spec.yaml 없으면 meta order로 degrade. judge·critic엔 절대 안 가는
    context(합성기 전용)만 만든다 — 호출부가 run(synth_context=)로 전달. reuse_manifest는 부모
    검증 done 유닛의 바 지문(WO#71) — 새 run의 reuse_of 검증·done 시드용(없으면 빈 dict).
    """
    from haetae.intake import build_continuation_context

    parent_dir = resolve_parent_dir(continue_from, runs_dir)
    # WO#108-B: 부모 코드 위치 자동 해소(meta.json workdir → <run-dir>/work → <run-dir>) — 부모가
    # 별도 --workdir를 썼어도 수동 symlink 없이 시딩원을 찾는다. 못 찾으면 seed가 명확 에러.
    seed_src = _resolve_seed_src(parent_dir)
    seeded_n = seed_workdir_from_parent(seed_src, Path(new_workdir))

    parent_spec = _load_parent_spec(parent_dir)
    parent_state = _load_parent_state(parent_dir)
    parent_order = _load_parent_order(parent_dir)

    ctx = build_continuation_context(parent_spec, parent_state, parent_order=parent_order)
    reuse_manifest = build_reuse_manifest(parent_spec, parent_state)
    return ctx, parent_dir, seeded_n, reuse_manifest


# ──────────────────── WO#91: 순수 재개(부모 plan/criteria 보존) vs 증분 연속 분기 ────────────────────


@dataclass
class ResumePlan:
    """`--continue-from` 재개 페이로드. mode로 *순수 재개* vs *증분 연속*을 구분한다(WO#91).

    - "pure_resume": 같은 order로 미완 run을 마저 끝낸다 — 부모 plan/criteria를 *보존*(부모
      spec.yaml/state.yaml 로드, 재합성 skip). resume_spec/resume_state를 채운다. 부모 done 유닛은
      보존(시드)되고 미완만 재빌드된다(anti-erosion by construction — 바가 변할 여지 없음).
    - "incremental": 부모 + 새 order/요구 — 기존대로 증분 재합성(새 작업이라 합성 필요, 무변경).
      synth_context/reuse_manifest를 채운다(기존 #58/#71 경로 그대로, back-compat).
    """

    mode: str  # "pure_resume" | "incremental"
    parent_dir: Path
    seeded_n: int
    synth_context: str | None = None
    reuse_manifest: dict | None = None
    resume_spec: ProjectSpec | None = None
    resume_state: State | None = None


def is_pure_resume(
    new_order: str | None,
    parent_order: str | None,
    parent_spec: ProjectSpec | None,
    *,
    resynthesize: bool = False,
) -> bool:
    """순수 재개 판별(WO#91): `--continue-from`인데 *새 요구 없음*(부모와 같은 order)이면 True.

    부모 order 출처: meta.json order(런처/CLI가 쓴 verbatim) 우선, 없으면 부모 spec.order_raw로
    폴백. 새 order가 부모와 *정확히* 같을 때만 순수 재개로 분기한다(요구가 바뀌면 증분 합성이
    필요하므로 False). 보수적: 부모 order/spec를 알 수 없으면 False=증분(back-compat 안전).
    `--resynthesize`(opt-in)면 순수 재개여도 항상 False(강제 재합성 — 부모 plan이 나쁠 때 escape).
    """
    if resynthesize or parent_spec is None:
        return False
    ref = parent_order if parent_order is not None else parent_spec.order_raw
    if not ref:
        return False
    return (new_order or "").strip() == ref.strip()


def load_resume(
    continue_from: str,
    runs_dir: str | Path,
    new_workdir: str | Path,
    *,
    order: str,
    resynthesize: bool = False,
) -> ResumePlan:
    """부모 해석 + workdir 시딩 후 *순수 재개 vs 증분 연속*을 판별해 페이로드를 만든다(WO#91).

    순수 재개(같은 order · 부모 spec.yaml 존재 · not --resynthesize): 부모 spec/state를 *직접
    로드*해 재합성을 skip한다 → done 유닛 보존·미완만 *부모 criteria 그대로* 재빌드(rebuild-all
    회피, #71 reuse가 매칭할 것도 없이 보존). 그 외(새 order/spec.yaml 부재/--resynthesize)는
    기존 증분 합성 context + 재사용 manifest(무변경, back-compat). 부모 해석/시딩 실패는
    ContinuationError(명확 — 부모는 명시 입력). 부모 spec.yaml 손상/부재는 graceful 증분 폴백.
    """
    from haetae.intake import build_continuation_context

    parent_dir = resolve_parent_dir(continue_from, runs_dir)
    parent_work = parent_dir / "work"
    seed_src = parent_work if parent_work.is_dir() else parent_dir
    seeded_n = seed_workdir_from_parent(seed_src, Path(new_workdir))

    parent_spec = _load_parent_spec(parent_dir)
    parent_state = _load_parent_state(parent_dir)
    parent_order = _load_parent_order(parent_dir)

    if is_pure_resume(order, parent_order, parent_spec, resynthesize=resynthesize):
        # 순수 재개: 부모 plan/criteria 보존(재합성 skip). 미완만 부모 criteria로 재빌드.
        return ResumePlan(
            mode="pure_resume", parent_dir=parent_dir, seeded_n=seeded_n,
            resume_spec=parent_spec, resume_state=parent_state,
        )
    # 증분 연속(새 order/spec.yaml 부재/--resynthesize) — 기존 경로 그대로(무변경).
    ctx = build_continuation_context(parent_spec, parent_state, parent_order=parent_order)
    reuse_manifest = build_reuse_manifest(parent_spec, parent_state)
    return ResumePlan(
        mode="incremental", parent_dir=parent_dir, seeded_n=seeded_n,
        synth_context=ctx, reuse_manifest=reuse_manifest,
    )


def format_summary(state: State) -> str:
    """사람이 읽기 좋은 최종 State 요약."""
    lines = [
        "── haetae 루프 종료 ──",
        f"status            : {state.status.value}",
        f"events            : {len(state.events)}",
        "plan              : "
        + (", ".join(f"{p.unit}={p.state.value}" for p in state.plan) or "(없음)"),
    ]
    if state.pending_escalations:
        lines.append(f"pending_escalations: {len(state.pending_escalations)}")
        for e in state.pending_escalations:
            lines.append(f"  - {e}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="haetae.run",
        description="order 한 줄에서 시작해 haetae 루프를 돈다 (사람이 executor).",
    )
    parser.add_argument("--order", required=True, help="주문 원문")
    parser.add_argument(
        "--auto",
        action="store_true",
        default=False,
        help=(
            "제로-config auto 모드(기본 OFF). order만으로 *미설정* 운영 옵션을 자동 해석한다: "
            "기본 tier 사다리(effort medium→high→xhigh, #64)·자동 critic-model(독립 시도, "
            "불가하면 경고)·scaffold/skills on·sensible parallel/timeout/iters. **명시 플래그가 "
            "auto를 오버라이드**. **거버넌스(능력 채택·capability-search 네트워크·executor "
            "네트워크·bar)는 자동으로 안 켠다(사람 게이트 유지)**. 해석된 config는 이벤트/state에 노출."
        ),
    )
    parser.add_argument(
        "--workdir", default=".", help="check 실행 + codex executor의 cwd (gate/executor 공유, 기본: .)"
    )
    parser.add_argument("--model", default=None, help="codex 모델 override (기본: codex 설정)")
    parser.add_argument(
        "--reasoning-effort",
        choices=["minimal", "low", "medium", "high", "xhigh"],
        default=None,
        help=(
            "codex executor 추론 강도(minimal..xhigh). 미설정(기본)이면 플래그 미부착 → "
            "codex 기본(medium) 그대로(기존 동작 불변). xhigh=거친 동선 frontier 레버."
        ),
    )
    parser.add_argument(
        "--judge-model",
        default=None,
        help="judge 전용 codex 모델 (executor --model과 다르게 줘 독립성 확보; 기본: codex 설정)",
    )
    parser.add_argument(
        "--critic-model",
        default=None,
        help=(
            "spec critic 전용 codex 모델 (주면 적대적 spec 비평 ON — 다른 모델 권장 = 독립성). "
            "없으면 critic OFF(추가 비용 0, 기존 동작 불변)"
        ),
    )
    # ── WO#171: 완전-로컬 자급 모드(새 thesis: 강 모델 0) — 역할별 실행자 라우팅 + shadow 관측 ──
    parser.add_argument(
        "--fully-local",
        action="store_true",
        default=False,
        help=(
            "완전-로컬 자급 모드(새 thesis: 강 모델 0). brain·builder·judge·critic을 *전부* 약 로컬 "
            "모델로 라우팅하는 한-플래그 프리셋(brain/judge/critic-executor=local + executor=local). "
            "**기본 OFF = 기존 codex 경로 보존(불변·opt-out)**. 명시 역할 플래그가 프리셋을 오버라이드. "
            "shadow OFF(기본)면 codex 흔적 0(thesis 순수). 적대성=빌더≠judge 인스턴스 분리 + 기계적 "
            "게이트(바 불완화 아님 — 같은 gate에 약 모델만 꽂음)."
        ),
    )
    parser.add_argument(
        "--brain-executor", choices=["codex", "local"], default=None,
        help="합성/replan/scaffold LLM 실행자(기본: codex; --fully-local이면 local). codex 경로 보존.",
    )
    parser.add_argument(
        "--judge-executor", choices=["codex", "local"], default=None,
        help=(
            "gate judge/run-judge 실행자(기본: codex; --fully-local이면 local). local=약-judge"
            "(LocalJudgeClient — 빌더와 다른 인스턴스/역할/시드, 적대 프롬프트 유지). **gate 판정 로직·"
            "바 불변** — 약 모델을 *같은 judge 슬롯*에 꽂을 뿐. 약함은 --shadow-judge로 측정."
        ),
    )
    parser.add_argument(
        "--critic-executor", choices=["codex", "local"], default=None,
        help="spec/decomp critic 실행자(기본: codex; --fully-local이면 local). local이면 critic ON.",
    )
    parser.add_argument(
        "--shadow-judge", choices=["codex"], default=None,
        help=(
            "shadow 비교 관측(opt-in·기본 OFF·적용 0). 약 judge가 verdict 권위로 *적용*되고 codex가 "
            "*같은 산출물*을 shadow 판정해 **나란히 기록만**(검증역전=약pass·강fail 누적) → 약 self-judge가 "
            "어디서 봐주는지 *측정*. OFF면 codex 흔적 0. shadow=관측이지 적용 아님(verdict 권위는 약 judge)."
        ),
    )
    parser.add_argument(
        "--decomp-critic",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "분해 critic at replan (기본 on, Phase C = LEAP LLM 리뷰어). replan이 낸 매 work "
            "order의 *진전성*을 독립 critic(critic-model)이 판정 → 무진전/재진술이면 reject·재계획. "
            "--no-decomp-critic으로 끔. **--critic-model이 있어야 동작**(없으면 자동 OFF). "
            "verifier-side·soft·best-effort(실패 시 진행)."
        ),
    )
    parser.add_argument(
        "--decomp-retries",
        type=int,
        default=1,
        help=(
            "분해 critic이 weak(무진전) 판정 시 재계획하는 최대 횟수 (기본 1). "
            "소진 후에도 weak면 *진행*(데드락 금지)하고 critique를 state에 기록."
        ),
    )
    parser.add_argument(
        "--or-alternatives",
        type=int,
        default=1,
        help=(
            "OR-node 대안(기본 1, Phase D = LEAP AND-OR DAG). gate(유닛/통합)가 정상 재시도까지 "
            "소진하고도 실패하면 *같은 criteria를 둔 채* 근본적으로 다른 접근으로 갈아타 백트래킹·재시도. "
            "대안 소진 시 escalate(시도한 접근 첨부). 0이면 기존 동작(즉시 escalate, 후방호환). 병렬(>1) 경로."
        ),
    )
    parser.add_argument(
        "--executor",
        choices=["human", "codex", "local"],
        default="human",
        help=(
            "실행자 (기본: human=사람 릴레이). codex=자율 쓰기 실행(opt-in). "
            "local=약한 로컬 모델 빌더(OpenAI 호환 엔드포인트, #136/#137; **빌더 전용**·판정 무접촉)"
        ),
    )
    parser.add_argument(
        "--local-endpoint",
        default="http://100.70.109.50:8089/v1",
        help="--executor local의 OpenAI 호환 베이스 URL (기본: GB10 llama.cpp #136)",
    )
    parser.add_argument(
        "--local-model",
        default="qwen2.5-coder:7b",
        help="--executor local의 서빙 모델명 (기본: qwen2.5-coder:7b)",
    )
    parser.add_argument(
        "--local-max-turns",
        type=int,
        default=3,
        help="--executor local 에이전틱 루프 턴 상한 (기본 3; 28 t/s 경제성·#134 단발 선호)",
    )
    parser.add_argument(
        "--local-timeout",
        type=float,
        default=300.0,
        help=(
            "--executor local 모델 스트림 **idle-timeout(초)** — 토큰 간 최대 무응답(#54). "
            "스트리밍이라 느리지만-진행 생성은 (총 길이 무관) 완주하고 진짜 stall만 중단한다. "
            "기본 300(로드-박스 내성; #149: 14 t/s 로드-박스서 180s 총-캡이 편집 블록 전 truncate→스텁)."
        ),
    )
    parser.add_argument(
        "--local-smoke",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "--executor local 빌더-측 자기검사 ON(기본). (1) 구조적 스모크(컴파일+디스커버리 "
            "findability, #139/#141) 통과 후 (2) *정밀 자기-테스트*(자기 유닛 테스트 실행→실패 "
            "detail 주입→타겟 수정→green, #144). --no-local-smoke로 둘 다 끔. "
            "**판정 아님** — 정답/행동/완결/통합은 독립 적대 gate(불변)."
        ),
    )
    parser.add_argument("--state-path", default=None, help="최종 State를 저장할 YAML 경로")
    parser.add_argument(
        "--continue-from",
        default=None,
        metavar="RUN",
        help=(
            "이어가기(②a): 완료된 부모 run 위에서 *증분*으로 새 run을 돈다. 값은 부모 run "
            "디렉터리 경로 또는 --runs-dir 아래 run-id. 부모 최종 코드를 workdir에 시딩 + "
            "scaffold 스킵 + 부모 spec/완료요약을 합성기 context로 주입(delta 계획). "
            "judge엔 부모 context 무주입(적대 분리)·기준 약화 금지(anti-erosion)."
        ),
    )
    parser.add_argument(
        "--runs-dir",
        default="runs",
        help="--continue-from을 run-id로 줄 때 부모를 찾을 베이스 디렉터리 (기본 runs/).",
    )
    parser.add_argument(
        "--no-reuse",
        action="store_true",
        default=False,
        help=(
            "이어가기(②b) 시 검증된 부모 유닛 재사용을 끄고 *전부 재빌드*한다(escape). 기본은 "
            "ON(continue-from에서만 동작) — 부모서 done이고 acceptance_criteria·scope가 불변인 "
            "유닛을 done으로 시드해 재빌드를 생략한다(토큰 절약). 바가 바뀐 유닛은 재사용 안 됨 "
            "(재빌드+재gate, anti-erosion). 통합 gate는 재사용 run서도 항상 최종 결과에 실행. "
            "**증분 연속(새 order)에만 적용** — 순수 재개(같은 order)서 전부 재빌드하려면 --resynthesize."
        ),
    )
    parser.add_argument(
        "--resynthesize",
        action="store_true",
        default=False,
        help=(
            "순수 재개(WO#91)서도 *강제 재합성*한다(opt-in escape, 기본 OFF). 기본은 --continue-from"
            "에 *부모와 같은 order*를 주면 순수 재개로 분기해 부모 plan/criteria를 보존(재합성 skip, "
            "done 유닛 시드·미완만 재빌드)한다. 부모 plan 자체가 나빠 멈춘 경우 이 플래그로 부모 "
            "spec을 버리고 증분 재합성 경로로 돌린다. (새 order면 어차피 증분 연속 — 이 플래그 불필요.)"
        ),
    )
    parser.add_argument("--max-iters", type=int, default=30, help="최대 루프 횟수 (기본 30)")
    # ── WO#68 비용 거버넌스(전부 opt-in, 미지정=무제한·기존 동작 불변) ──
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=None,
        metavar="N",
        help=(
            "전역 예산 cap(B): 누적 토큰이 N 초과면 다음 호출 전 clean stop(외부 크레딧 컷오프 "
            "전에 의도적). 미지정이면 무제한(기존 동작). 충전/상한 조정 후 --continue-from으로 재개."
        ),
    )
    parser.add_argument(
        "--unit-attempt-budget",
        type=int,
        default=None,
        metavar="N",
        help=(
            "유닛 수렴 ceiling(C): 한 유닛의 *누적* 재dispatch(재시도+OR 대안+통합OR 층 합산)가 "
            "N 도달하고도 미통과면 그 유닛을 사람에게 escalate(다음 OR로 안 던짐). 바는 자동으로 "
            "안 낮춘다(anti-erosion) — 사람이 governed로 결정. 미지정이면 층별 bound만(기존)."
        ),
    )
    parser.add_argument(
        "--unit-token-budget",
        type=int,
        default=None,
        metavar="T",
        help=(
            "유닛 수렴 ceiling(C, 토큰 기준): 한 유닛의 누적 귀속 토큰이 T 초과하고 미통과면 "
            "사람에게 escalate(바 자동 미완화). --unit-attempt-budget과 OR로 작동. 미지정=off."
        ),
    )
    parser.add_argument(
        "--unit-retries",
        type=int,
        default=3,
        help=(
            "병렬 경로: 유닛 gate 실패/머지 충돌 시 그 유닛 재dispatch 최대 횟수 (기본 3, WO#108-C). "
            "소진 후 escalate. (LLM 출력 재시도 replan_retries와는 별개.)"
        ),
    )
    parser.add_argument(
        "--tier-ladder",
        default=None,
        metavar="TIERS",
        help=(
            "반응형 tier 사다리(opt-in, 병렬 경로). 형식 'model:effort,...' "
            "(예: 'gpt-5-mini:medium,gpt-5.5:high,gpt-5.5:xhigh'). 유닛은 싼 tier로 *시작*하고 "
            "gate 실패/머지 충돌로 재dispatch될 때마다 한 칸 위로 올라간다(첫 시도가 probe, "
            "top에서 cap). **빌더(executor)만 라우팅** — judge/critic 모델은 불변(적대 분리), "
            "spec bar도 불변(anti-erosion). 미지정이면 단일 tier(--model/--reasoning-effort) = "
            "기존 동작 그대로(후방호환)."
        ),
    )
    parser.add_argument(
        "--run-timeout",
        type=float,
        default=120.0,
        help="run 체크(산출물 실행)의 타임아웃 초 (기본 120). 호스트에서 실행, 바운드 필수.",
    )
    parser.add_argument(
        "--codex-idle-timeout",
        type=float,
        default=300.0,
        help=(
            "codex 호출의 *무진행(idle) timeout* 초 (기본 300). `--json` 이벤트가 이 시간 동안 "
            "끊기면(무음=멈춤) 멈춘 프로세스를 정리하고 라우팅한다(필수=재시도/escalate, "
            "best-effort=degrade). **총 시간 cap이 아니라 침묵만 잼** — 진행 중인 긴 호출은 "
            "안 죽인다. 추론 갭보단 길고 무한 hang보단 짧게."
        ),
    )
    parser.add_argument(
        "--codex-max-duration",
        type=float,
        default=None,
        help=(
            "codex 호출의 *선택적* 절대 backstop 초 (기본 없음=off). 진행 중이어도 pathological"
            "하게 길면 차단. 주 메커니즘은 idle — 이건 안전망일 뿐."
        ),
    )
    parser.add_argument(
        "--install-deps",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "gate 체크 전에 호스트(네트워크 O)가 npm/pip install을 대신 수행 (기본 on). "
            "--no-install-deps로 끈다. executor sandbox는 그대로 offline."
        ),
    )
    parser.add_argument(
        "--install-timeout",
        type=int,
        default=300,
        help="호스트 install 타임아웃 초 (기본 300). non-fatal — 초과해도 run은 진행.",
    )
    parser.add_argument(
        "--scaffold",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "executor dispatch 전에 director(host=네트워크 O)가 진짜 스택 스캐폴드를 깔고 "
            "deps 설치 (기본 on=auto: dep 스택 필요할 때만, 아니면 자동 스킵). "
            "--no-scaffold로 끈다(기존 동작 그대로). executor sandbox는 그대로 offline."
        ),
    )
    parser.add_argument(
        "--max-parallel",
        type=int,
        default=4,
        help=(
            "동시에 굴릴 ready unit 수 (기본 4). 1이면 현행 순차 경로(worktree 미사용). "
            ">1이면 git worktree per unit 격리 + 결정적 DAG 스케줄링."
        ),
    )
    parser.add_argument(
        "--max-parallel-burst",
        type=int,
        default=0,
        help=(
            "WO#110(OMC #1): scope 입증 disjoint 유닛(#72 — 서로 겹치지 않는 file-scope·의존 없음)에 "
            "한해 허용할 *상향* 동시 실행 cap. 보수적 cap의 주 이유는 머지충돌 리스크인데 disjoint면 "
            "그 리스크가 부재하므로 cap을 *자원 한계*에만 묶는다(머신에 맞게 설정). 비-disjoint/미선언 "
            "유닛은 --max-parallel 한정 유지. 기본 0=opt-out(= --max-parallel, 기존 동작 그대로). "
            "충돌 backstop(serialize-on-conflict·#48)은 가정이 틀려도 그대로 안전망."
        ),
    )
    parser.add_argument(
        "--skills-dir",
        default=_DEFAULT_SKILLS_DIR,
        help=(
            "읽기전용 패턴 스킬(skills/<name>/SKILL.md) 디렉토리 (기본: 이 repo의 skills/). "
            "매칭된 스킬을 유닛 work order에 빌더 가이드로 주입한다(judge/gate엔 안 들어감)."
        ),
    )
    parser.add_argument(
        "--skills",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="스킬 주입 on/off (기본 on). --no-skills로 끄면 주입 없음(기존 동작 불변).",
    )
    parser.add_argument(
        "--research",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "분해 전 director-측 research 단계(WO#166, pipeline-strengthening B). 기본 off "
            "(opt-in — 기존 동작 byte-identical). 켜면 *복잡* 의뢰에 한해(복잡도 게이트) 첫 "
            "synthesize *전* 1회 오프라인 research(#32 레지스트리)로 ResearchBrief(후보 "
            "disjoint-scope 경계·facade 계약·패턴)를 만들어 합성기에 *제안*으로 주입한다. "
            "단순 의뢰는 skip(추가 콜 0). research=오케스트레이션 LLM이지 executor 아님."
        ),
    )
    parser.add_argument(
        "--fix-ref",
        default=None,
        help=(
            "run 계보(WO#167)에 기록할 *fix 참조* — --continue-from 시 부모→이번 런 사이 적용된 "
            "commit/WO. 미지정이면 현재 HEAD commit(짧은 해시)을 자동 사용. lineage.json에 기록돼 "
            "대시보드 lineage 트리의 엣지로 표시된다(read-only 메타 — 판정 아님)."
        ),
    )
    parser.add_argument(
        "--pricing",
        default=None,
        help=(
            "코스트 계측용 가격표 JSON 경로 (model → [input_$/Mtok, output_$/Mtok]). "
            "주면 usd를 계산, 없으면 모델 미상 → usd=null(토큰만 기록). best-effort 로드."
        ),
    )
    # ── 능력 획득 거버넌스(WO#53 F.1) — opt-in. 기본 OFF면 완전 no-op(기존 동작 불변) ──
    parser.add_argument(
        "--capabilities",
        action="store_true",
        default=False,
        help=(
            "능력 획득 거버넌스 ON(기본 OFF). spec이 요청한 *없는 능력*을 큐레이션 레지스트리에서 "
            "발견→POC→사람 승인(escalate)→채택(provenance). **자동 채택 없음**·executor sandbox 불변."
        ),
    )
    parser.add_argument(
        "--capability-registry",
        default=_DEFAULT_CAPABILITIES_DIR,
        help="큐레이션 능력 레지스트리 디렉토리(capabilities/*.yaml). --capabilities일 때만 사용.",
    )
    parser.add_argument(
        "--capability-allowlist",
        default="",
        help=(
            "사람이 *out-of-band로 승인*한 능력 식별자(쉼표 구분). 승인된 것만 채택(provenance) — "
            "미승인은 escalate(검토 후 여기 추가하고 재실행). 비면 전부 미승인(자동 채택 없음)."
        ),
    )
    parser.add_argument(
        "--capability-search",
        nargs="?",
        const="npm,pypi",
        default=None,
        metavar="REGISTRIES",
        help=(
            "능력 발견을 인터넷으로 확장(기본 OFF · --capabilities 전제). 레지스트리 콤마 리스트: "
            "`npm`(키워드 의미 검색)·`pypi`(이름 기반)·`npm,pypi`. 플래그만 주면 기본 'npm,pypi'. "
            "director-side 검색으로 *원격 후보*(description/keywords/관련도 포함)를 만들어 기존 "
            "escalation(사람 검토)에 surface. **실행 0**(메타데이터만, ok=None)·**자동 채택 없음**"
            "(allowlist 게이트 그대로)·sandbox 불변. 미지정이면 큐레이션-only(네트워크 0)."
        ),
    )
    args = parser.parse_args(argv)

    pricing = _load_pricing(args.pricing)

    # WO#171: 역할별 실행자 배선 해석(--fully-local 프리셋 + 명시 플래그). 기본 전부 codex/human(무회귀).
    wiring = resolve_executor_wiring(args)

    # WO#65: --auto → 미설정 운영 knob 자동 해석(거버넌스 게이트는 비-자동). 명시 플래그는 그대로.
    #   auto_cfg가 tier 사다리/critic-model을 결정하고, summary/warnings를 투명하게 노출한다.
    auto_cfg = resolve_auto_config(args) if args.auto else None
    auto_config_note = None
    if auto_cfg is not None:
        auto_config_note = " | ".join([auto_cfg.summary, *(f"⚠ {w}" for w in auto_cfg.warnings)])
        for w in auto_cfg.warnings:
            print(f"… ⚠ {w}", file=sys.stderr, flush=True)
    # critic 배선: auto면 강제 ON(critic 절대 OFF 아님), 아니면 --critic-model 게이트(기존 동작).
    effective_critic_model = auto_cfg.critic_model if auto_cfg is not None else args.critic_model
    # WO#171: critic-executor=local(완전-로컬)이면 critic ON(약 critic으로 적대 비평 유지).
    critic_on = bool(auto_cfg is not None) or bool(args.critic_model) or (wiring.critic == "local")

    # WO#54: idle(무진행) timeout을 모든 codex 클라이언트에 건다. brain(합성/replan/scaffold)과
    #   executor(빌드)는 *필수* → stall_retries=1(bounded 재시도 후 escalate). judge/critic은
    #   *best-effort* → stall_retries=0(degrade 빠르게). 멈춰도 무한 hang 없이 종료.
    idle_to = args.codex_idle_timeout
    max_dur = args.codex_max_duration

    # WO#55: 라이브 하트비트 사이드카. state.yaml 옆(같은 run 디렉터리)에 heartbeat.json을
    #   best-effort로 쓴다(순수 텔레메트리·state 스키마 무변경). --state-path 없으면 None(무사이드카).
    heartbeat = (
        HeartbeatWriter(Path(args.state_path).parent / "heartbeat.json")
        if args.state_path else None
    )
    # WO#67: 라이브 호출 트랜스크립트 사이드카. state.yaml 옆 transcripts.json에 각 호출의
    #   입력(order/프롬프트) + 실시간 출력 tail을 bounded·best-effort로 흘린다(순수 텔레메트리·
    #   state/heartbeat 스키마 무변경). --state-path 없으면 None(무사이드카). 모든 codex 클라이언트/
    #   executor가 같은 인스턴스를 공유 → 합성·빌드·judge·critic 호출이 한 사이드카로 모인다.
    transcript = (
        TranscriptWriter(Path(args.state_path).parent / "transcripts.json")
        if args.state_path else None
    )

    # WO#171: brain(합성/replan/scaffold)도 역할별 라우팅 — codex(기본·보존) 또는 local(완전-로컬).
    #   stall_retries는 codex에서만 의미(필수 호출). local이면 LocalJudgeClient(오류 시 빈 출력 degrade).
    client = _make_role_client(
        wiring.brain, role="synth", model=args.model,
        local_endpoint=args.local_endpoint, local_model=args.local_model,
        idle_timeout=idle_to, max_duration=max_dur, stall_retries=1,
        local_timeout=args.local_timeout, heartbeat=heartbeat, transcript=transcript,
    )

    # judge LLM 비용 계측(WO#34): judge client를 passthrough MeteredClient로 감싼다.
    # complete 결과를 그대로 통과시키므로 gate 검증 *행동*은 불변 — 비용만 기록된다.
    # gate가 호출마다 이 client를 드레인해 GateResult.judge_cost로 노출하고, 루프가
    # event.cost/budget에 합산한다. judge_model이 없으면 codex 기본(모델 미상→usd=null).
    # WO#171: judge도 역할별 라우팅 — codex(기본·보존) 또는 local(약-judge LocalJudgeClient). 어느
    #   쪽이든 *같은 judge 슬롯*(MeteredClient→CompositeGate.judge_client)에 꽂힌다(gate 판정 로직 불변).
    def make_judge_client() -> LLMClient:
        return MeteredClient(
            _make_role_client(
                wiring.judge, role="judge", model=args.judge_model,
                local_endpoint=args.local_endpoint, local_model=args.local_model,
                idle_timeout=idle_to, max_duration=max_dur,
                stall_retries=0,  # best-effort: 멈추면 degrade(skipped→ambiguous, 가짜 pass 금지)
                local_timeout=args.local_timeout, heartbeat=heartbeat, transcript=transcript,
            ),
            source="judge", pricing=pricing,
        )

    # judge는 read-only CodexClient(executor와 다른 --judge-model 가능). judge 타입
    # 기준이 없는 spec(예: palindrome)이면 CompositeGate가 judge를 아예 안 부른다.
    gate = CompositeGate(
        workdir=args.workdir,
        judge_client=make_judge_client(),
        run_timeout=args.run_timeout,
        install_deps=args.install_deps,
        install_timeout=args.install_timeout,
    )
    # WO#171: 빌더 실행자는 wiring.builder(--fully-local 프리셋 반영). 기본 human(무회귀).
    if wiring.builder == "codex":
        # 자율 쓰기 실행 — gate와 같은 --workdir로 범위 한정.
        # reasoning_effort: 미설정(None)이면 codex 기본(medium) 그대로(후방호환).
        executor: Executor = CodexExecutor(
            model=args.model, workdir=args.workdir,
            reasoning_effort=args.reasoning_effort,
            idle_timeout=idle_to, max_duration=max_dur, stall_retries=1,
            heartbeat=heartbeat, transcript=transcript,
        )
    elif wiring.builder == "local":
        # WO#137: 약한 로컬 모델 빌더(OpenAI 호환 엔드포인트, #136 GB10 llama.cpp).
        # **빌더 ≠ judge 인스턴스 분리(WO#171-A)**: LocalAgentExecutor는 run()만(complete 無)이라
        # judge 슬롯에 못 끼운다. judge가 local이어도(완전-로컬) judge는 *다른 인스턴스*
        # (LocalJudgeClient — 다른 역할/시드/적대 프롬프트)다. 빌더 출력은 judge에 재사용되지 않는다.
        executor = LocalAgentExecutor(
            endpoint=args.local_endpoint, model=args.local_model,
            workdir=args.workdir, max_turns=args.local_max_turns,
            timeout=args.local_timeout,  # WO#150: idle-timeout(#54)
            verify=(builder_smoke if args.local_smoke else None),
            selftest=(builder_selftest if args.local_smoke else None),
            heartbeat=heartbeat, transcript=transcript,
        )
    else:
        executor = HumanRelayExecutor()

    # WO#64: tier 사다리 — 미지정이면 단일 tier[(--model, --reasoning-effort)] = 기존 동작.
    # 다중 tier면 빌더 팩토리가 *tier 인자*(model/effort)로 그 강도의 executor를 만든다.
    # WO#65: --auto면 해석된 사다리(자동 effort 사다리/명시 오버라이드)를 쓴다.
    tier_ladder = (
        auto_cfg.tier_ladder if auto_cfg is not None
        else parse_tier_ladder(args.tier_ladder, args.model, args.reasoning_effort)
    )

    # 병렬 모드(>1): unit마다 worktree 경로에 묶인 executor/gate를 만든다.
    # 통합 gate(머지된 main 1회)는 위 gate(=main workdir)를 그대로 쓴다.
    executor_factory = None
    gate_factory = None
    if args.max_parallel > 1:
        if wiring.builder == "codex":
            # tier-aware(2-arg): 루프가 그 시도의 Tier를 넘긴다(단일 tier면 사다리 0번 = 기존값).
            executor_factory = lambda wt, tier: CodexExecutor(
                model=tier.model, workdir=wt, reasoning_effort=tier.reasoning_effort,
                idle_timeout=idle_to, max_duration=max_dur, stall_retries=1,
                heartbeat=heartbeat, transcript=transcript,
            )
        elif wiring.builder == "local":
            # WO#137 빌더 전용(1-arg=후방호환). 로컬 엔드포인트는 단일 모델을 서빙하므로
            # tier model override는 적용 안 한다(설정된 endpoint/model 재사용). 판정 무접촉.
            executor_factory = lambda wt: LocalAgentExecutor(
                endpoint=args.local_endpoint, model=args.local_model,
                workdir=wt, max_turns=args.local_max_turns,
                timeout=args.local_timeout,  # WO#150: idle-timeout(#54)
                verify=(builder_smoke if args.local_smoke else None),
                selftest=(builder_selftest if args.local_smoke else None),
                heartbeat=heartbeat, transcript=transcript,
            )
        else:
            executor_factory = lambda wt: HumanRelayExecutor()
        # per-worktree gate도 metered judge client(유닛마다 새 인스턴스 → 스레드 안전).
        gate_factory = lambda wt: CompositeGate(
            workdir=wt, judge_client=make_judge_client(),
            run_timeout=args.run_timeout,
            install_deps=args.install_deps, install_timeout=args.install_timeout)

    # spec critic: --critic-model 줄 때만 ON(read-only, 합성기와 다른 모델 권장 = 독립성).
    # 없으면 None → critic OFF(추가 비용 0, 기존 동작 불변).
    # WO#65: --auto면 critic 강제 ON(절대 OFF 아님 — decomp critic/OR 살림). effective_critic_model이
    #   None이면 codex 기본(최신)으로 돈다. 빌더 model과 분리 못 하면 위에서 독립성 경고를 냈다.
    # WO#171: critic도 역할별 라우팅 — codex(기본·보존) 또는 local(약 critic, critic-executor=local이면 ON).
    critic_client = (
        _make_role_client(
            wiring.critic, role="critic", model=effective_critic_model,
            local_endpoint=args.local_endpoint, local_model=args.local_model,
            idle_timeout=idle_to, max_duration=max_dur,
            stall_retries=0,  # best-effort: 멈추면 진행(critic은 advisory)
            local_timeout=args.local_timeout, heartbeat=heartbeat, transcript=transcript,
            default_call_kind="critic",
        )
        if critic_on else None
    )

    # WO#171: shadow 비교 관측(opt-in·적용 0). 약 judge(적용) 옆에 codex shadow judge(기록만)를 단다.
    #   gate + gate_factory를 ShadowComparingGate로 감싸 검증역전(약pass·강fail)을 sink에 누적한다
    #   (verdict 권위 불변 — primary 약 judge만 적용). shadow OFF(기본)면 이 블록 skip = codex 흔적 0
    #   (완전-로컬이면 thesis 순수). shadow gate는 같은 workdir를 codex로 *재판정*만 한다(판정 로직 불변).
    shadow_sink = None
    if wiring.shadow_judge:
        from haetae.shadow import ShadowComparingGate, ShadowSink

        shadow_sink = ShadowSink()

        def _make_shadow_gate(wd) -> Gate:
            return CompositeGate(
                workdir=wd,
                judge_client=MeteredClient(
                    _make_role_client(
                        wiring.shadow_judge, role="judge", model=args.judge_model,
                        local_endpoint=args.local_endpoint, local_model=args.local_model,
                        idle_timeout=idle_to, max_duration=max_dur, stall_retries=0,
                        local_timeout=args.local_timeout, heartbeat=heartbeat, transcript=transcript,
                    ),
                    source="shadow", pricing=pricing,
                ),
                run_timeout=args.run_timeout,
                install_deps=args.install_deps, install_timeout=args.install_timeout,
            )

        gate = ShadowComparingGate(gate, _make_shadow_gate(args.workdir), shadow_sink)
        if gate_factory is not None:
            _primary_gf = gate_factory
            gate_factory = lambda wt: ShadowComparingGate(
                _primary_gf(wt), _make_shadow_gate(wt), shadow_sink
            )

    # WO#171-C: 약-judge 정직 표기(judge/critic/빌더/brain 실행자 정체성 + weak·shadow). read-only 메타 —
    #   verdict를 절대 바꾸지 않는다(gate/run_judge 로직 불변). 약-judge면 stderr로도 한 줄 정직 경고.
    judge_profile = JudgeProfile(
        brain_executor=wiring.brain, builder_executor=wiring.builder,
        judge_executor=wiring.judge, critic_executor=wiring.critic,
        judge_model=(args.local_model if wiring.judge == "local" else args.judge_model),
        weak_judge=(wiring.judge == "local"),
        shadow_judge=wiring.shadow_judge,
        note=_judge_profile_note(wiring),
    )
    if wiring.judge == "local":
        print(f"… ⚠ {judge_profile.note}", file=sys.stderr, flush=True)

    # 선제 스캐폴드(WO#27): --scaffold(기본 on)면 brain client를 scaffold 생성에 재사용.
    # --no-scaffold면 None → 스캐폴드 OFF(기존 동작 그대로). 생성기는 dep 스택 필요할 때만
    # 골격을 내고 아니면 자동 스킵(auto). 호스트 install은 --install-deps 토글을 공유한다.
    scaffold_client = client if args.scaffold else None

    # ── 이어가기(②a, WO#58): 부모 해석 + workdir 시딩 + 증분 context ──
    # 명시적 요청이라 실패는 명확한 에러(조용한 폴백 아님). scaffold는 스킵(스택 이미 존재).
    synth_context: str | None = None
    seeded = False
    reuse_manifest: dict | None = None
    resume_spec: ProjectSpec | None = None
    resume_state: State | None = None
    if args.continue_from:
        # WO#91: 순수 재개(같은 order · 부모 spec.yaml 존재 · not --resynthesize)면 부모
        #   plan/criteria를 *보존*(재합성 skip)하고, 그 외(새 order/spec 부재/--resynthesize)는
        #   기존 증분 연속(재합성)으로 분기한다. 둘 다 부모 코드는 workdir에 시딩(scaffold 스킵).
        try:
            rp = load_resume(
                args.continue_from, args.runs_dir, args.workdir,
                order=args.order, resynthesize=args.resynthesize,
            )
        except ContinuationError as e:
            print(f"이어가기 실패: {e}", file=sys.stderr, flush=True)
            return 2  # 명확한 에러(조용한 폴백 아님) — 부모는 명시적 입력
        seeded = True
        scaffold_client = None  # 이어가기 = scaffold 스킵(스택 이미 시딩됨)
        if args.state_path:
            # WO#167: 계보 사이드카(parent_run_id + fix_ref) — 다-런 arc 추적(read-only 메타·best-effort).
            _write_lineage(
                args.state_path, rp.parent_dir.name, fix_ref=_resolve_fix_ref(args.fix_ref)
            )
        if rp.mode == "pure_resume":
            resume_spec = rp.resume_spec
            resume_state = rp.resume_state
            n_done = sum(
                1 for p in (resume_state.plan if resume_state else [])
                if p.state == PlanState.done
            )
            n_units = len(resume_spec.decomposition) if resume_spec else 0
            print(
                f"… 순수 재개(WO#91): 부모 {rp.parent_dir.name} plan/criteria 보존(재합성 skip) — "
                f"{rp.seeded_n}개 파일 시딩 + done {n_done}/{n_units} 유닛 시드(미완만 재빌드)",
                file=sys.stderr, flush=True,
            )
            if args.no_reuse:
                # 순수 재개는 done을 _init_resume_state로 시드한다(evaluate_reuse 미경유) — --no-reuse
                # 무효. 전부 재빌드를 원하면 --resynthesize(증분 재합성 경로서 --no-reuse 적용).
                print(
                    "… 참고: --no-reuse는 순수 재개엔 적용되지 않는다(done 유닛 보존). "
                    "전부 재빌드하려면 --resynthesize를 쓰라.",
                    file=sys.stderr, flush=True,
                )
        else:
            synth_context = rp.synth_context
            reuse_manifest = rp.reuse_manifest
            reuse_n = 0 if (args.no_reuse or not reuse_manifest) else len(reuse_manifest)
            print(
                f"… 이어가기(증분): 부모 {rp.parent_dir.name}에서 {rp.seeded_n}개 파일 시딩 + "
                f"scaffold 스킵 + 증분 합성 (재사용 후보 done 유닛 {reuse_n}개"
                + (", --no-reuse로 끔" if args.no_reuse else "") + ")",
                file=sys.stderr, flush=True,
            )

    # WO#75: CLI run의 원 주문을 meta.json 사이드카로 기록(런처 형식 호환) → 대시보드 #57 주문
    # 뷰가 CLI run도 커버. 런처가 spawn한 경우 이미 meta가 있어 안 덮는다(추가형·best-effort).
    if args.state_path:
        _write_cli_meta(args.state_path, args.order, workdir=args.workdir)  # WO#108-B: 시드 자동해소용 workdir 기록

    # 스킬 주입(빌더 전용): --skills(기본 on)면 --skills-dir에서 로드. --no-skills면 None.
    skills_dir = args.skills_dir if args.skills else None

    # 능력 발견 F.2(opt-in): --capability-search(+ --capabilities)일 때만 인터넷 searcher를 만든다.
    # **네트워크 모듈은 여기서만 import**(opt-in 경로) — 기본 경로는 network-free 유지.
    capability_searcher = None
    if args.capabilities and args.capability_search:
        from haetae.capability_search import make_searcher  # opt-in 시에만 import(네트워크 격리)

        # 콤마 리스트(npm,pypi 등) 통과 — 미지 레지스트리는 make_searcher가 ValueError.
        capability_searcher = make_searcher(args.capability_search)

    # 진행 표시: 느린 codex 호출이 "행"으로 안 보이게 stderr로 한 줄씩.
    def progress(msg: str) -> None:
        print(f"… {msg}", file=sys.stderr, flush=True)

    # WO#166: 분해 전 director-측 research 단계(pipeline-strengthening B) — replan 루프 *밖*·1회.
    # opt-in(--research, 기본 off → 기존 동작 byte-identical). 켜져도 복잡도 게이트가 단순 의뢰는
    # skip(추가 콜 0). 순수 재개(resume_spec≠None)면 합성 skip이라 research도 skip. brief는
    # synth_context에 *제안*으로 얹혀 합성기가 소비(override 가능 — 적대 spec/decomp critic 그대로).
    # research=오케스트레이션 LLM(critic-model 우선, 없으면 main client)이지 *executor 아님* →
    # ALLOWED_SANDBOXES 무관. 소스=#32 레지스트리(오프라인) + 의뢰 분석(네트워크 0, F.2 후속).
    if args.research and resume_spec is None:
        from haetae.research import maybe_research

        synth_context = maybe_research(
            args.order,
            critic_client or client,
            synth_context,
            skills_dir=args.skills_dir,
            progress=progress,
        )

    # graceful stop(WO#43): 웹 stop(#37)은 이 프로세스에 SIGINT를 보낸다. run_loop이
    # 이미 정리·저장·클린 마무리하지만(KeyboardInterrupt를 잡아 State 반환), 그 바깥
    # (배선/예외 흐름)에서 인터럽트가 와도 raw traceback 없이 클린 종료한다.
    # 종료코드 0 = stopped로 해석(대시보드가 "failed"로 오해하지 않게 정합).
    try:
        state = run(
            args.order,
            client=client,
            executor=executor,
            gate=gate,
            critic_client=critic_client,
            max_iters=args.max_iters,
            decomp_critic=args.decomp_critic,
            decomp_retries=args.decomp_retries,
            or_alternatives=args.or_alternatives,
            max_tokens=args.max_tokens,                      # WO#68 (B)
            unit_attempt_budget=args.unit_attempt_budget,    # WO#68 (C)
            unit_token_budget=args.unit_token_budget,        # WO#68 (C)
            state_path=args.state_path,
            progress=progress,
            max_parallel=args.max_parallel,
            max_parallel_burst=args.max_parallel_burst,  # WO#110: disjoint burst(0=보수적 기본)
            workdir=args.workdir,
            executor_factory=executor_factory,
            gate_factory=gate_factory,
            unit_retries=args.unit_retries,
            tier_ladder=tier_ladder,
            auto_config_note=auto_config_note,
            scaffold_client=scaffold_client,
            install_deps=args.install_deps,
            skills_dir=skills_dir,
            pricing=pricing,
            heartbeat=heartbeat,
            synth_context=synth_context,
            resume_spec=resume_spec,                  # WO#91: 순수 재개 시 부모 spec(재합성 skip)
            resume_state=resume_state,                # WO#91: 부모 plan-state(done 시드/미완 재빌드)
            seeded=seeded,
            reuse_manifest=reuse_manifest,            # WO#71 ②b: 부모 검증 done 유닛 manifest
            reuse=not args.no_reuse,                  # --no-reuse면 전부 재빌드(escape)
            capabilities_on=args.capabilities,
            # opt-in: --capabilities일 때만 레지스트리/allowlist를 넘긴다(OFF면 no-op).
            capability_registry_path=(args.capability_registry if args.capabilities else None),
            capability_allowlist=(
                [s.strip() for s in args.capability_allowlist.split(",") if s.strip()]
                if args.capabilities else None
            ),
            capability_searcher=capability_searcher,  # F.2: opt-in 원격 발견(off면 None)
            judge_profile=judge_profile,               # WO#171-C: 약-judge 정직 표기(read-only 메타)
            shadow_sink=shadow_sink,                   # WO#171-shadow: 검증역전 누적기(opt-in, 적용 0)
        )
    except KeyboardInterrupt:
        print("중단됨 (사용자 stop/SIGINT)", file=sys.stderr, flush=True)
        return 0
    print(format_summary(state))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
