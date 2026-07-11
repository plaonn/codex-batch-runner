# Review Contract

이 문서는 review bundle, review-next, reviewer Codex, mechanical gates, bounded auto-fix loop의 public contract를 정의합니다. Task review 상태 필드는 [task-schema.md](task-schema.md)를 참고하십시오.

## Automatic review bundle and reviewer Codex

규칙만으로 `completed` task를 자동 accept하는 방식은 충분하지 않습니다. 파일 변경, 테스트 명령, commit/push 상태 같은 기계적 신호는 누락과 모순을 찾는 데 유용하지만, 원래 prompt 의도 충족 여부, 문서/코드 변경의 적절성, 공개 저장소 안전 정책 준수 여부, 후속 작업 필요성은 task마다 문맥 판단이 필요합니다. 따라서 review는 아래 단계로 분리합니다.

- Mechanical gates: task 상태, dependency 상태, final JSON schema, verification 유무, git dirty/unpushed 상태, diff 크기, 금지된 runtime/private 파일 포함 여부 같은 결정적 검사를 수행합니다.
- Narrow mechanical safe-accept: mechanical auto-accept와 Reviewer Codex가 모두 켜져 있어도, 모든 mechanical gate가 통과하고 tracked/public diff가 없으며 reported changes가 ignored operator-local path(`.private/`, `*.local.md` 등)로만 제한되고 clean tracked-state verification이 있는 경우에는 Reviewer Codex 호출 없이 accept할 수 있습니다. 이 shortcut은 semantic tracked/public code/docs diff에는 적용하지 않습니다.
- Reviewer Codex: 독립적으로 생성한 review bundle만 읽고 작업 결과를 평가합니다. 현재 대화 context나 작업 실행 thread 기억에 의존하지 않습니다.
- Human fallback: confidence가 낮거나 private/public 안전성, 의도 충족, 큰 diff, 실패한 검증, credential 가능성처럼 사람이 봐야 하는 항목이 있으면 accept하지 않고 확인 대상으로 남깁니다.

Reviewer Codex는 선택 기능이며 기본값은 비활성화입니다. 토큰을 소비하고 실행 thread의 전체 대화 context를 갖지 못할 수 있으므로, local mechanical review와 human review fallback이 안정적으로 동작하는 것을 전제로 별도 opt-in해야 합니다. 안전 모델은 “호출하지 않는 것이 기본이며, 호출하더라도 한 번의 runner 실행 안에서 작고 감사 가능한 판단만 수행한다”는 원칙을 따릅니다.

## Review outcome evidence and evaluation boundaries

`review_outcome_evidence_history`는 task의 기존 `review_status`, reviewer metadata,
또는 `execution_evidence_history`를 재작성하지 않는 append-only supplemental
history다. 현재 contract는 `review-outcome-evidence-v1`이며, 각 record는 아래를
분리해 보관한다.

- `acceptance.method`: `mechanical_safe`, `reviewer_pass`, `human_accept`,
  `external_review`, `none` 중 하나. 서로 다른 method의 acceptance는 합산하거나
  단일 acceptance rate로 계산하지 않는다.
- `objective_verification`: deterministic check 결과와 `semantic_review` 판단을
  별도 field로 둔다. verification 통과만으로 semantic acceptance를 뜻하지 않는다.
- reviewer `kind`, `role`, `decision_confidence`와 `actual_identity`를 분리한다.
  actual identity는 provider/wrapper가 관측한 source와 matching confidence가 있을
  때만 `observed`가 될 수 있다. planned model, role, alias, self-claim에서 identity를
  추론하지 않으며, 그렇지 않으면 `unknown`이다.

Routing evaluation은 quality sample을 같은 task bucket, execution cohort, outcome
contract version, review policy version, rubric version, acceptance method, reviewer
provenance class 안에서만 비교한다. Worker cell마다 matched anchor semantic-review
coverage를 report하며, anchor가 없거나 cohort가 mismatch이면 quality rate는
non-comparable로 표시한다. v1은 coverage를 관찰할 뿐 numerical threshold를 적용하지
않는다. 기존 task metadata만 가진 legacy row는 `legacy-review-unknown`으로 dual-read
되며 quality-rate 및 anchor-coverage numerator/denominator에서 제외된다.

