"""스킬 자동 학습 (WO#103, OMC #4) — F.1 거버넌스(자동채택 0·사람 승인·provenance).

#32 스킬은 seeded(수동). 여기서는 *완주(done) run*에서 재사용 **패턴**을 후보 SKILL.md로
추출(staging)한다 — 단 **자동채택은 절대 없다**. F.1(#53) 정신:
  - **자동채택 0**: 학습기는 *후보*만 만든다(skills/_candidates/<name>/). 활성 #32 레지스트리는
    `_`-접두 디렉토리를 로드에서 제외(skills.load_skills) → 후보는 *미주입*. 사람이 명시
    `approve`로 활성 skills/<name>/로 옮길 때만 일반 apply_builder 주입 대상이 된다(opt-in).
  - **provenance**: 후보 frontmatter에 status(candidate/approved)·source_run·learned_date 기록.
  - **적대 분리 = 자기학습 안전망**: 학습 스킬도 빌더-측(apply_builder)일 뿐 judge/run-judge
    무수신. 독립 적대 gate가 backstop이라 *나쁜 학습 스킬도 나쁜 산출물을 통과시킬 수 없다*
    (자기학습이 표류해도 검증은 독립). 학습은 빌더를 *돕기만*.
  - **품질/IP 가드**: 추출 프롬프트가 패턴(구현 아님)·자가채점/바완화 금지·로직-렌더 분리·
    원본(IP 클론 금지)을 강제하고, lint가 결정적으로 재검증한다.

이 모듈은 추출(extract_candidate)·검증(lint_candidate)·staging(write_candidate)·
승인(approve_candidate)·열람(list_candidates)을 제공하고, `python -m haetae.skill_learner`
CLI(--from-run/--approve/--list)로 사람이 구동한다. 추출 외엔 LLM 불필요(승인/열람은 순수).
"""

from __future__ import annotations

import argparse
import re
import shutil
from pathlib import Path

import yaml

from haetae.skills import SKILL_FILENAME, _split_frontmatter

CANDIDATES_DIRNAME = "_candidates"  # 비활성 staging(skills.load_skills가 `_` 접두로 제외)

# 추출 시스템 프롬프트 — 가드를 *명시*로 강제(패턴/원본/분리/비-치팅). 빌더 가이드용 SKILL.md 생성.
LEARN_PROMPT = """\
너는 *완주한(done)* 빌드의 승리 해법에서 **재사용 가능한 패턴**을 추출해 빌더 가이드
SKILL.md 후보를 만든다. 엄격한 규칙:

1. **패턴이지 구현이 아니다**: 접근/구조/원칙을 적어라. 코드를 통째로 복붙하지 마라(긴 코드
   블록 금지). 다른 주문에도 옮겨갈 수 있는 *방법*을 적어라.
2. **원본·IP 클론 금지**: 특정 제품/브랜드/저작물을 모사하지 마라. 일반화된 기법만.
3. **자가채점·바 완화 금지**: "스스로 pass 도장"·"검증/테스트 건너뛰기"·"기준 낮추기" 류
   문구 금지. 이 스킬은 *빌더 가이드*일 뿐 — 채점은 독립 적대 gate가 한다(절대 언급/완화 금지).
4. **로직-렌더 분리** 같은 검증가능 구조를 권장하라(엔진을 렌더에서 분리 등).
5. 출력은 **SKILL.md 한 장**: frontmatter(`name`, `triggers`[매칭 키워드]) + 본문(패턴 설명).
   provenance(출처 run/날짜/상태)는 *시스템이 채우니 너는 적지 마라*.
"""

# lint: 자가채점 스탬프(빌더가 스스로 통과 선언) — 정상 패턴 산문엔 없는 명백한 토큰만(오탐 방지).
_SELF_GRADE_STAMPS = (
    "pass:true", "pass: true", '"pass":true', '"pass": true',
    "passed=true", "passed = true", "self.pass", "passed: true",
)
# lint: 긴 코드 블록(구현 복붙) 임계 — 패턴이 아니라 구현 덤프로 간주.
_IMPL_CODEBLOCK_THRESHOLD = 1500


class SkillLearnError(Exception):
    """학습/승인 실패(후보 없음·lint 차단·이름 충돌 등)."""


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return s or "candidate"


