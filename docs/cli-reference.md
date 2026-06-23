# CLI Reference

이 문서는 cbr command surface와 human/JSON output semantics를 정의합니다. 설치와 scheduler 설정은 [operator-installation.md](operator-installation.md)를 참고하십시오.

## Operational triage plan

실운용에서 중앙 queue가 커지면 full transcript를 읽기 전에 저비용 triage가 가능해야 함.

구현:

- `cbr list`는 실행 대기 task와 검토 대기 task를 기본 표시합니다.
- `cbr list --all`은 accepted/archived까지 포함한 전체 목록을 표시합니다.
- `cbr summary TASK_ID`는 `last_result.summary`, changed files, verification, last_error를 transcript보다 짧게 보여줍니다.
- failed/blocked task와 completed rejected/needs_followup task에는 `resolution`을 기록해 `wont_fix`, `superseded`, `manual`, `smoke`, `duplicate` 같은 운영 결정을 남길 수 있습니다.

계획:

- `cbr list --verbose`는 summary의 핵심 정보를 목록 화면에 압축해서 보여줄 수 있습니다.
- 오래된 `accepted`/`archived` task와 로그는 추후 `cbr prune`으로 정리할 수 있게 합니다.
- 초기 `cbr prune`은 dry-run report를 기본값으로 두고, 명시적인 `--apply`가 있을 때만 삭제합니다.


## CLI

초기 CLI:

```bash
cbr enqueue --cwd /repo --prompt-file prompt.md
cbr enqueue --cwd /repo --prompt "작업 지시문"
cbr enqueue --cwd /repo --project project-id --category implementation --label queue --created-by operator --prompt-file prompt.md
cbr enqueue --cwd /repo --profile small --model gpt-5-small --codex-profile batch-small --config-override model_reasoning_effort=low --prompt-file prompt.md
cbr enqueue --cwd /repo --profile small --routing-size small --routing-risk low --verification-scope unit --verification-scope docs --prompt-file prompt.md
cbr list
cbr list --project project-id
cbr list --project-root /repo
cbr list --cwd /repo
cbr list --category implementation
cbr list --label queue
cbr list --verbose
cbr list --graph
cbr list --demo
cbr list --demo --graph
cbr run-next
cbr show TASK_ID
cbr summary TASK_ID
cbr routing-report --project project-id --json
cbr logs TASK_ID
cbr transcript TASK_ID
cbr archive TASK_ID
cbr accept TASK_ID --reason "verified"
cbr reject TASK_ID --reason "missing tests"
cbr resolve TASK_ID --resolution manual --reason "handled outside cbr"
cbr list --all
cbr list --unreviewed
cbr list --needs-review
cbr rate-limits
cbr cooldown show
cbr cooldown set 7:6
cbr cooldown set "6/22 7:06"
cbr cooldown set +2h30m
cbr cooldown clear
cbr pause show
cbr pause set --reason "operator maintenance window"
cbr pause clear
cbr events
cbr events --task-id TASK_ID --limit 20
cbr events --json
cbr doctor
cbr doctor --json
cbr prune
cbr prune --older-than-days 60 --json
cbr prune --apply
```

공통 option:

```bash
--config path/to/config.json
```

config 탐색 순서:

1. `--config` 명시값
2. `CBR_CONFIG` 환경 변수
3. `~/.config/codex-batch-runner/config.json`
4. 현재 작업 디렉터리 기준 기본값

자동화와 launchd에서는 명시적 config 또는 절대 경로 기반 호출을 권장함. 운영자가 직접 viewer/review 용도로 사용할 때는 `cbr` console script를 설치하고 사용자 config 자동 탐색을 활용할 수 있음.

`cbr list` 기본 출력은 운영자가 신경 써야 할 task 중심으로 유지합니다. `archived`, completed accepted and applied worktree task, non-worktree `completed + accepted`, resolution이 기록된 task는 기본 출력에서 숨기고, `completed + unreviewed/rejected/needs_followup`과 accepted-but-unapplied worktree task는 표시합니다. 전체 조회가 필요하면 `--all`을 사용합니다. `failed` task는 한 줄짜리 `last_error` 요약을 함께 표시합니다.

