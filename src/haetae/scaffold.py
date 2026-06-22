"""선제 스캐폴드 (WO#27) — offline executor가 *진짜* 스택을 보게 만든다.

배경(캡스톤이 드러낸 뿌리): executor sandbox는 offline이라 React/Vite/TS 같은 dep 스택을
못 깔고 → 그 스택을 *통째로 회피*해 plain Node `.mjs`로 빌드한다(스택 치환). #23 호스트-설치는
*선언된* deps만 까는데 executor가 React를 *선언조차 안 하니* 깔 게 없다.

근본 수정: executor 시작 *전에*, 네트워크 있는 director(host)가 진짜 스택 스캐폴드
(package.json + 최소 config/entry stub)를 깔고 deps를 설치 → executor가 React를 *실재하는
것으로* 본다. 본체 구현은 executor 몫, 골격만 host가 깐다.

안전 불변: executor sandbox는 계속 offline(`providers/codex.py` 불변). 스캐폴드 생성·설치는
전부 host(director)에서. executor는 *이미 채워진* workspace(package.json + node_modules +
config)를 받을 뿐 — 네트워크를 *주지 않는다*.

best-effort: LLM 호출/파싱/검증 실패는 전부 `None`으로 흡수한다(스캐폴드 없이 진행, raise 금지).
`generate_scaffold`가 `None`이면 호출 루프의 모든 신규 경로가 no-op → 기존 동작 불변.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable

import yaml
from pydantic import BaseModel, ValidationError

from haetae.llm import LLMClient
from haetae.models import ProjectSpec
from haetae.parsing import strip_code_fence

DEFAULT_SCAFFOLD_PROMPT_PATH = "prompts/scaffold.md"

# 커밋 신원 — worktree.py와 동일. user.* 미설정 환경(CI/테스트)에서도 커밋되게 박는다.
_GIT_IDENT = ("-c", "user.email=haetae@local", "-c", "user.name=haetae")

# symlink된 node_modules가 머지에 안 잡히게 보장할 *bare* 항목.
# deps.py가 쓰는 `node_modules/`(디렉토리 한정)는 symlink를 무시하지 못한다(검증됨) →
# main에 bare `node_modules`를 커밋해 worktree들이 상속하게 한다(머지 정합).
_BARE_NODE_MODULES = "node_modules"

# git 실행 결과를 받는 runner 시그니처: (args, cwd) -> (returncode, output). 테스트 주입용.
GitRunner = Callable[[list[str], str], "tuple[int, str]"]


class Scaffold(BaseModel):
    """director가 spec으로부터 생성한 *최소 실행 가능 골격*.

    files: 경로(workdir 상대) → 내용. package.json(실제 deps) + 최소 config/entry stub.
           본체 로직은 비워두고 executor가 채운다.
    install: 호스트에서 deps install(ensure_deps)을 돌릴지. dep-bearing 스택이면 True.
    """

    files: dict[str, str]
    install: bool = True


# ──────────────────────────── 생성 (LLM) ────────────────────────────


def _dump_spec(spec: ProjectSpec) -> str:
    return yaml.safe_dump(
        spec.model_dump(by_alias=True, mode="json"),
        allow_unicode=True,
        sort_keys=False,
    )


def _generate_stack_scaffold(
    spec: ProjectSpec,
    client: LLMClient,
    prompt_path: str | Path = DEFAULT_SCAFFOLD_PROMPT_PATH,
) -> Scaffold | None:
    """spec을 보고 dep 스택이 필요하면 최소 골격 `Scaffold`를, 아니면 `None`을 반환한다.

    완전 best-effort: LLM 호출 실패·YAML 파싱 실패·스키마 검증 실패·빈 files를 전부
    `None`으로 흡수한다(스캐폴드 없이 진행 — raise 금지). `None`이면 호출 루프의 모든
    신규 경로가 no-op이 되어 기존 동작이 불변으로 보존된다.

    LLM이 스택 불필요라고 판단하면 빈/null/none을 내도록 프롬프트가 유도한다 → `None`.
    """
    try:
        system = Path(prompt_path).read_text(encoding="utf-8")
        user = f"# 합성된 spec\n```yaml\n{_dump_spec(spec)}```"
        raw = client.complete(system, user)
    except Exception:  # noqa: BLE001 — best-effort: 어떤 클라이언트/IO 실패도 run을 죽이면 안 됨
        return None

    try:
        data = yaml.safe_load(strip_code_fence(raw))
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None  # null/none/스칼라 → 스택 불필요 = 스킵

    files = data.get("files")
    if not isinstance(files, dict) or not files:
        return None  # 빈/없음 → 스킵 (no-op)

    # 값 정규화(None 항목 제거 + str 강제) 후 검증. 깨지면 None으로 흡수.
    norm = {str(k): str(v) for k, v in files.items() if v is not None}
    if not norm:
        return None
    try:
        return Scaffold.model_validate(
            {"files": norm, "install": bool(data.get("install", True))}
        )
    except ValidationError:
        return None


# ──────────────────────── 트레이스-하니스 스캐폴드 (WO#157) ────────────────────────
#
# #156 진단: 검증 *트레이스*(풀-사슬 플레이스루)는 한 end-to-end 유닛이라야 한다(행동-split 금지 —
# #157 decomp-critic). 그러나 그 한 유닛이 약빌더 역량 초과 → escalate. #27 원칙으로 director가
# 트레이스-하니스 *보일러플레이트*(엔진 로드·결정적 tick 드라이버·상태 레코더·단언 프레임)를 선제
# 생성해 tractable화한다. **빌더는 시나리오 시퀀스 + 행동별 단언만 채운다**(아래 TODO). 스캐폴드는
# 인프라(보일러플레이트)지 *단언/시나리오/판정이 아니다* — run-judge가 트레이스 *출력*을 #113 풀-사슬로
# 독립 평가(적대 분리·바 불변). 서버리스(#128): 헤드리스 node, loopback/서버 금지.

# 트레이스-하니스 유닛 탐지(보수적·로컬 — intake와 동형, import 사이클 회피).
_TRACE_UNIT_MARKERS = (
    "트레이스", "하니스", "헤드리스", "trace", "harness", "headless", "playthrough", "플레이스루",
)
_TRACE_UNIT_EXCLUDE = (  # 트레이스를 *언급*만 하고 생산 안 하는 준비/네이밍 유닛(#99 동형)
    "이름 준비", "이름만", "구조만", "네이밍", "naming", "스크립트 이름",
)

# 별도 *참고 경로* — 빌더의 실제 트레이스 파일/스택 파일을 덮어쓰지 않는다(continue-from seed 안전).
_TRACE_HARNESS_SKELETON_PATH = "scripts/trace/harness.skeleton.mjs"
# 보일러플레이트 골격(결정적·서버리스). 빌더가 TODO(시나리오+단언)만 채운다 — 판정/단언 아님.
_TRACE_HARNESS_SKELETON = '''\
// haetae 트레이스-하니스 스캐폴드 (#157 · #27 원칙) — *보일러플레이트 골격*.
// 빌더는 아래 TODO(시나리오 시퀀스 + 행동별 단언)만 채운다. 검증 트레이스는 한 end-to-end 유닛
// (행동-split 금지, #157) — 한 플레이스루로 통합 게임 전체를 구동해 증거를 emit한다.
// 판정은 *이 파일이 안 한다* — 독립 run-judge가 트레이스 *출력(stdout JSON)*을 #113 풀-사슬로
// 평가한다(부분 트레이스는 fail — 바 불변). 서버리스(#128): 헤드리스 node, http/listen/서버 금지.

// -- 1. 엔진/모듈 로드 (빌더: 실제 import 경로로 교체) -----------------------------------
// import { createGame, step } from "../../src/engine/index.js"  // TODO(builder): 실제 경로

// -- 2. 상태 레코더 (틱별 관측 상태 캡처 - 빌더: 검증할 필드 채움) ------------------------
function snapshot(game) {
  // TODO(builder): 검증할 상태 필드를 game에서 캡처(head/length/score/collision/game_over 등)
  return {};
}

// -- 3. 결정적 tick 드라이버 (입력 시퀀스를 한 틱씩 진행하며 상태 기록) -------------------
function drive(game, inputs) {
  const frames = [];
  for (const input of inputs) {
    // TODO(builder): step(game, input) 으로 한 틱 진행
    frames.push(snapshot(game));
  }
  return frames;
}

// -- 4. 시나리오 (빌더: 풀-사슬 입력 시퀀스 - 이동/먹이/성장/점수/벽충돌/자기충돌/game over) --
const scenario = [
  // TODO(builder): 전체 행동 사슬을 한 플레이스루로 구동하는 결정적 입력 시퀀스
];

// -- 5. 실행 + 단언 프레임 (빌더: frames에서 행동별 증거 채움) ----------------------------
function main() {
  // const game = createGame(/* TODO(builder) */);
  // const frames = drive(game, scenario);
  const evidence = {
    // TODO(builder): 관측된 frames로부터 행동별 증거(directions_covered, ate, grew,
    //                score_increased, wall_collision, self_collision, game_over ...)
  };
  process.stdout.write(JSON.stringify(evidence));  // 단일 JSON만 stdout(#86 위생)
}

main();
'''

# ──────────── WO#160: facade 계약 → 트레이스 import 선채움 (A) + 런타임-smoke 하니스 (B) ────────────
# #158 진단: 빌더가 트레이스 import를 *추측*(createGameEngine vs 실제 GameEngine 계약)해
# ERR_MODULE_NOT_FOUND; 통합 런타임 계약 버그(Food.generate static/instance → 빌드되나 new에서 크래시)가
# 빌드-only ac를 통과. 합성기가 명시한 *고정 facade 계약*(spec.facade_contract)을 스캐폴드가 결정화 —
# (A) 트레이스 import 선채움(추측 제거), (B) 런타임-smoke 하니스 생성(빌드-passes-but-crashes 조기 포착).
# 인프라지 단언/판정 아님 — 트레이스 run-judge의 #113 풀-사슬 바는 불변(smoke=필요조건이지 충분조건 아님).

# 트레이스/스모크 하니스는 scripts/trace/ 아래(2단계 깊이) → workdir-상대 모듈을 ../../ 로 참조.
_TRACE_DIR_TO_ROOT = "../../"
# 트레이스 골격의 placeholder import 줄(계약 있으면 이 한 줄을 선채움 import로 치환 — 나머지 골격 불변).
_TRACE_IMPORT_PLACEHOLDER = (
    '// import { createGame, step } from "../../src/engine/index.js"  // TODO(builder): 실제 경로'
)
_RUNTIME_SMOKE_PATH = "scripts/trace/runtime-smoke.mjs"


def _facade_import_line(contract) -> str:
    """facade 계약 → ES import 한 줄. named(`import {{ X }}`) 기본, export_kind=='default'면 default import."""
    mp = (contract.module_path or "").strip().lstrip("./")
    rel = _TRACE_DIR_TO_ROOT + mp
    if (contract.export_kind or "").lower() == "default":
        return f'import {contract.export_name} from "{rel}";'
    return f'import {{ {contract.export_name} }} from "{rel}";'


def _facade_construct_expr(contract) -> str:
    """인스턴스화 식. contract.construct 우선, 없으면 export_kind로 추론(class→`new X()`, 그 외→`X()`)."""
    if (contract.construct_expr or "").strip():
        return contract.construct_expr.strip()
    if (contract.export_kind or "class").lower() == "class":
        return f"new {contract.export_name}()"
    return f"{contract.export_name}()"


def _runtime_smoke_harness(contract) -> str:
    """런타임-smoke 하니스(#160 B): wire된 엔진을 facade 계약대로 import→인스턴스화→(1-tick)→throw 0.
    빌드-passes-but-crashes(계약 불일치: #158 Food.generate static/instance)를 *통합*서 조기 포착한다.
    완전 생성(빌더 TODO 0) — 빌더는 *엔진을 계약에 맞춰* 이 smoke를 통과시킨다. 크래시 0만 검사(단언/판정
    아님) — 풀-사슬 행동은 트레이스 run-judge가 #113으로 검증(불변). 서버리스(#128): 헤드리스 node.
    """
    import_line = _facade_import_line(contract)
    construct = _facade_construct_expr(contract)
    tick = (contract.tick or "").strip()
    tick_line = f"  {tick};  // ← 1-tick(계약)\n" if tick else ""
    return (
        "// haetae 런타임-smoke 스캐폴드 (#160) — 통합 엔진이 *빌드뿐 아니라 런타임에 도는지* 검사.\n"
        "// facade 계약대로 wire된 엔진을 import → 인스턴스화 → (1-tick) → throw 0. 헤드리스 node(#128 서버리스).\n"
        "// 판정/단언 아님(인프라): 크래시 0만 검사 — 풀-사슬 행동은 트레이스 run-judge가 #113으로 검증(불변).\n"
        f"{import_line}  // ← facade 계약(선채움)\n"
        "function main() {\n"
        f"  const engine = {construct};  // ← construct(계약) — 여기서 throw면 통합 런타임 계약 버그\n"
        f"{tick_line}"
        '  process.stdout.write(JSON.stringify({ runtime_smoke: "ok", engine: typeof engine }));\n'
        "}\n"
        "main();\n"
    )


def runtime_smoke_harness(spec: ProjectSpec) -> dict[str, str] | None:
    """facade 계약이 있으면 런타임-smoke 하니스 파일(경로→내용)을, 없으면 None (WO#160 B).

    통합 acceptance가 빌드 → 빌드 + 런타임-smoke로 강화돼 빌드-passes-but-crashes(계약 불일치)를
    *통합*서(트레이스 前) 조기 포착한다. 결정적(완전 생성·LLM 무관). 서버리스(#128). 별도 참고 경로라
    빌더 파일을 덮어쓰지 않는다(seed 안전). 트레이스 run-judge·#113 바 불변(smoke=필요조건이지 충분조건 아님).
    """
    contract = getattr(spec, "facade_contract", None)
    if contract is None:
        return None
    return {_RUNTIME_SMOKE_PATH: _runtime_smoke_harness(contract)}


def _spec_has_trace_harness_unit(spec: ProjectSpec) -> bool:
    """spec 분해에 *검증 트레이스-하니스* 유닛이 있는지 보수적 판정(WO#157)."""
    for u in (spec.decomposition or []):
        low = (getattr(u, "desc", None) or "").lower()
        if any(m in low for m in _TRACE_UNIT_MARKERS) and not any(
            x in low for x in _TRACE_UNIT_EXCLUDE
        ):
            return True
    return False


def trace_harness_skeleton(spec: ProjectSpec) -> dict[str, str] | None:
    """검증 트레이스-하니스 유닛이 있으면 *보일러플레이트 골격* 파일(경로→내용)을, 없으면 None (WO#157).

    골격 = 인프라(엔진 로드·tick 드라이버·상태 레코더·단언 프레임)지 *단언/시나리오/판정 아님* —
    빌더가 TODO(시나리오+단언)만 채워 풀-사슬 트레이스를 단일-유닛 역량 내로 끌어온다. 서버리스(#128).
    결정적(LLM 무관). 별도 *참고 경로*라 빌더의 실제 트레이스 파일을 덮어쓰지 않는다(seed 안전).
    """
    if not _spec_has_trace_harness_unit(spec):
        return None
    skeleton = _TRACE_HARNESS_SKELETON
    contract = getattr(spec, "facade_contract", None)
    if contract is not None:
        # (A) #160: placeholder import 한 줄을 facade 계약 import로 *선채움*(빌더 추측 제거 — #158 직격).
        # 나머지 골격(레코더·드라이버·시나리오·단언 TODO)은 불변 — 빌더는 여전 시나리오+단언만 채운다.
        prefilled = (
            _facade_import_line(contract)
            + "  // (#160 facade 계약 — 선채움; 빌더는 시나리오+단언만 채운다)"
        )
        skeleton = skeleton.replace(_TRACE_IMPORT_PLACEHOLDER, prefilled)
    return {_TRACE_HARNESS_SKELETON_PATH: skeleton}


def generate_scaffold(
    spec: ProjectSpec,
    client: LLMClient,
    prompt_path: str | Path = DEFAULT_SCAFFOLD_PROMPT_PATH,
) -> Scaffold | None:
    """선제 스캐폴드: (LLM) dep-스택 골격 + (결정적·WO#157) 트레이스-하니스 골격을 합쳐 반환.

    스택 골격은 best-effort(`_generate_stack_scaffold` — 실패/불필요면 None). 거기에 검증
    트레이스-하니스 유닛이 있으면 결정적 트레이스 골격을 *추가*한다(#157 — 빌더가 시나리오+단언만
    채우게). 둘 다 없으면 None(기존 동작 불변). 트레이스 골격은 별도 경로라 스택/빌더 파일과 충돌 0.
    스캐폴드(인프라)는 단언/판정이 아니다 — run-judge가 트레이스 출력을 독립 평가(#113 바 불변).
    """
    stack = _generate_stack_scaffold(spec, client, prompt_path)
    trace = trace_harness_skeleton(spec)
    smoke = runtime_smoke_harness(spec)  # WO#160 (B): facade 계약 있으면 런타임-smoke 하니스 추가
    if trace is None and smoke is None:
        return stack
    files = dict(stack.files) if stack is not None else {}
    for extra in (trace, smoke):
        if extra:
            for path, content in extra.items():
                files.setdefault(path, content)  # 충돌 안 나게(스택이 이미 쓴 경로 보존)
    return Scaffold(files=files, install=(stack.install if stack is not None else False))


# ──────────────────────────── 파일 쓰기 ────────────────────────────


def write_scaffold(scaffold: Scaffold, workdir: str | Path) -> list[str]:
    """scaffold 파일들을 workdir에 쓴다(부모 디렉토리 생성). 쓴 상대경로 목록 반환.

    경로 안전: workdir 밖으로 나가는 항목(절대경로·`..` 탈출)은 건너뛴다(사고 방지).
    """
    wd = Path(workdir).resolve()
    written: list[str] = []
    for rel, content in scaffold.files.items():
        target = (wd / rel).resolve()
        try:
            target.relative_to(wd)
        except ValueError:
            continue  # workdir 탈출 시도 → 무시
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        written.append(rel)
    return written


# ──────────────────────────── main 커밋 (병렬 상속) ────────────────────────────


def _default_git(args: list[str], cwd: str) -> tuple[int, str]:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    return proc.returncode, (proc.stderr or proc.stdout or "")


def _ensure_symlink_safe_gitignore(workdir: Path) -> None:
    """main .gitignore에 *bare* `node_modules`를 보장(symlink 머지 누수 방지). non-fatal.

    deps.ensure_deps는 `node_modules/`(디렉토리 한정)만 쓴다 → symlink된 node_modules는
    그 패턴으로 무시되지 않아 worktree 머지 `git add -A`에 잡힌다(검증됨). bare 항목을
    main에 박아 worktree들이 상속하게 하면 symlink가 머지에서 안전히 빠진다.
    """
    gi = workdir / ".gitignore"
    try:
        existing = gi.read_text(encoding="utf-8") if gi.exists() else ""
        if _BARE_NODE_MODULES in {ln.strip() for ln in existing.splitlines()}:
            return
        prefix = existing if (existing == "" or existing.endswith("\n")) else existing + "\n"
        gi.write_text(prefix + _BARE_NODE_MODULES + "\n", encoding="utf-8")
    except OSError:
        pass


def commit_scaffold(
    workdir: str | Path,
    message: str = "haetae: scaffold (director stack)",
    *,
    git: GitRunner | None = None,
) -> bool:
    """workdir의 scaffold 변경을 main에 커밋한다(worktree들이 분기 시 상속하도록). best-effort.

    node_modules 등 install 산출물은 .gitignore(ensure_deps + bare 보강)로 staging에서
    빠진다. 커밋 실패는 non-fatal(False) — 파일은 이미 디스크에 있어 worktree 분기 시 보인다.
    호출 전 ensure_deps로 node_modules가 이미 생성돼 있어도 무방(gitignore로 제외).
    """
    git = git or _default_git
    _ensure_symlink_safe_gitignore(Path(workdir))
    wd = str(workdir)
    try:
        git(["add", "-A"], wd)
        rc, _out = git([*_GIT_IDENT, "commit", "-m", message], wd)
        return rc == 0
    except Exception:  # noqa: BLE001 — 커밋 실패는 run을 죽이지 않는다(파일은 이미 디스크에)
        return False


# ──────────────────────────── worktree node_modules 준비 ────────────────────────────


def prepare_worktree_deps(
    main_workdir: str | Path,
    worktree_path: str | Path,
    *,
    ensure_deps_fn: Callable[[Path], object] | None = None,
) -> str:
    """worktree에 node_modules를 준비한다. 반환: "symlink" | "copy" | "install" | "none".

    node_modules는 gitignore라 worktree가 git으로 *상속하지 못한다* → executor dispatch
    *전에* 여기서 따로 채운다. main의 node_modules를 symlink(기본: 빠름·디스크 0)하고,
    symlink 불가면 copytree, 그래도 안 되면 per-worktree host-install(ensure_deps)로 폴백.

    offline executor는 node_modules를 *읽기만* 하면 되므로 공유 symlink로 충분하다.
    bare `node_modules` gitignore(commit_scaffold가 main에 보강)가 상속돼 symlink가
    머지에서 안전히 빠진다.
    """
    main = Path(main_workdir)
    wt = Path(worktree_path)
    src = main / "node_modules"
    dst = wt / "node_modules"

    if src.is_dir() and not dst.exists():
        try:
            os.symlink(src.resolve(), dst, target_is_directory=True)
            return "symlink"
        except OSError:
            try:
                shutil.copytree(src, dst, symlinks=True)
                return "copy"
            except OSError:
                pass  # 폴백으로

    # 폴백: worktree에 매니페스트가 있으면 거기서 직접 host-install(상속된 package.json).
    if ensure_deps_fn is not None and not dst.exists():
        res = ensure_deps_fn(wt)
        return "install" if getattr(res, "installed", False) else "none"
    return "none"