def extract_candidate(
    client,
    *,
    spec_summary: str,
    solution_summary: str,
    scenarios: str = "",
    run_id: str,
    learned_date: str,
) -> str:
    """완주 run 입력(spec/해법/시나리오)에서 후보 SKILL.md 텍스트를 추출(경계된 LLM 1패스).

    LLM이 패턴 본문 + triggers를 주고, **provenance(status=candidate·source_run·learned_date)는
    *시스템이* frontmatter에 강제 주입**한다(LLM에 맡기지 않음 — 거버넌스 보장). 반환은 최종 텍스트.
    """
    user = (
        f"# 완주 spec 요약\n{spec_summary}\n\n"
        f"# 승리 해법 요약\n{solution_summary}\n\n"
        f"# 통과 시나리오\n{scenarios}\n\n"
        "위에서 재사용 패턴을 뽑아 SKILL.md 후보를 출력하라(규칙 준수)."
    )
    raw = client.complete(LEARN_PROMPT, user)
    return _stamp_provenance(raw, run_id=run_id, learned_date=learned_date, status="candidate")


def _stamp_provenance(text: str, *, run_id: str, learned_date: str, status: str) -> str:
    """SKILL.md frontmatter에 provenance(status·source_run·learned_date)를 *강제* 주입/갱신.

    LLM이 준 name/triggers/body는 보존하고 provenance 키만 시스템 값으로 덮어쓴다(거버넌스 권위).
    frontmatter가 없으면 최소 frontmatter를 만든다(triggers는 lint가 따로 강제).
    """
    fm, body = _split_frontmatter(text)
    meta = {}
    if fm is not None:
        try:
            loaded = yaml.safe_load(fm)
            if isinstance(loaded, dict):
                meta = loaded
        except Exception:
            meta = {}
    meta["status"] = status
    meta["source_run"] = run_id
    meta["learned_date"] = learned_date
    fm_out = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip()
    return f"---\n{fm_out}\n---\n{body.strip()}\n"


def lint_candidate(text: str) -> tuple[bool, list[str]]:
    """후보 SKILL.md를 결정적으로 검증. (ok, reasons). 거버넌스/품질 가드 재검증.

    차단 사유: provenance(status+source_run) 누락 · triggers 누락 · 자가채점 스탬프 ·
    긴 코드블록(구현 덤프, 패턴 아님). 통과해야 approve 가능.
    """
    reasons: list[str] = []
    fm, body = _split_frontmatter(text)
    meta: dict = {}
    if fm is not None:
        try:
            loaded = yaml.safe_load(fm)
            if isinstance(loaded, dict):
                meta = loaded
        except Exception:
            meta = {}
    # provenance
    if not meta.get("status") or not meta.get("source_run"):
        reasons.append("provenance 누락(status/source_run)")
    # triggers
    trg = meta.get("triggers")
    if not trg or (isinstance(trg, list) and not [t for t in trg if str(t).strip()]):
        reasons.append("triggers 누락(매칭 불가)")
    low = text.lower()
    # 자가채점 스탬프(바 완화/치팅)
    if any(stamp in low.replace(" ", "") or stamp in low for stamp in _SELF_GRADE_STAMPS):
        reasons.append("자가채점/바완화 문구(독립 gate 침해)")
    # 구현 덤프(패턴 아님) — 긴 코드 펜스
    for m in re.finditer(r"```.*?```", body, flags=re.DOTALL):
        if len(m.group(0)) > _IMPL_CODEBLOCK_THRESHOLD:
            reasons.append("긴 코드블록(구현 덤프 — 패턴 아님)")
            break
    return (not reasons, reasons)


def candidates_dir(skills_dir: str | Path) -> Path:
    return Path(skills_dir) / CANDIDATES_DIRNAME


def write_candidate(skills_dir: str | Path, name: str, text: str) -> Path:
    """후보 SKILL.md를 staging(skills/_candidates/<name>/)에 기록. **활성 아님**(로더가 제외).

    이 경로는 skills.load_skills에서 `_` 접두로 제외되므로 *승인 전엔 절대 주입되지 않는다*.
    """
    slug = _slugify(name)
    d = candidates_dir(skills_dir) / slug
    d.mkdir(parents=True, exist_ok=True)
    p = d / SKILL_FILENAME
    p.write_text(text, encoding="utf-8")
    return p


def list_candidates(skills_dir: str | Path) -> list[str]:
    """staging의 후보 이름 목록(정렬). 순수 — LLM 불필요."""
    base = candidates_dir(skills_dir)
    if not base.is_dir():
        return []
    return sorted(
        sub.name for sub in base.iterdir()
        if sub.is_dir() and (sub / SKILL_FILENAME).is_file()
    )