사람이 읽는 기본 `cbr list` 출력은 header가 있는 compact list입니다. 첫 줄은 `PROJECT`, `ID`, `STATUS`, `ATT`, `DEPS`, `NOTE`를 표시합니다. `PROJECT`는 task metadata fallback 규칙으로 계산한 project id입니다. 표시 task는 project별 `[project-id]` section 아래에 묶습니다. `parent_task_id`, `subtask_for`, `root_task_id`, `blocking_subtask_ids`로 parent/root 관계가 확인되면 child task title row에 `├─`/`└─` tree connector를 붙여 parent 아래에 표시합니다. Color-enabled 출력에서는 child connector와 child title을 dim 처리합니다. 기본 list에서 parent/root task가 표시되면 자체 상태만으로는 숨겨질 completed accepted subtask도 그 parent/root 아래에 함께 표시합니다. 각 task는 최소 두 줄로 표시하며 첫 줄에는 project id, task id, effective status, attempts, 첫 dependency id, 첫 note segment를 표시합니다. 둘째 줄은 왼쪽에서 바로 task metadata fallback 규칙으로 계산한 사람이 읽는 title을 표시하고, tree 관계가 있는 경우에만 connector prefix를 추가합니다. `STATUS`는 task JSON의 raw status를 그대로 복사한 값이 아니라 현재 운영 관점의 표시 상태입니다. Raw status가 `runnable` 또는 `needs_resume`이어도 dependency readiness 정책상 runner가 선택할 수 없는 task는 `blocked_dependency`로 표시합니다. Capacity 때문에 현재 admission되지 않는 runnable task는 raw status를 바꾸지 않고 `NOTE`에 `capacity blocked: ...` reason을 표시합니다. `DEPS`와 `NOTE`는 blocker id를 계속 표시하며, completed-but-unaccepted dependency 또는 accepted-but-unapplied worktree dependency가 막는 경우 title, id, blocker reason으로 구분합니다. 중간 폭 table에서는 `NOTE` 폭을 보존하기 위해 `ID`와 `DEPS`의 긴 task id를 middle ellipsis로 축약할 수 있습니다. `DEPS`는 dependency task title을 펼치지 않고 dependency task id를 표시하며, dependency가 여러 개이면 한 줄에 하나씩 이어지는 continuation row에 세로로 표시합니다. 완료되어 현재 dependency readiness 정책상 만족된 dependency도 기본 출력에서 숨기지 않습니다. 색이 꺼진 출력에서는 만족된 dependency를 `dep-id (done)`처럼 최소 텍스트로 표시하고, 색이 켜진 출력에서는 dim style로 표시합니다. Dependency가 없으면 첫 줄에 `-`를 표시합니다. `NOTE`는 cooldown/resume timing, capacity blocker, dependency blocked 상태, failed error, resolution, review 상태, startup stall evidence, running runtime/progress, completed elapsed/duration, blocking subtask aggregate timing, non-default scheduling metadata를 사람이 읽는 segment로 표시합니다. Segment가 여러 개이면 continuation row의 `NOTE` 열에 이어서 표시하고, 정보가 없으면 `-`를 표시합니다. `needs_resume` task는 cooldown 중이면 `resume in 12m (14:32)`, 바로 실행 가능하면 `resume ready`를 표시합니다. `running` task는 `running for 12m` 또는 `running for 1h 04m`처럼 `started_at` 기준 경과 시간을 표시하고, progress metadata가 있으면 `last event 35s ago` 또는 `no progress 9m` 같은 최근 활동 상태를 함께 표시합니다. 초 단위는 1분 미만 elapsed/age에만 표시합니다. 완료됐지만 review/apply 등 후속 조치가 남아 list에 보이는 task는 `completed 8m ago`와 `duration 21m` 같은 timing segment를 표시할 수 있습니다. Parent/root task에 active blocking subtask가 있으면 parent `NOTE`는 subtask count와 함께 failed/blocked, running, review 대기 중 가장 actionable한 aggregate timing 하나를 표시하고, child row는 자기 own timing을 표시합니다. 자동화나 스크립트는 human list 형식에 의존하지 말고 raw task status를 유지하는 `--json`을 사용해야 합니다.