Review bundle은 특정 task의 결과를 재검토하기 위한 self-contained artifact입니다. 생성 시점의 현재 대화 context, Codex transcript 전체, operator 개인 메모에 의존하지 않고, task JSON과 대상 git repository의 현재 local state에서 다시 만들 수 있어야 합니다. bundle은 기본적으로 report-only 입력이며, 첫 구현은 파일 저장 또는 stdout 출력만 수행하고 review status를 변경하지 않습니다.

필수 입력:

- task prompt: task에 저장된 prompt와, `needs_resume` 완료인 경우 관련 `next_prompt` 요약
- task metadata: id, status, review_status, cwd, project_root, project_id, category, labels, created_by, attempts, timestamps
- dependencies: `depends_on` id와 각 dependency의 status/review_status 요약
- `last_result`: status, summary, next_prompt, changed_files, verification, optional `commits`, optional `push_status`
- `last_run`: command_kind, returncode, started/finished time, duration_seconds, resume_id_used 존재 여부, log path 존재 여부
- changed files: `last_result.changed_files`와 git diff/name-status에서 확인한 변경 파일 목록
- verification: Codex가 보고한 검증 명령과 결과 요약. 필요하면 reviewer가 재실행할 명령을 제안할 수 있으나 bundle 생성 단계에서 임의 실행하지 않음
- git status: completion-time `task_git_status_snapshot`과 review-time `current_git_repository` state를 구분함. Snapshot은 runner가 task 완료 시 저장한 branch, upstream/comparison ref, ahead/behind, dirty 여부, unpushed commit 요약, warnings이며, current state는 review 시점의 local repository head/dirty/ahead/behind/unpushed 상태임
- commit data: 관련 commit hash, 짧은 subject/stat, 필요한 경우 sanitized diff. commit/push metadata는 `cbr-result-push-metadata`에서 저장한 optional result fields와 task `git_status`를 함께 사용함
- relevant docs/spec excerpts: README, `docs/spec.md`, examples, public policy 문서가 변경된 경우 해당 주변 문단의 짧은 excerpt
- public/private safety policy: 공개 repo에 commit하면 안 되는 runtime state, 실제 logs/prompts/session ids/thread ids, credentials, 개인 경로, Telegram token/chat id, private queue contents 금지 규칙

Bundle에 기본 포함하지 않는 정보:

- raw private logs 또는 전체 JSONL transcript
- 전체 대화 transcript
- credentials, tokens, chat ids, 개인 계정 식별자
- session id/thread id 원문. 필요한 경우 존재 여부만 표시하거나 sanitized placeholder 사용
- `.codex-batch-runner/` runtime state contents, 실제 queue contents, `.private/` contents, operator-local `*.local.md` 세부 내용

Reviewer Codex가 받을 수 있는 context는 review bundle, sanitized prompt/result, commit diff/stat, verification summary로 제한합니다. Raw log, raw transcript, secret, credential, session id, thread id, 개인 절대 경로는 기본 입력에서 제외합니다. Reviewer가 원래 실행 대화의 숨은 의도나 중간 합의를 모르는 위험은 task prompt, `next_prompt` 요약, `last_result`, changed files, verification, git snapshot/current state, commit ancestry, safety policy를 한 묶음으로 제공해 줄입니다. 보고된 task commit이 현재 `HEAD`와 같으면 `equal`, 현재 `HEAD`의 ancestor이면 `ancestor`로 표시합니다. `ancestor`는 후속 commit이 위에 쌓인 정상 상태이므로 단독으로 mismatch로 보지 않습니다. 보고된 commit이 현재 `HEAD`에서 도달 불가능하면 `not_reachable`로 표시하고 human check 대상으로 남깁니다. 그래도 bundle만으로 의도를 재구성할 수 없으면 reviewer는 통과 결정을 내리지 말고 `needs_human`을 반환해야 합니다.

Reviewer Codex 호출 허용 조건:

