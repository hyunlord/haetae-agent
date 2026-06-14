# skills/_candidates/ — 학습 스킬 staging (WO#103, OMC #4)

자동 학습된 **후보 스킬**(완주 캡스톤서 추출)이 여기 머문다. **비활성 staging**이다.

## 거버넌스 (F.1 정합 — 자동채택 0)
- `skills.load_skills`는 `_`-접두 디렉토리(이 폴더 포함)를 **로드에서 제외** → 후보는 활성 #32
  레지스트리에 **미편입·미주입**. 사람이 명시 승인하기 전엔 빌더가 절대 못 본다.
- 후보 추출:  `python -m haetae.skill_learner --from-run <run-dir>`
- 후보 열람:  `python -m haetae.skill_learner --list`
- **사람 승인**: `python -m haetae.skill_learner --approve <candidate>`
  → lint 통과 시에만 `skills/_candidates/<name>/` → 활성 `skills/<name>/`로 이동(status=approved).
  이후에야 일반 `apply_builder` 주입 대상이 된다(opt-in).

## 안전
- **적대 분리**: 학습 스킬도 빌더-측(apply_builder)일 뿐 judge/run-judge 무수신. 독립 적대 gate가
  backstop이라 *나쁜 학습 스킬도 나쁜 산출물을 통과시킬 수 없다*(자기학습 표류 안전망).
- **lint 가드**: provenance(status·source_run)·triggers 필수, 자가채점/바완화 스탬프 차단,
  긴 코드 덤프(구현, 패턴 아님) 차단.
- provenance는 후보 frontmatter(status·source_run·learned_date)에 기록된다.