`cbr list --graph`는 기본 compact list와 분리된 human dependency graph를 출력합니다. Project section 아래에 source task를 `* status title` graph node로 표시하고, dependency가 있으면 source graph rail 옆에 `|       ├─ dependency-state title`, `|       └─ dependency-state title` child line으로 표시합니다. Source task는 compact list와 같은 parent/subtask tree 순서를 사용합니다. Graph mode는 사람이 dependency shape를 직관적으로 보는 view이므로 task id, attempts, note는 출력하지 않습니다. Source task status는 기본 list와 같은 effective status와 color policy를 사용하고, dependency state label은 기본 `DEPS` 열과 같은 dependency readiness policy와 color policy를 사용합니다. Color-enabled 출력에서는 graph branch marker/rail을 source task별 stable color로 표시하고, dependency tree connector와 title은 항상 dim 처리해 source task와 시각적 위계를 둡니다. 좁은 terminal에서는 graph/tree prefix와 status/state label 영역을 유지하고 title만 continuation line으로 wrap합니다. `--graph`는 human renderer만 바꾸고, `--json`과 함께 사용하면 graph-specific JSON schema를 만들지 않고 기존 raw task JSON 배열을 그대로 출력합니다. `--watch --graph`는 같은 graph renderer를 반복 갱신합니다.

`cbr list --demo`는 실제 queue나 runner state를 읽거나 쓰지 않고 in-memory synthetic task set을 기존 list renderer에 통과시킵니다. 기본 compact list, `--graph`, `--verbose`, `--color`, narrow layout, `--json` renderer를 작업이 없는 환경에서도 확인하기 위한 sample surface입니다. Demo JSON task에는 `"demo": true`가 포함됩니다.

Human title은 execution profile marker를 항상 포함합니다. Small/light profile은 `[S]`, deep/high-cost profile은 `[D]`, default/unspecified/normal profile은 `[N]`을 title 앞에 붙입니다. Color-enabled 출력에서는 marker 자체에도 profile 색을 적용합니다. 이 marker는 ASCII만 사용합니다.

Review와 resolution metadata도 effective `STATUS`에 반영합니다. `completed + unreviewed`는 `awaiting_review`, `completed + rejected`는 `review_failed`, `completed + needs_followup`은 `needs_followup`, accepted-but-unapplied worktree task는 `accepted_unapplied`, resolution이 기록된 task는 `resolved`로 표시합니다. Startup stall evidence는 현재 재시도 대상이면 retry evidence로, 이미 완료되었거나 해결된 task이면 history로 `NOTE`에 표시해 과거 이력이 현재 장애처럼 보이지 않게 합니다.

`cbr list` human 출력은 optional color를 지원합니다. `--color=auto|always|never` 중 하나를 사용할 수 있으며 기본값은 `auto`입니다. `auto`는 stdout이 TTY이고 `NO_COLOR`가 없을 때만 색을 켭니다. `always`는 색을 강제로 켜고 `never`는 항상 끕니다. 같은 task id는 stable color를 받으며 아직 만족되지 않은 `DEPS`에 같은 id가 나타날 때도 같은 색을 사용합니다. 만족된 dependency는 color-enabled 출력에서 dim style로 표시해 inactive dependency임을 구분합니다. `PROJECT`는 title보다 옅은 색 계열로 표시하고, prefix 없는 title row는 기본 읽기 색을 유지합니다. `STATUS`는 color-enabled human output에서 foreground 색만이 아니라 background가 있는 label 형태로 표시합니다. 상태 label 색은 문제 또는 후속 조치가 필요한 `failed`, `blocked_user`, `blocked_dependency`, `review_failed`, `needs_followup`은 red, 검토 대기/진행은 yellow, 실행 중은 cyan, 실행 가능/재개 대기는 blue, cooldown/usage exhausted 계열은 dim, completed/accepted는 전체 이력 조회에서만 green 계열로 표시합니다. 색은 보조 시각 정보이며 색이 꺼져도 같은 정보를 텍스트로 읽을 수 있어야 합니다. `--json` 출력에는 ANSI code를 포함하지 않습니다.