- config `auto_review_codex_enabled=true`가 명시되어 있어야 합니다.
- `auto_review_codex_max_calls_per_run`이 1 이상이어야 하며, 한 번의 `run-next` 또는 `review-next --apply` 실행에서 이 한도를 넘기면 안 됩니다.
- 대상 task가 `status=completed`이고 `review_status`가 `unreviewed`, `rejected`, `needs_followup` 중 하나여야 합니다.
- Mechanical gates가 reviewer 호출 전 단계까지 치명적 오류 없이 통과해야 합니다. 예를 들어 final result 누락, verification 누락, 공개 금지 파일 의심, dirty/unpushed 상태 모호성, dependency 미충족, 보고 commit이 현재 `HEAD`에서 도달 불가능한 상태는 reviewer 호출 없이 human review로 남길 수 있습니다.
- Global cooldown 또는 reviewer 전용 cooldown이 활성 상태가 아니어야 합니다.
- Review bundle 크기와 diff 크기가 configured limit 안에 있어야 합니다. 초과하면 bundle을 임의로 크게 잘라 자동 판단하지 않고 `needs_human`으로 남깁니다.
- 예외적으로 narrow mechanical safe-accept class는 Reviewer Codex 입력이 필요 없으므로 bundle/diff 크기 제한 때문에 `needs_human`으로 전환하지 않고 mechanical accept를 먼저 적용할 수 있습니다.
- Large semantic diff가 configured bundle/diff limit을 넘으면 자동 `pass`를 허용하지 않고 human review로 남깁니다.

Reviewer Codex 호출 금지 조건:

- 명시 opt-in이 없거나 `auto_review_codex_max_calls_per_run=0`인 경우
- task가 실행 중이거나 stale state check가 실패한 경우
- raw log/transcript, credential, token, session id, thread id, private queue contents 없이는 판단할 수 없는 경우
- 공개 저장소 안전 위반 가능성이 감지된 경우
- rate-limit/usage-limit evidence, global cooldown, reviewer cooldown, lock contention이 있는 경우
- 이미 같은 task에서 허용된 fix loop 한도를 사용한 경우
- reviewer 응답 schema가 invalid하거나 confidence가 낮거나 결정 근거가 비어 있는 경우

Opt-in placeholder config:

```json
{
  "auto_review_codex_enabled": false,
  "auto_review_codex_max_calls_per_run": 0,
  "auto_review_codex_max_fix_loops_per_task": 0,
  "auto_review_codex_cooldown_seconds": 1800,
  "auto_review_codex_max_bundle_chars": 120000,
  "auto_review_codex_max_diff_chars": 60000
}
```

`auto_review_codex_enabled=false`와 `auto_review_codex_max_calls_per_run=0`은 reviewer Codex 호출이 불가능한 기본값입니다. `auto_review_codex_max_fix_loops_per_task=0`은 reviewer가 후속 수정 필요성을 발견해도 runner가 자동 follow-up 실행 loop를 시작하지 않는다는 뜻입니다. Reviewer Codex 호출 경로를 사용하려면 config opt-in 또는 command opt-in과 호출 한도, cooldown, bundle 크기 제한을 모두 통과해야 합니다.

Reviewer Codex decision schema:

```json
{
  "task_id": "string",
  "decision": "pass | needs_fix | needs_human | failed_review",
  "confidence": "low | medium | high",
  "reason": "string",
  "findings": [
    {
      "severity": "info | warning | error",
      "summary": "string",
      "evidence": "string"
    }
  ],
  "required_human_checks": ["string"],
  "auto_fix_allowed": false,
  "auto_fix_risk": "low | medium | high",
  "suggested_fix_prompt": "string",
  "finding_fingerprints": ["string"],
  "reviewer_limits": {
    "calls_used_this_run": 1,
    "fix_loops_used_for_task": 0,
    "cooldown_recommended_seconds": 0
  }
}
```

Decision 의미:

- `pass`: bundle만으로 prompt 충족, 검증, 공개 안전 정책을 high confidence로 확인했습니다.
- `needs_fix`: 기본 방향은 맞지만 추가 수정이 필요합니다. 이 경우 `suggested_fix_prompt`를 구체적으로 작성합니다.
- `needs_human`: 자동 판단에는 정보가 부족하거나 사람이 봐야 할 위험이 있습니다.
- `failed_review`: reviewer 호출 자체가 실패했거나 schema 검증, rate-limit, timeout, cooldown, bundle 해석에 실패했습니다.

`auto_fix_allowed`, `auto_fix_risk`, `suggested_fix_prompt`, `finding_fingerprints`, `reviewer_limits`는 bounded automatic review-fix loop를 위한 optional field입니다. 기존 reviewer result가 이 필드를 생략해도 유효하며, 누락 또는 모호한 값은 자동 fix enqueue 금지로 해석합니다.