def approve_candidate(skills_dir: str | Path, name: str) -> Path:
    """**사람 명시 승인** — 후보를 lint 통과 시에만 staging→활성 skills/<name>/로 편입.

    승인 후에야 skills.load_skills가 로드하고 apply_builder가 주입한다. lint 실패/이름 충돌/
    후보 부재면 SkillLearnError(자동·암묵 채택 경로 없음). status를 approved로 갱신.
    """
    slug = _slugify(name)
    src = candidates_dir(skills_dir) / slug / SKILL_FILENAME
    if not src.is_file():
        raise SkillLearnError(f"후보 없음: {slug} (staging에 SKILL.md 부재)")
    text = src.read_text(encoding="utf-8")
    ok, reasons = lint_candidate(text)
    if not ok:
        raise SkillLearnError(f"lint 차단({slug}): {', '.join(reasons)}")
    dest_dir = Path(skills_dir) / slug
    if dest_dir.exists():
        raise SkillLearnError(f"이름 충돌: 활성 skills/{slug} 이미 존재(수동 검토 필요)")
    approved = _stamp_status(text, "approved")
    dest_dir.mkdir(parents=True, exist_ok=True)
    (dest_dir / SKILL_FILENAME).write_text(approved, encoding="utf-8")
    # staging 후보 제거(승인됨 — 활성으로 이동 완료)
    shutil.rmtree(candidates_dir(skills_dir) / slug, ignore_errors=True)
    return dest_dir / SKILL_FILENAME


def _stamp_status(text: str, status: str) -> str:
    fm, body = _split_frontmatter(text)
    meta: dict = {}
    if fm is not None:
        try:
            loaded = yaml.safe_load(fm)
            if isinstance(loaded, dict):
                meta = loaded
        except Exception:
            meta = {}
    meta["status"] = status
    fm_out = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip()
    return f"---\n{fm_out}\n---\n{body.strip()}\n"


# ──────────────────────────── CLI (사람이 구동 — opt-in) ────────────────────────────


def _spec_summary_from_run(run_dir: Path) -> tuple[str, str]:
    """완주 run-dir에서 spec/해법 요약 텍스트를 best-effort로 모은다(spec.yaml 사이드카 #58)."""
    spec_p = run_dir / "spec.yaml"
    spec_text = spec_p.read_text(encoding="utf-8") if spec_p.is_file() else ""
    # 해법 요약은 state.yaml의 plan/done + (있으면) artifacts; 여기선 spec goal/decomposition 위주.
    return spec_text[:6000], "완주(done) — 통과한 유닛/통합 기준 참고(상세는 run-dir)."


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="haetae learn-skill",
        description="완주 캡스톤서 후보 스킬 추출/승인 (F.1 거버넌스 — 자동채택 0·사람 승인).",
    )
    parser.add_argument("--skills-dir", default="skills", help="스킬 레지스트리 루트(기본 skills/)")
    parser.add_argument("--from-run", default=None, help="완주 run-dir에서 후보 추출")
    parser.add_argument("--name", default=None, help="후보 이름(미지정 시 run-dir 이름)")
    parser.add_argument("--approve", default=None, help="후보를 검토 후 활성 편입(사람 승인)")
    parser.add_argument("--list", action="store_true", help="staging 후보 열람")
    args = parser.parse_args(argv)

    if args.list:
        for c in list_candidates(args.skills_dir):
            print(f"candidate: {c}")
        return 0
    if args.approve:
        dest = approve_candidate(args.skills_dir, args.approve)
        print(f"approved → {dest}")
        return 0
    if args.from_run:
        run_dir = Path(args.from_run)
        spec_summary, solution_summary = _spec_summary_from_run(run_dir)
        # 추출엔 LLM이 필요 — director가 검토하므로 여기선 codex 클라이언트를 지연 구성.
        from haetae.llm import CodexClient  # 지연 import(테스트 무관)

        name = args.name or run_dir.name
        text = extract_candidate(
            CodexClient(),
            spec_summary=spec_summary,
            solution_summary=solution_summary,
            run_id=run_dir.name,
            learned_date="",  # CLI는 날짜 미스탬프(결정성)·director가 frontmatter 보강 가능
        )
        p = write_candidate(args.skills_dir, name, text)
        print(f"candidate staged → {p}  (활성 아님 — `--approve {_slugify(name)}` 로 승인)")
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