`cbr list --verbose`는 사람용 table에 `RAW_STATUS`, `LAST_RESULT`, `LAST_RUN`, `LAST_ERROR` 열을 추가합니다. `RAW_STATUS`는 task JSON에 저장된 원래 status를 표시해 effective `STATUS`와 구분합니다. `LAST_RESULT`는 `last_result.status`, `last_result.summary`, optional `commits`/`push_status`, task `git_status`의 한 줄 요약을, `LAST_RUN`은 `last_run.command_kind`, `returncode`, `duration_seconds`를, `LAST_ERROR`는 `last_error`의 한 줄 요약을 표시합니다. 누락된 값은 `-`로 표시하고 transcript 또는 raw JSONL 내용은 출력하지 않습니다. `--json`을 함께 사용하면 verbose 열을 만들지 않고 JSON 배열을 출력합니다.

`cbr list --unreviewed`는 `completed + unreviewed` task만 표시함. `cbr list --needs-review`는 `completed + unreviewed/rejected/needs_followup` task를 표시함.

`cbr archive TASK_ID`는 task 파일을 삭제하지 않고 `status=archived`, `previous_status`, `archived_at`을 기록함.

Successful queue mutations run the optional `post_mutation_trigger_command` after durable writes. This includes `enqueue`, `accept`, `reject`, `resolve`, `archive`, `cooldown clear`, `pause clear`, and successful `apply-plan --apply` mutations. After `run-next` processes one task and releases the runner lock, it may run the same wake-up hook when eligible follow-up work remains and neither global cooldown nor runner pause is active. For auto-review acceptance or bounded auto-fix enqueue, eligible follow-up work includes newly runnable implementation work, the newly created fix task, and another immediately actionable auto-review candidate. Read-only commands, `apply-plan` dry-runs, `cooldown show`, `cooldown set`, `pause show`, `pause set`, empty/cooldown/paused `run-next` exits, and mutation-free auto-review outcomes do not run the trigger.

`cbr summary TASK_ID`는 task metadata, dependency blocked 상태, dependency blocker reason, `last_result.summary`, optional commits/push_status, changed files, verification, task `git_status`, last_error, next_prompt, log path를 transcript보다 짧은 Markdown 형식으로 표시합니다.

`cbr review-bundle TASK_ID`는 현재 대화 context 없이 task 결과를 재검토하기 위한 read-only bundle을 stdout에 생성합니다. 기본 출력은 Markdown-like human report이고, `--json`은 같은 정보를 structured JSON으로 출력합니다. 포함 정보는 task metadata, sanitized prompt excerpt, status/review/resolution, dependencies와 blockers, `last_result`, `last_run`, worktree/follow-up linkage, changed files, verification, `last_error`, relevant log paths, completion-time `task_git_status_snapshot`, review-time task execution repository state, review-time main repository state, inferable commit information, safely scoped commit 또는 working tree diff/stat, public/private safety policy입니다. JSON compatibility를 위해 legacy `git_status`와 `git_repository` aliases도 유지합니다. `current_git_repository`는 gate가 검사하는 task execution repository를 나타내며, worktree-backed task에서는 `current_main_repository`와 `current_task_worktree_repository`도 별도로 표시합니다. commit hash를 명확히 하나로 추론할 수 있으면 해당 commit의 subject/stat/diff를 포함하고, 추론이 여러 개이거나 모호하면 diff를 생략하고 ambiguity를 보고합니다. commit을 추론할 수 없고 task execution repository의 working tree가 dirty이면 working tree diff/stat만 포함합니다. repository가 아니거나 git metadata를 읽을 수 없으면 fallback warning을 보고합니다. 원본 JSONL transcript 내용은 기본적으로 포함하지 않고, 명령은 Codex 호출, enqueue, accept/reject, task state 변경을 수행하지 않습니다.