자동 accept 조건은 보수적으로 제한합니다. Mechanical gates가 모두 통과하고, stale state 재확인이 통과하고, reviewer decision이 `pass`이며 `confidence=high`이고, findings에 `error`가 없고, required human check가 비어 있을 때만 accepted 반영 후보가 될 수 있습니다. Reviewer-backed auto-apply는 별도 config 또는 CLI로 명시적으로 켠 뒤에만 허용합니다.

후속 수정 조건은 `needs_fix` decision, `confidence=high`, `auto_fix_allowed=true`, `auto_fix_risk=low`, 구체적인 `suggested_fix_prompt`, 남은 `auto_review_codex_max_fix_loops_per_task`, fresh state 재확인이 모두 있을 때에만 자동 loop 후보가 됩니다. Fix loop 한도가 0이면 항상 human-visible pending state로 남깁니다.

Human escalation 조건은 넓게 잡습니다. Reviewer decision이 `needs_human` 또는 `failed_review`인 경우, confidence가 low/medium인 `pass`, required human check 존재, 공개/비공개 안전 의심, verification 실패/누락, 큰 diff, ambiguous commit inference, 보고 commit이 현재 `HEAD`에서 도달 불가능한 상태, stale repository state, rate-limit/cooldown, schema invalid response는 모두 자동 accept하지 않습니다.

Token, loop, rate-limit, cooldown safeguards:

- Reviewer Codex 호출은 runner queue lock 아래에서 한 번에 하나의 task만 다룹니다.
- 한 번의 runner 실행당 reviewer 호출 수는 `auto_review_codex_max_calls_per_run`으로 제한하고 기본값은 0입니다.
- task별 자동 fix loop는 `auto_review_codex_max_fix_loops_per_task`로 제한하고 기본값은 0입니다.
- 한 runner invocation은 reviewer 대상 한 건만 처리하고, 자동 fix도 최대 한 건만 enqueue합니다. 이 경로는 별도 count option 없이 고정 정책으로 제한합니다.
- Reviewer 호출에서 rate-limit 또는 usage-limit evidence가 나오면 sanitized event만 기록하고 `auto_review_codex_cooldown_seconds` 또는 global cooldown 중 더 보수적인 값을 적용합니다.
- Reviewer timeout, invalid JSON, schema mismatch, empty reason은 retry loop를 만들지 않고 `failed_review` 또는 `needs_human`으로 종료합니다.
- Bundle/diff size limit을 넘으면 truncation된 내용으로 pass를 허용하지 않고 human review로 남깁니다.
- Reviewer Codex는 follow-up task를 직접 enqueue하지 않습니다. `needs_fix`는 sanitized finding과 suggested prompt만 반환하며, 모든 opt-in gate를 통과한 경우에만 runner control-plane이 별도 일반 cbr fix task를 생성합니다.

구현은 dry-run/report-only planner, local-only auto-accept, optional reviewer-backed auto-accept, optional bounded auto-fix enqueue를 분리합니다. `cbr review-next` 또는 `cbr review-next --dry-run`은 다음 검토 대상과 mechanical gate 근거를 출력하되 `review_status`를 바꾸거나 follow-up task를 만들지 않습니다. `cbr review-next --apply`는 runner와 같은 queue lock 아래에서만 실행하며, 기본값은 적용 거부와 `needs_human` 보고입니다. `--mechanical-auto-accept` 또는 config `auto_review_mechanical_accept=true`가 명시되고 모든 mechanical gate가 통과할 때만 reviewer Codex 호출 없이 `review_status=accepted`를 적용할 수 있습니다. `--reviewer-codex` 또는 config `auto_review_codex_enabled=true`와 `auto_review_codex_max_calls_per_run >= 1`이 명시되고 모든 guardrail이 통과하면 reviewer Codex를 한 번 호출할 수 있습니다. 같은 config가 켜져 있으면 `run-next`도 같은 lock 안에서 최대 한 건의 auto-review pass를 구현 task보다 먼저 시도할 수 있습니다. 자동 검토가 task를 accept하거나 reviewer Codex를 호출해 검토 작업을 소비한 경우 같은 invocation에서 구현 task를 시작하지 않습니다. Reviewer Codex가 `needs_human`, `failed_review`, 또는 자동 follow-up으로 이어질 수 없는 `needs_fix`를 반환하면 현재 review fingerprint를 포함한 backoff marker를 저장합니다. 이후 `run-next` 자동 검토는 task/result/git 상태가 바뀌지 않은 같은 후보를 다시 reviewer Codex에 보내지 않고 다음 검토 후보나 runnable 구현 작업으로 넘어갈 수 있습니다. Gate 실패처럼 task 상태를 변경하지 않는 비실행 가능한 검토 후보만 있으면 starvation guard로 runnable 구현 task 선택을 계속 진행할 수 있습니다. 모든 auto-fix gate를 통과한 `needs_fix`는 bounded prompt를 가진 별도 fix task를 enqueue하고, 같은 invocation에서는 그 fix task를 실행하지 않습니다.