`cbr review-next`와 `cbr review-next --dry-run`은 `status=completed`이고 `review_status`가 `unreviewed`, `rejected`, `needs_followup`인 task 중 가장 오래된 항목 하나를 선택해 concise review report를 출력합니다. 선택 기준 timestamp는 `completed_at`, fallback으로 `updated_at`, `created_at`, `id`를 사용합니다. `--project`, `--project-root`, `--category`, `--label`은 `list`와 같은 metadata fallback 규칙으로 후보를 좁힙니다. `--json`은 human report와 같은 정보를 structured JSON으로 출력합니다.

`review-next` report는 selected 여부, candidate count, task id, review status, dependency summary, review bundle 핵심 요약, mechanical gates를 포함합니다. Gate는 task status completed, final result status completed, `last_error` 없음, verification list 존재, changed_files list 존재, dependency ready, current git working tree clean, current unpushed commit 없음, task metadata/review bundle에서 감지 가능한 공개/비공개 안전 위반 없음 여부를 확인합니다. `no_unpushed_commits` detail은 current state와 task snapshot을 구분해 예를 들어 `current_has_unpushed=False; snapshot_has_unpushed=True`처럼 표시합니다. Current repository inspection에서 unpushed 상태를 확인할 수 있으면 task `git_status` snapshot의 old ahead/push 정보는 authoritative gate result로 사용하지 않습니다. Dependency summary는 config의 `dependency_requires_accepted_review` 적용 여부와 blocker reason(`not_completed`, `not_accepted`)을 포함합니다. 저장된 reviewer result가 `needs_fix`이면 report-only auto-fix planner도 함께 출력하며, 자동 fix task를 만들 수 있는지와 skip reason 또는 sanitized fix task draft를 보고합니다. Dry-run 명령은 read-only이며 task JSON, review_status, event log, post-mutation trigger를 변경하지 않고, follow-up task를 enqueue하지 않으며, Codex 또는 reviewer Codex를 호출하지 않습니다.

`review-next --apply`는 같은 report/gate 계산을 runner queue lock 아래에서 수행합니다. `--mechanical-auto-accept`, `--reviewer-codex`, config `auto_review_mechanical_accept=true`, config `auto_review_codex_enabled=true` 중 어떤 명시 opt-in도 없으면 task를 변경하지 않고 structured output의 `auto_review.decision=needs_human`으로 보고합니다. 모든 gate가 통과하면 적용 직전에 task `updated_at`, `last_result`, repository head/dirty/ahead 상태, inferred commit 정보가 gate 계산 시점과 같은지 다시 확인합니다. Stale state이면 accept/reject를 적용하지 않습니다. Completion-time task `git_status` snapshot의 old push/ahead 정보만으로 stale state가 되지는 않습니다. Gate 실패, stale state, lock busy 상태는 reviewer Codex 호출 없이 보고됩니다. Reviewer Codex는 `auto_review_codex_enabled=false`와 `auto_review_codex_max_calls_per_run=0`이 기본값인 별도 선택 경로입니다.

`review-next --apply --reviewer-codex`는 config의 reviewer call limit, global/reviewer cooldown, bundle/diff size limit을 통과한 경우에만 reviewer Codex를 한 번 호출합니다. Reviewer 입력은 sanitized review bundle로 제한하고 task 실행 raw log, session id, thread id, private queue contents를 전달하지 않습니다. Reviewer 응답은 decision schema를 엄격하게 검증합니다. `pass` + `confidence=high` + error finding 없음 + required human check 없음 + mechanical/stale-state 재확인 통과인 경우에만 `review_status=accepted`로 바꿉니다. `needs_fix`는 accept하지 않습니다. `auto_review_codex_max_fix_loops_per_task >= 1`, reviewer `auto_fix_allowed=true`, `confidence=high`, `auto_fix_risk=low`, non-empty `suggested_fix_prompt`, repeated finding 없음, fresh state 재확인이 모두 통과하면 별도 fix subtask를 enqueue합니다. 그 외 `needs_fix`, `needs_human`, `failed_review`, invalid schema, rate-limit은 accept하지 않고 sanitized reviewer summary/evidence와 skip reason을 task metadata와 event log에 기록합니다. Rate-limit은 reviewer 전용 cooldown을 설정하며 같은 invocation에서 retry하지 않습니다.

`run-next`의 sequential auto-review phase는 config `auto_review_mechanical_accept=true` 또는 `auto_review_codex_enabled=true`일 때만 켜집니다. Runner는 같은 queue lock 보유 상태에서 completed review candidate 한 건에 대한 apply logic을 먼저 호출합니다. 한 번의 `run-next` invocation은 auto-review accept, reviewer Codex 검토 호출, 또는 구현 task 실행 중 하나만 처리합니다. Gate 실패나 human review가 필요한 상태처럼 task를 변경하지 않고 reviewer Codex도 호출하지 않는 후보는 starvation guard에 따라 해당 invocation에서 runnable/needs_resume 구현 task 선택으로 넘어갈 수 있습니다. 이 규칙은 오래된 비실행 가능 review candidate 하나가 runnable 구현 작업을 계속 막지 않도록 하기 위한 transient skip이며, manual `review-next` 선택 순서나 task JSON의 review 상태를 변경하지 않습니다. Auto-review accept가 dependency policy상 blocked된 child task를 runnable하게 만들거나 다음 자동 검토 후보가 gate, cooldown, reviewer backoff, size limit 조건상 즉시 처리 가능한 상태로 남아 있으면 lock 해제 뒤 기존 post-run trigger 조건으로 scheduler wake-up hook을 실행할 수 있습니다. 같은 기준으로 구현 task를 완료한 직후에도 runnable follow-up 구현 작업이 없더라도 즉시 actionable auto-review 후보가 남아 있으면 wake hook을 실행할 수 있습니다. Mutation 없는 auto-review skip은 trigger를 실행하지 않습니다. 별도로 global runner pause가 active이면 `run-next`는 queue lock 아래에서 stale `running` recovery만 수행하고 `paused` outcome으로 종료합니다. 이때 새 implementation task, reviewer Codex call, bounded auto-fix enqueue를 시작하지 않으며, 기존 live Codex child를 강제로 종료하지도 않습니다.

runner는 각 Codex 호출 후 task에 `last_run` metadata를 저장합니다. 필드는 `command_kind`, `returncode`, `started_at`, `finished_at`, `duration_seconds`, `resume_id_used`, `log_path`입니다. Watchdog이 Codex child를 종료한 경우 `watchdog_reason`도 포함합니다. task-level counters로 `run_count`, `resume_count`, `rate_limit_count`, `failure_count`도 유지합니다.

정상 final JSON 응답을 받은 뒤 runner는 실제 실행 cwd에서 네트워크를 사용하지 않는 local Git inspection을 시도할 수 있습니다. `worktree_mode=disabled`에서는 task `cwd`, `worktree_mode=task`에서는 task worktree가 inspection 대상입니다. repository이면 `git_status`에 `branch`, `upstream`, `comparison_ref`, `ahead`, `behind`, `has_unpushed`, `dirty`, `unpushed_commits`, `warnings`, `inspected_at`을 저장합니다. 비교 기준은 configured upstream을 우선하고, 없으면 local `origin/<branch>` 또는 `origin/main` ref를 사용합니다. runner는 push를 수행하지 않으며, 이 metadata는 운영자가 남은 push 작업을 판단하기 위한 보고용입니다.