`completed + needs_followup`은 cleanup candidate가 아니라 operator action item입니다. `cbr list`, `cbr summary`, `cbr review-next`는 follow-up action report를 표시합니다. 연결된 follow-up task가 없으면 follow-up work를 만들거나 기존 fix task를 연결해야 하며, 더 이상 follow-up이 필요 없으면 `resolve`로 `manual`, `superseded`, `wont_fix`, `duplicate` 중 명시 결정을 기록해야 합니다. 연결된 follow-up task가 active이면 실행/대기 상태를 관찰하고, completed but unreviewed이면 follow-up task를 review하고, accepted이면 원 `needs_followup` task를 `superseded` 또는 `manual` 등으로 resolve할지 판단합니다. 이 report는 stale row를 줄이기 위한 read-only guidance이며 자동 cleanup 근거가 되지 않습니다.


## Bounded automatic review-fix loop

이 section은 reviewer Codex 자동 검토 이후의 bounded auto-fix loop 설계와 현재 구현 기준입니다. Reviewer가 직접 파일을 수정하지 않는 원칙을 유지하면서, 수정 범위가 작고 명확한 경우에만 runner가 별도 fix task를 제한적으로 enqueue하고 다시 review합니다. 기본값은 disabled이며, 명시적 config gate와 reviewer gate가 모두 통과해야 합니다.

기본 workflow:

1. Implementation task 실행: 원 task가 `runnable` 또는 `needs_resume`으로 실행되고 `completed + unreviewed` 상태가 됩니다.
2. Mechanical review: final JSON, verification, changed files, dependency readiness, git cleanliness, public/private safety policy, stale state를 결정적 gate로 검사합니다.
3. Reviewer Codex review: sanitized review bundle만 입력으로 받아 structured findings를 반환합니다. Reviewer Codex는 review phase에서 파일을 수정하거나 queue를 직접 변경하지 않습니다.
4. Accept 또는 escalation: reviewer decision이 high-confidence `pass`이고 모든 gate가 통과하면 `accepted`가 될 수 있습니다. `needs_human`, `failed_review`, high-risk blocker, stale state, limit 초과는 자동 loop를 중단합니다.
5. Needs-fix auto enqueue: reviewer decision이 `needs_fix`이고 `auto_fix_allowed=true`이며 confidence/risk/limit gate가 모두 통과한 경우에만 runner가 별도 fix task를 생성합니다.
6. Fix task 실행: fix task는 원 task의 child로 실행되며 reviewer의 bounded fix prompt만 수행합니다.
7. Review again: fix task 완료 후 같은 mechanical review와 reviewer Codex review를 다시 수행합니다. cycle limit 안에서 pass하면 chain을 `accepted`로 닫고, 다시 `needs_fix`가 나오면 limit과 repeated finding gate를 먼저 확인합니다.

상태 label은 실행 status와 review metadata를 조합해 표시합니다.

- `awaiting_review`: implementation 또는 fix task가 완료되어 review 대기 중입니다.
- `reviewing`: runner가 queue lock 아래에서 mechanical review 또는 reviewer Codex call을 수행 중입니다.
- `needs_fix`: reviewer가 자동 또는 수동 follow-up 수정 필요를 판단했습니다.
- `fixing`: 자동 생성된 fix task가 실행 중이거나 실행 후보입니다.
- `accepted`: chain의 최신 결과가 review gate를 통과했습니다.
- `needs_human`: 자동 판단 또는 자동 수정에 필요한 조건이 부족합니다.
- `loop_limit_reached`: cycle, Codex call, wall time, repeated finding 중 하나의 hard limit에 도달했습니다.