`cbr follow TASK_ID`는 저장 중인 attempt JSONL을 read-only polling으로 관찰하는 operator view입니다. `--lines N`은 처음 표시할 기존 JSONL line 수를 제한하고, `--poll-interval SECONDS`는 새 log path와 append된 event 확인 주기를 정합니다. 출력은 compact human stream이며 assistant message, command start/finish, command exit code, final JSON, `turn.failed`/`error`/rate-limit marker 요약을 포함합니다. 사용자 prompt, session/thread id, obvious secret, credential, token, personal user path는 transcript/review sanitization pattern으로 redacted됩니다. task가 `running`이 아니고 더 읽을 새 이벤트가 없으면 종료합니다. 이 명령은 task JSON, runner state, event log, post-mutation trigger를 변경하지 않고 Codex를 호출하지 않습니다.

`cbr transcript TASK_ID`는 저장된 cbr JSONL 로그와, `session_id` 또는 `thread_id`로 찾을 수 있는 Codex 원본 세션 로그에서 주요 대화, tool 호출, patch, final/error event를 사람이 읽기 좋은 형태로 재구성함. `--raw`는 원본 JSONL을 출력함.

`cbr accept TASK_ID`는 `completed` task에만 `review_status=accepted`를 기록함. Worktree-backed task에서는 branch/worktree linkage를 human output에 표시하고 같은 queue lock 안에서 post-accept apply path를 시도함. Fast-forward apply가 가능하면 main에 반영하고 apply metadata를 기록함. Clean stale-base rebase는 re-review로 되돌리고, stale-base conflict는 bounded `worktree_conflict_fix` subtask를 enqueue함. `cbr reject TASK_ID`는 `review_status=rejected`를 기록하고, `--follow-up`을 붙이면 `review_status=needs_followup`을 기록함. Reject는 운영자가 비정상 실행 결과나 후속 처리 필요성을 표시할 수 있도록 non-completed task에서도 허용함. `reject --follow-up`은 새 task를 생성하지 않고 원 task branch/worktree를 가리키는 `review_follow_up` metadata를 기록함.

`cbr resolve TASK_ID --resolution VALUE`는 `failed`, `blocked_user`, 또는 `completed + rejected/needs_followup` task에 운영상 처리 결정을 기록합니다. 허용값은 `wont_fix`, `superseded`, `manual`, `smoke`, `duplicate`입니다. resolution이 있는 task는 기본 `cbr list`와 `review-next` 후보에서 제외되고, `cbr list --all` 또는 `cbr summary TASK_ID`에서 확인합니다.

`cbr rate-limits`는 저장된 sanitized rate-limit evidence event를 조회함. `--json`을 붙이면 evidence JSON 배열을 출력함.

`cbr cooldown show`는 runner state의 `global_cooldown_until`, 활성 여부, approximate remaining duration을 표시합니다. `cbr cooldown clear`는 `global_cooldown_until`을 `null`로 지우고, 즉시 실행 가능한 작업이 있을 수 있으므로 post-mutation trigger를 실행합니다. Set/clear는 작은 sanitized `cooldown_updated` audit event를 기록합니다.

`cbr cooldown set VALUE`는 운영자가 알고 있는 다음 usage/rate-limit reset 시각을 기존 state mechanism의 `global_cooldown_until`에 기록합니다. 입력은 local timezone 기준으로 해석하며, 저장값은 `interpreted_reset_at + 60 seconds`입니다. 이 safety offset은 reset 직전 재시도를 피하기 위한 고정 기본값입니다. 출력은 원본 입력, zero-padded local `interpreted_reset_at`, 실제 저장되는 `effective_cooldown_until`, 그리고 현재 시각 기준 duration을 표시해 잘못 입력한 시간을 운영자가 바로 확인할 수 있게 합니다.