Task chain metadata는 원 task와 fix task 모두에 저장할 수 있어야 합니다. 기존 task schema와 호환되도록 모두 optional field로 시작합니다.

- `root_task_id`: review/fix chain의 최초 implementation task id입니다. 원 task에서는 자기 id입니다.
- `parent_task_id`: 현재 task를 만든 직전 task id입니다. 원 task에서는 `null`입니다.
- `review_cycle`: implementation 결과를 cycle 0으로 보고, fix task가 생성될 때마다 1씩 증가합니다.
- `review_attempts`: 현재 chain에서 reviewer Codex review를 시도한 횟수입니다.
- `fix_attempts`: 현재 chain에서 자동 fix task를 생성한 횟수입니다.
- `chain_status`: `awaiting_review`, `reviewing`, `needs_fix`, `fixing`, `accepted`, `needs_human`, `loop_limit_reached` 중 하나입니다.
- `review_findings`: sanitized reviewer finding 요약입니다. raw transcript, raw log, secret, session id, thread id는 저장하지 않습니다.
- `last_review_decision`: 최신 reviewer decision입니다.
- `auto_fix_allowed`: reviewer가 fix task 생성을 허용한다고 명시했는지 나타냅니다. 기본값은 `false`입니다.
- `auto_fix_budget`: 현재 chain의 남은 fix budget과 limit snapshot입니다. 예: `max_cycles`, `max_fix_attempts`, `max_codex_calls`, `deadline_at`, `remaining_fix_attempts`.
- `last_auto_fix_task_id`: 자동 생성된 최신 fix task id입니다.
- `finding_fingerprints`: 반복 finding 감지를 위한 normalized finding hash 목록입니다.

Reviewer Codex result schema는 아래 field를 사용할 수 있습니다. 기존 reviewer result를 읽는 코드는 field가 없으면 보수적으로 `false` 또는 `null`로 해석해야 합니다.

```json
{
  "task_id": "string",
  "decision": "pass | needs_fix | needs_human | failed_review",
  "confidence": "low | medium | high",
  "reason": "string",
  "findings": [
    {
      "severity": "info | warning | error",
      "summary": "string",
      "evidence": "string",
      "fingerprint": "string optional"
    }
  ],
  "required_human_checks": ["string"],
  "suggested_fix_prompt": "string",
  "auto_fix_allowed": false,
  "auto_fix_risk": "low | medium | high",
  "reviewer_limits": {
    "calls_used_this_run": 1,
    "fix_loops_used_for_task": 0,
    "cooldown_recommended_seconds": 0
  }
}
```

Auto-fix enqueue는 모든 조건이 동시에 충족될 때만 허용합니다.

- Config에서 `auto_review_codex_max_fix_loops_per_task >= 1`이 명시되어 있습니다. 기본값은 disabled입니다.
- Reviewer decision이 `needs_fix`입니다.
- `auto_fix_allowed=true`입니다.
- Confidence가 `high`입니다.
- `auto_fix_risk=low`입니다.
- `suggested_fix_prompt`가 구체적이고 bounded합니다.
- Mechanical gates가 fatal blocker 없이 통과했으며 stale state 재확인이 통과했습니다.
- `auto_review_codex_max_fix_loops_per_task`, chain-level `max_cycles`, `max_codex_calls`, `deadline_at`의 남은 예산이 있습니다.
- Finding fingerprint가 같은 chain에서 반복 실패로 판정되지 않았습니다.
- Global cooldown, reviewer cooldown, rate-limit evidence, lock contention이 없습니다.

자동 fix task prompt는 reviewer의 `suggested_fix_prompt`를 그대로 신뢰하지 않고 runner가 wrapper를 붙여 제한합니다. Prompt에는 root/parent task id, review cycle, sanitized findings, 허용된 변경 범위, 요구 verification, 금지 항목, final JSON schema를 포함합니다. Fix task는 원칙적으로 parent task의 `cwd`, `project_id`, `category`, `labels`를 상속하지만 `depends_on`에는 parent를 넣지 않습니다. 대신 `subtask_type=auto_review_fix`, `subtask_for=<parent_task_id>`, `root_task_id`, `parent_task_id`, `blocks_root_completion=true` metadata로 root chain에 연결합니다. Parent/root task에는 `chain_status=fixing`, `last_auto_fix_task_id`, `blocking_subtask_ids`를 기록하여 root chain이 아직 완전히 accepted/applied 상태가 아님을 표시합니다. `dependency_requires_accepted_review=true`는 일반 `depends_on` 관계에만 적용되므로, 자동 fix subtask는 parent/root가 completed but unaccepted 상태여도 runnable 상태를 유지하고, 외부 dependent task는 기존대로 blocked 상태에 남습니다.