`cbr pause show`는 runner state의 `runner_pause.active`, `reason`, `paused_at`, `paused_by`를 표시합니다. `cbr pause set --reason TEXT`는 expiry 없는 global runner admission pause를 설정합니다. 이 state는 `global_cooldown_until`과 별개이며 rate-limit cooldown semantics를 재사용하지 않습니다. 또한 apply-plan이 task에 기록할 수 있는 task-level `status=paused`와도 다른 control-plane 상태입니다. Pause set은 runner를 깨우지 않습니다. `cbr pause clear`는 pause state를 기본값으로 되돌리고, runnable task 또는 review candidate가 다시 처리 가능해질 수 있으므로 post-mutation trigger를 실행합니다. Set/clear는 작은 sanitized `runner_pause_updated` audit event를 기록하며, state와 event에는 공개 가능한 짧은 reason과 operator id만 저장합니다.

지원 형식은 자연어 parser 없이 제한된 형식만 허용합니다. Time-only 형식은 `H:M`, `HH:M`, `H:MM`, `HH:MM`이며 오늘 해당 local clock time이 미래이면 오늘, 이미 지났으면 내일로 해석합니다. Date-time 형식은 slash `M/D H:M`, `MM/DD HH:MM`, dash `M-D H:M`, `MM-DD HH:MM`, year date `YYYY-MM-DD H:M` 또는 `YYYY-MM-DD HH:MM`을 지원합니다. Slash date는 항상 month/day이며 day/month로 해석하지 않습니다. Timezone이 포함된 ISO datetime은 정확한 advanced input으로 허용합니다. Relative duration은 `+90m`, `+2h30m`, `+1d3h`처럼 day/hour/minute 조합을 지원합니다. Hour는 `0..23`, minute은 `0..59`만 허용합니다. 명시적 date-time이 과거이면 다음 해로 roll forward하지 않고 오류로 종료하며, 해석된 reset 시각이 현재보다 7일을 초과해 먼 경우에도 오류로 종료합니다.

`cbr events`는 append-only event log에서 최근 event를 조회함. 기본 출력은 human-readable table이고, `--json`은 event object 배열을 출력함. `--task-id`로 특정 task event만 필터링할 수 있고 `--limit`으로 최대 출력 개수를 제한함.

`cbr doctor`는 저비용 health check임. resolved `queue_dir`, `log_dir`, `event_dir`, `lock_file`, `state_file` 경로, runtime directory 접근 가능 여부, configured Codex executable path, resolved Codex executable path, executable availability, bounded `codex --version` output, execution profile 이름과 allowlisted override key, global cooldown, runner pause, active lock age/pid/liveness, status별 task 수, needs-review completed task 수, resolved failed/blocked task 수, resolved completed-review task 수, runnable task 수, cooldown task 수, mechanical auto-review enable 상태, reviewable completed task 수, startup/no-progress stall evidence를 표시함. Version 확인은 configured executable에 `--version`만 붙여 짧은 timeout으로 실행하며, `codex exec`를 호출하지 않음. Version command 실패, 빈 output, timeout은 warning으로 보고하고 doctor 실패로 취급하지 않음. configured/current project root가 git repository 안에 있으면 branch, dirty status, upstream 또는 local `origin/main` 대비 ahead/behind count도 표시함. git check는 local repository metadata만 읽고 fetch/pull 같은 network operation을 실행하지 않음. git executable 없음, git repository 아님, upstream 없음, remote ref 조회 불가 같은 상태는 warning으로 보고하고 doctor 실패로 취급하지 않음. `--json`을 붙이면 같은 정보를 JSON으로 출력함. error check가 있으면 non-zero로 종료하고 warning은 종료 코드에 영향을 주지 않음.

Runner lock recovery treats a lock as recoverable immediately when metadata contains a pid for the same hostname and that pid is no longer alive. Unknown host, missing/invalid pid, invalid metadata, and cross-host locks fall back to the age-based stale threshold.

Doctor는 기본적으로 Codex application bundle 안의 별도 executable과 configured CLI를 비교하지 않음. macOS나 특정 app install layout을 가정하지 않기 위함임. 운영자가 app-bundled CLI와 standalone CLI 차이를 확인해야 하는 환경에서는 별도 수동 조사로 처리함. Routine doctor는 대형 binary hash를 계산하지 않음. Hash는 향후 `--verbose` 또는 deep diagnostic check에서 명시적으로 요청할 때만 고려함.