Hard limits:

- Max cycles: 기본 0, opt-in 시에도 초기 권장값은 1입니다. 2 이상은 별도 운영 판단이 필요합니다.
- Max Codex calls: 한 runner invocation과 한 chain 전체 모두에 별도 상한을 둡니다. Reviewer call과 fix task execution call을 모두 계산합니다.
- Max wall time/deadline: root task completion 또는 첫 review 시작 시점 기준 deadline을 저장하고, deadline이 지나면 `needs_human` 또는 `loop_limit_reached`로 종료합니다.
- Repeated same finding detection: finding `fingerprint` 또는 severity/summary/evidence normalized hash가 같은 chain에서 다시 나타나면 자동 fix를 중단합니다.
- Rate-limit/cooldown handling: rate-limit evidence가 있으면 해당 invocation에서 retry하지 않고 reviewer 또는 global cooldown을 기록합니다. Cooldown이 활성화된 동안 자동 fix enqueue를 수행하지 않습니다.
- Failure escalation: invalid reviewer schema, empty reason, missing fix prompt, fix task failure, `blocked_user`, `failed`, verification 누락, stale state, lock loss는 자동 loop를 중단하고 human review로 남깁니다.

High-risk blocker는 자동 fix를 금지하고 human review를 요구합니다.

- Destructive edit: 삭제, 대량 이동, history rewrite, cleanup, prune, reset, migration rollback처럼 되돌리기 어렵거나 범위가 큰 변경
- Auth/security: credential, token, 권한, signing, encryption, secret handling, network auth, access policy 변경
- Dependency upgrades: runtime dependency 추가/업그레이드, lockfile 대규모 변경, toolchain version 변경
- Migration: DB/schema/data migration, queue format migration, backward compatibility가 불명확한 schema 변경
- Broad public API change: CLI option 의미 변경, public task schema/status 의미 변경, README/spec의 사용자 계약 변경
- Product/policy ambiguity: reviewer가 의도, 정책, UX, 운영 판단을 bundle만으로 확정할 수 없는 경우
- Repeated identical failure: 같은 finding이나 같은 verification failure가 chain에서 반복되는 경우

Audit trail은 append-only event log와 task metadata 양쪽에 남깁니다. 저장하는 정보는 sanitized summary와 decision evidence로 제한합니다.

- `task_review_started`: review 대상, cycle, attempt, gate snapshot summary
- `task_reviewer_codex_reviewed`: decision, confidence, finding count, sanitized finding summaries, `auto_fix_allowed`, risk
- `task_auto_fix_enqueued`: root/parent/fix task id, cycle, budget snapshot, finding fingerprints, sanitized prompt summary
- `task_auto_fix_skipped`: skip reason, failed gate, limit, high-risk blocker
- `task_review_chain_closed`: final `chain_status`, accepted/needs_human/loop_limit reason

Event payload에는 raw private logs, full JSONL transcript, full prompt, credentials, token, session id, thread id, private queue contents, operator-local path를 넣지 않습니다. 필요한 경우 존재 여부, count, hash, sanitized excerpt만 저장합니다.

현재 bounded auto-fix는 explicit opt-in과 hard limit을 통과한 경우에만 separate fix task를 enqueue합니다. Fix task는 일반 `run-next`가 처리하며 reviewer phase는 직접 파일을 수정하지 않습니다. Enqueue와 skip 모두 sanitized event를 남기며 raw log, session id, thread id, full prompt는 event payload에 저장하지 않습니다. Fix task 완료 후 chain metadata를 갱신하고 다시 review candidate로 선택합니다. Repeated fingerprint, stale state, missing prompt, exceeded budget은 추가 enqueue 없이 human-visible pending state로 남깁니다. 운영자가 수동으로 판단해야 하는 영역은 high-risk 수정, 반복 finding, stale state, prompt가 불명확한 reviewer output, loop limit 초과, root chain의 blocking subtask 미해결 상태입니다. Worktree branch의 main 반영은 여전히 `cbr worktree apply` 같은 명시적 절차로만 수행합니다.
