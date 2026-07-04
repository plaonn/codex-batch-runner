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
cbr enqueue --cwd /repo --reasoning-depth low --cost-sensitivity high --prompt-file prompt.md
cbr enqueue --cwd /repo --routing-size small --routing-risk low --verification-scope unit --verification-scope docs --prompt-file prompt.md
cbr enqueue --cwd /repo --backend shell --command-json '["python3", "-m", "pytest", "tests/test_smoke.py"]'
cbr enqueue --cwd /repo --backend external-json-command --prompt-file task.md --command-json '["./tools/cbr-json-wrapper"]'
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
cbr run-loop --json
cbr show TASK_ID
cbr summary TASK_ID
cbr routing-report --project project-id --json
cbr routing-policy-candidates --project project-id --json
cbr decision-cards --project project-id --json
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
cbr dashboard
cbr dashboard --host 127.0.0.1 --port 8765
cbr doctor
cbr doctor --json
cbr maintenance direct-worktrees --dry-run
cbr maintenance direct-worktrees --repo-root path/to/repo --dry-run --json
cbr maintenance direct-worktrees --apply
cbr maintenance direct-worktrees --json
cbr prune
cbr prune --older-than-days 60 --json
cbr prune --apply
cbr prune --notifier-cursor-state path/to/notify-state.json
```

공통 option:

```bash
--config path/to/config.json
```

`cbr enqueue --backend external-json-command` requires `--command-json` or final-position `--command`. The command must be an argv list; cbr does not evaluate shell strings. The runner appends the wrapped cbr prompt as the command's final argv argument and expects stdout to contain one final JSON object with `task_id`, `status`, `summary`, `changed_files`, and `verification`.

config 탐색 순서:

1. `--config` 명시값
2. `CBR_CONFIG` 환경 변수

둘 다 없으면 `cbr`는 실패합니다. 자동화와 launchd에서는 명시적 config 또는 절대 경로 기반 호출을 사용하고, 운영자가 직접 viewer/review 용도로 사용할 때는 `CBR_CONFIG`를 설정한 shell에서 `cbr` console script를 사용합니다.

`enqueue --title`은 list scanability를 위한 사람이 붙이는 짧은 제목입니다. 보통 4-8 words 정도의 `action + object + short qualifier` 형태를 쓰되, 글자 수 목표를 맞추려고 늘리지 않습니다. 같은 list에서 충분히 구분될 정도면 되고 전역 고유성은 필요하지 않습니다. Canonical identifier는 task id입니다. Full prompt 첫 문장, 긴 배경 설명, private detail, raw path, session/thread id, runtime/log 내용은 title에 넣지 않습니다. 저장 및 표시 title은 whitespace를 한 칸으로 접고 80자에서 deterministic ellipsis 처리합니다. `--title`이 없으면 prompt의 첫 non-empty line을 같은 규칙으로 fallback 표시하고, 그것도 없으면 task id를 표시합니다.

`cbr list` 기본 출력은 운영자가 신경 써야 할 task 중심으로 유지합니다. `archived`, completed accepted and applied worktree task, non-worktree `completed + accepted`, resolution이 기록된 task, discard cleanup이 완료된 rejected worktree task는 기본 출력에서 숨기고, `completed + unreviewed/rejected/needs_followup`과 accepted-but-unapplied worktree task는 표시합니다. 전체 조회가 필요하면 `--all`을 사용합니다. `failed` task는 한 줄짜리 `last_error` 요약을 함께 표시합니다.

사람이 읽는 기본 `cbr list` 출력은 header가 있는 compact list입니다. Wide table의 첫 줄은 `[M]`, `TITLE`, `STATUS`, `DETAIL`을 표시합니다. `[M]`는 model requirement marker를 표시하고, `TITLE`은 marker를 제외한 task title만 표시합니다. 표시 task는 project별 `[project-id]` section 아래에 묶고, 각 item row에는 project id와 task id를 반복하지 않습니다. `parent_task_id`, `subtask_for`, `root_task_id`, `blocking_subtask_ids`로 parent/root 관계가 확인되면 child task의 `TITLE` cell에 `├─`/`└─` tree connector를 붙여 parent 아래에 표시합니다. Color-enabled 출력에서는 child connector와 child title을 dim 처리합니다. 기본 list에서 parent/root task가 표시되면 자체 상태만으로는 숨겨질 completed accepted subtask도 그 parent/root 아래에 함께 표시합니다. `--needs-review`처럼 source task를 좁히는 human filter를 사용해도 parent/root task가 표시되면 숨겨질 subtask는 같은 parent/root 아래에 다시 포함됩니다. `TITLE`과 `DETAIL`은 terminal width 안에서 wrap 또는 ellipsis 처리되고, 산출된 최소 table width가 terminal width보다 크면 table 대신 project section과 `[M]:`, `TITLE:`, `STATUS:`, `DETAIL:` block layout을 사용합니다. `--json`과 `--verbose`는 이 narrow block layout 영향을 받지 않습니다. `STATUS`는 compact phase+kind 토큰이며, 예시는 `--success`, `##dep`, `??review`, `++followup`, `++apply`, `..new`, `..resume`, `||cooldown`, `||capacity`입니다. `RAW_STATUS`는 task JSON의 raw status로, `blocked_dependency`, `needs_followup`, `awaiting_review`, `review_rejected`, `accepted_unapplied`, `discarded` 같은 legacy/effective/debug 문자열은 JSON/`RAW_STATUS`에서 확인할 수 있는 내부 참조값입니다. Capacity 때문에 admission되지 않는 runnable task는 raw status를 바꾸지 않고 `DETAIL`에 `capacity blocked: ...` reason을 표시합니다. `DETAIL`은 dependency readiness, cooldown/resume timing, capacity blocker, failed error, resolution, review state, startup stall evidence, running runtime/progress, completed elapsed/duration, blocking subtask aggregate timing, non-default scheduling metadata를 사람이 읽는 segment로 표시합니다. 완료되어 현재 dependency readiness 정책상 만족된 dependency도 기본 출력에서 숨기지 않습니다. 색이 꺼진 출력에서는 만족된 dependency를 `[N] dependency title (done)`처럼 최소 텍스트로 표시하고, 색이 켜진 출력에서는 dim style로 표시합니다. 아직 만족되지 않은 dependency는 `[N] dependency title (blocked)`, `[N] dependency title (not_accepted)`, `[N] dependency title (not_applied)`, `missing dependency: dep-id (missing)`처럼 `DETAIL`에서 직접 구분합니다. `needs_resume` task는 cooldown 중이면 `resume in 12m (14:32)`, 바로 실행 가능하면 `resume ready`를 표시합니다. `running` task는 `running for 12m` 또는 `running for 1h 04m`처럼 `started_at` 기준 경과 시간을 표시하고, progress metadata가 있으면 `last event 35s ago` 또는 `no progress 9m` 같은 최근 활동 상태를 함께 표시합니다. 초 단위는 1분 미만 elapsed/age에만 표시합니다. 완료됐지만 review/apply 등 후속 조치가 남아 list에 보이는 task는 `completed 8m ago`와 `duration 21m` 같은 timing segment를 표시할 수 있습니다. Parent/root task에 active blocking subtask가 있으면 parent `DETAIL`은 subtask count와 함께 failed/blocked, running, review 대기 중 가장 actionable한 aggregate timing 하나를 표시하고, child row는 자기 own timing을 표시합니다. 자동화나 스크립트는 human list 형식에 의존하지 말고 raw task status를 유지하는 `--json`을 사용해야 합니다.

`cbr list --graph`는 기본 compact list와 분리된 human dependency graph를 출력합니다. `--graph`는 현재 목록 대상의 **non-interactive 스냅샷 렌더러**로, 노드 선택/펼치기/검색/필터 네비게이션 같은 대화형 상호작용은 제공하지 않습니다. Project section 아래에서 source task는 한 번씩 `* status title` graph node로 표시합니다. `depends_on` 관계는 dependency task를 dependent task 아래의 child/detail row로 복제하지 않고, source node 사이의 git-style diagonal/vertical rail로 연결합니다. 선형 chain은 `* ... A`, `|`, `* ... B`, `|`, `* ... C`처럼 한 레인으로 렌더링되어 fan-in을 표시하지 않습니다. 다수의 dependency가 하나의 task로 모이면 `* ... dependency A`, `| * ... dependency B`, ` \\|`, `  * ... dependent`처럼 join 형태를 유지합니다. `├─`/`└─` tree connector는 parent/subtask 관계에만 사용하며, subtask row는 source `*` 없이 parent 아래에 `├─ status title` 또는 `└─ status title`로 표시합니다. Graph mode는 사람이 dependency shape를 직관적으로 보는 view이므로 task id, attempts, note는 출력하지 않습니다. Source task status는 기본 list와 같은 effective status와 color policy를 사용합니다. Color-enabled graph output은 glyph grid에 붙은 metadata로 색을 정합니다. Task node glyph `*`는 node identity metadata를 갖고 해당 task의 node color를 사용합니다. Line glyph `|`, `\\`, `/`는 lane/stroke identity metadata를 갖고, 이어지는 lane은 보통 그 lane을 시작한 source task의 node color를 유지합니다. Layout은 서로 독립인 lane 두 개가 같은 cell을 차지하는 true crossing을 피해야 하며, future layout에서 crossing이 필요해지면 한 glyph에 두 lane color를 인코딩하지 말고 row/column을 추가해 피합니다. Color는 보조 시각 정보이므로 `--color=never` ASCII 출력은 같은 graph/tree 의미를 유지합니다. Dependency task title은 dependency라는 이유만으로 dim 처리하지 않습니다. 반면 subtask title과 subtask tree glyph는 dim/tree style을 사용하며 dependency lane color와 분리합니다. 좁은 terminal에서는 graph/tree prefix와 status label 영역을 유지하고 title만 continuation line으로 wrap합니다. Parent title wrap은 pending subtask tree rail을 label gap에 유지하고, last-child subtask title wrap은 vertical tree rail을 유지하지 않으며, non-last child wrap은 tree rail을 유지합니다. Dependency source continuation은 `| |` 같은 visible rail을 보존합니다. `--graph`는 human renderer만 바꾸고, `--json`과 함께 사용하면 graph-specific JSON schema를 만들지 않고 기존 raw task JSON 배열을 그대로 출력합니다. `--watch --graph`는 같은 graph renderer를 반복 갱신합니다.

`cbr list --demo`는 실제 queue나 runner state를 읽거나 쓰지 않고 in-memory synthetic task set을 기존 list renderer에 통과시킵니다. 기본 compact list, `--graph`, `--verbose`, `--color`, narrow layout, `--json` renderer를 작업이 없는 환경에서도 확인하기 위한 sample surface입니다. Demo JSON task에는 `"demo": true`가 포함됩니다.

Human compact table은 model requirement marker를 `[M]` column에 표시합니다. `--graph`와 `DETAIL` dependency segment처럼 별도 marker column이 없는 human renderer는 title 앞에 marker를 붙여 `[N] dependency title (done)` 또는 `* ..new/..resume [D] title` 형태로 표시합니다. 낮은 reasoning depth와 높은 cost sensitivity 조합은 `[S]`, 높은 reasoning 또는 tool reliability 요구는 `[D]`, 그 외는 `[N]`을 사용합니다. Color-enabled 출력에서는 marker 자체에도 marker 색을 적용합니다. 이 marker는 ASCII만 사용합니다.

Review와 resolution metadata도 effective `STATUS` 토큰에 반영합니다. `completed + unreviewed`는 `??review`로 표시합니다. `completed + needs_followup`은 `++followup`으로, accepted-but-unapplied worktree task는 `++apply`로 표시합니다. 완료되었으나 폐기/거절/해결된 결과는 `--resolved`, `--success` 계열로 정리되며, 내부적으로는 `resolved`, `discarded`, `review_rejected`, `review_failed`, `needs_followup`, `accepted_unapplied` 같은 legacy/raw 이름이 사용될 수 있습니다. `discarded` row는 `--all`에서 `DETAIL`에 `rejected; discarded; not applied`를 표시하며, `RAW_STATUS`에서 `review_status`, `raw status`, `legacy status`를 함께 확인할 수 있습니다. Startup stall evidence는 현재 재시도 대상이면 retry evidence로, 이미 완료되었거나 해결된 task이면 history로 `DETAIL`에 표시해 과거 이력이 현재 장애처럼 보이지 않게 합니다.

`cbr list` human 출력은 optional color를 지원합니다. `--color=auto|always|never` 중 하나를 사용할 수 있으며 기본값은 `auto`입니다. `auto`는 stdout이 TTY이고 `NO_COLOR`가 없을 때만 색을 켭니다. `always`는 색을 강제로 켜고 `never`는 항상 끕니다. Color-enabled 출력에서는 project section header를 muted background stripe로 표시하고 terminal width를 알 수 있으면 줄 끝까지 채웁니다. `DETAIL`의 dependency blocker label은 dependency task의 effective status style을 사용합니다. 만족된 dependency는 color-enabled 출력에서 dim style로 표시해 inactive dependency임을 구분합니다. Missing dependency는 special danger style을 사용합니다. Completed-but-unaccepted dependency는 color-enabled 출력과 color-off 출력 모두 dependency title에 `(not_accepted)` suffix를 유지합니다. `STATUS`는 color-enabled human output에서 foreground 색만이 아니라 background가 있는 label 형태로 표시합니다. 색상 그룹은 compact 토큰의 phase/kind 조합을 기준으로 적용되며, 상태 라벨의 내부명은 `RAW_STATUS`/JSON에서 확인할 수 있습니다. 알려지지 않은 future/external status도 plain text로 떨어지지 않고 neutral background label로 표시합니다. 색은 보조 시각 정보이며 색이 꺼져도 같은 정보를 텍스트로 읽을 수 있어야 합니다. `--json` 출력에는 ANSI code를 포함하지 않습니다.

`cbr list --verbose`는 사람용 table에 `RAW_STATUS`, `LAST_RESULT`, `LAST_RUN`, `LAST_ERROR` 열을 추가합니다. `RAW_STATUS`는 task JSON에 저장된 원래 status를 표시해 effective `STATUS`와 구분합니다. `LAST_RESULT`는 `last_result.status`, `last_result.summary`, optional `commits`/`push_status`, task `git_status`의 한 줄 요약을, `LAST_RUN`은 `last_run.command_kind`, `returncode`, `duration_seconds`를, `LAST_ERROR`는 `last_error`의 한 줄 요약을 표시합니다. 누락된 값은 `-`로 표시하고 transcript 또는 raw JSONL 내용은 출력하지 않습니다. `--json`을 함께 사용하면 verbose 열을 만들지 않고 JSON 배열을 출력합니다.

`cbr list --unreviewed`는 `completed + unreviewed` task만 표시함. `cbr list --needs-review`는 `completed + unreviewed/rejected/needs_followup` task를 표시하되, discard cleanup이 완료된 rejected worktree task는 이미 닫힌 결과로 보고 제외함.

`cbr archive TASK_ID`는 task 파일을 삭제하지 않고 `status=archived`, `previous_status`, `archived_at`을 기록함.

Successful queue mutations run the optional `post_mutation_trigger_command` after durable writes. This includes `enqueue`, `accept`, `reject`, `resolve`, `archive`, `cooldown clear`, `pause clear`, and successful `apply-plan --apply` mutations. `enqueue` is refused while runner pause is active, so it does not write a task or run the trigger during a maintenance pause. After `run-next` processes one task and releases the runner lock, it may run the same wake-up hook when eligible follow-up work remains and neither global cooldown nor runner pause is active. For auto-review acceptance or bounded auto-fix enqueue, eligible follow-up work includes newly runnable implementation work, the newly created fix task, and another immediately actionable auto-review candidate. Read-only commands, `apply-plan` dry-runs, `cooldown show`, `cooldown set`, `pause show`, `pause set`, refused `enqueue`, empty/cooldown/paused `run-next` exits, and mutation-free auto-review outcomes do not run the trigger. `run-loop` suppresses these per-iteration post-run wake hooks because the same process claims eligible follow-up work on the next loop iteration.

`cbr summary TASK_ID`는 task metadata, dependency blocked 상태, dependency blocker reason, `last_result.summary`, optional commits/push_status, changed files, verification, task `git_status`, last_error, next_prompt, log path를 transcript보다 짧은 Markdown 형식으로 표시합니다.

`cbr routing-report [--project PROJECT_ID] [--category CATEGORY] [--label LABEL] [--execution-evidence-json PATH] [--json]`는 queue의 routing outcome/cost 신호를 read-only diagnostics로 출력합니다. 집계 그룹에는 model requirement, model selection rule, category/label, experiment, experiment lane family, routing_size/risk, verification scope, routing decision tuple, low-cost candidate 신호, provider resource evidence가 포함되며 accepted/needs-fix/rejected rate, reviewer decision, auto-fix frequency, attempts/run, duration 기반 cost proxy가 함께 표시됩니다. `evaluation_diagnostics.task_buckets`에는 static threshold-only advisory도 포함됩니다. 기준은 `min_accepted_count=5`, `min_first_pass_accept_rate=0.90`, `max_needs_fix_or_rejected_rate=0.05`이고 status는 `insufficient_sample`, `below_threshold`, `reviewable` 중 하나입니다. `evaluation_diagnostics.probe_lanes`는 existing `routing_experiment` 값을 `baseline`, `probe`, `guard`, `manual`, `unspecified`, `other` lane family로 분류해 task bucket과 requirement별 lane outcome을 보여주는 read-only diagnostic입니다. 이 advisory는 이미 있는 evaluation evidence만 요약하며 routing/model policy mutation 또는 apply 동작을 수행하지 않습니다. provider resource evidence는 현재 Codex provider 불확실성을 `provider_id=codex`, `quota_boundary=unknown`, `sharing_assumption=not_independent`로 드러내는 advisory signal이며, local `capacity_pool`, worker/reviewer role, legacy profile name에서 provider quota bucket을 추론하지 않습니다. 보고 결과는 요구 벡터와 selection rule 후보를 가늠하기 위한 운영 증거일 뿐, 정책을 자동 변경하지 않습니다.

`cbr routing-policy-candidates [--project PROJECT_ID] [--category CATEGORY] [--label LABEL] [--execution-evidence-json PATH] [--include-non-reviewable] [--json]`는 `routing-report`의 existing evaluation diagnostics task bucket advisory를 policy-change candidate surface로 재표시합니다. 기본 출력은 `threshold_advisory_status=reviewable` bucket만 `candidates`로 내보내며 각 entry는 `candidate_id`, `task_bucket_key`, evidence counts/rates, advisory status/reasons, fixed thresholds, `recommended_next_step=operator_review`, `read_only=true`, `mutation_allowed=false`를 포함합니다. `decision_cards`는 실행 후보 보고 상태와 사용자 결정 상태를 별도 축으로 표시하며, reviewable bucket은 `user_decision_status=decision_required`, non-reviewable bucket은 `user_decision_status=not_ready`로 표시합니다. `--include-non-reviewable`은 `insufficient_sample`/`below_threshold` bucket을 `non_reviewable_buckets`와 observation decision card에 함께 표시하고 `blocked_reason`, `rejection_reasons`, next step을 드러냅니다. 이 명령은 candidate를 승인하지 않고, model/routing/provider config를 쓰지 않으며, task JSON/event log/review status를 변경하지 않습니다.

`cbr decision-cards [--project PROJECT_ID] [--category CATEGORY] [--label LABEL] [--execution-evidence-json PATH] [--include-observations] [--decision-axis AXIS] [--user-decision-status STATUS] [--json]`는 현재 config의 `policy-proposals execution-target-freshness` decision cards와 `routing-policy-candidates` decision cards를 하나의 read-only inventory로 모읍니다. 기본 출력은 `decision_required`와 `approval_blocked`처럼 사용자가 볼 필요가 있는 카드만 포함하고, `--include-observations`를 붙이면 routing `not_ready` 관찰 카드도 함께 포함합니다. `--decision-axis`는 `execution_target_freshness` 또는 `routing_policy_change`를 받는 repeatable allowlist filter입니다. `--user-decision-status`는 repeatable allowlist filter이며 `decision_required`, `approval_blocked`, `decision_pending`, `approved`, `not_approved`, `not_ready`, `invalid` 중 하나를 받습니다. JSON에는 inventory `generated_at`, 각 source report의 generated timestamp/read-only metadata, `summary.by_recommendation`, `summary.by_blocked_reason`이 포함되고, human output도 recommendation/blocker summary groups를 표시합니다. 이 명령은 하위 report builder를 읽기 전용으로 호출하며 approval, apply, model/routing/provider config write, task/event/runtime mutation을 수행하지 않습니다.

`--execution-evidence-json PATH`는 repeatable option입니다. JSON object, `{"records": [...]}` object, 또는 object list를 받을 수 있으며 현재 public contract는 `record_kind="codex_subagent_execution"`입니다. 이 record는 queue task로 등록되지 않고 `execution_evidence_rows`에만 나타납니다. `task_count`, `task_rows`, queue task group 집계는 변하지 않습니다. Evidence row는 `execution_surface=codex_subagent`, `subject.queue_task=false`, hashed work id를 포함하고, Codex app actual model identity를 확정할 수 없으면 `model_source=codex_app_default` 또는 `unknown`을 사용합니다. Evaluation diagnostics의 `execution_surfaces` group은 queue task와 supplemental evidence를 surface별로 분리해 보여줍니다. 입력 파일에는 structured metadata와 sanitized `last_run`/`last_result`/reviewer result만 넣고 raw prompt, transcript/log body, session id, thread id, credential, private absolute path를 넣지 않습니다.

`cbr routing-eval-report --json`은 bounded row-level evaluation data와 함께 `evaluation_diagnostics.probe_lanes`와 `execution_evidence_diagnostics.probe_lanes`를 출력합니다. Queue task diagnostics는 `evaluation_rows`에서만 계산하고, supplemental diagnostics는 `execution_evidence_rows`에서만 계산합니다. 두 diagnostics 모두 `read_only=true`, `mutation_allowed=false`이며, baseline/probe/guard/manual/unspecified/other lane family, raw experiment, task bucket lane, model requirement lane별 outcome을 표시합니다.

Producer-facing example은 [subagent-execution-evidence.example.json](../examples/subagent-execution-evidence.example.json)을 참고합니다. 세부 contract는 [execution.md](execution.md#supplemental-codex-subagent-execution-evidence)에 유지합니다.

운영자용 범위/제외:
- 보여줌: model requirement/selection rule별 outcome/cost 집계, category/label-by outcome/cost 집계, low-cost 후보 힌트, reviewer decision와 auto-fix 빈도, stale sample 요약.
- 의도적 제외: raw prompt, full transcript/JSONL, per-task full body output, Codex 재호출/실행 계획/patch 제안 같은 auto action.
- 보장: task JSON/event log/review status 미변경, Codex/reviewer Codex 미호출, 기존 `routing-report` 자체는 advisory read-only.

`cbr review-bundle TASK_ID`는 현재 대화 context 없이 task 결과를 재검토하기 위한 read-only bundle을 stdout에 생성합니다. 기본 출력은 Markdown-like human report이고, `--json`은 같은 정보를 structured JSON으로 출력합니다. 포함 정보는 task metadata, sanitized prompt excerpt, status/review/resolution, dependencies와 blockers, `last_result`, `last_run`, worktree/follow-up linkage, changed files, verification, `last_error`, relevant log paths, completion-time `task_git_status_snapshot`, review-time task execution repository state, review-time main repository state, inferable commit information, safely scoped commit 또는 working tree diff/stat, public/private safety policy입니다. JSON compatibility를 위해 legacy `git_status`와 `git_repository` aliases도 유지합니다. `current_git_repository`는 gate가 검사하는 task execution repository를 나타내며, worktree-backed task에서는 `current_main_repository`와 `current_task_worktree_repository`도 별도로 표시합니다. commit hash를 명확히 하나로 추론할 수 있으면 해당 commit의 subject/stat/diff를 포함하고, 추론이 여러 개이거나 모호하면 diff를 생략하고 ambiguity를 보고합니다. commit을 추론할 수 없고 task execution repository의 working tree가 dirty이면 working tree diff/stat만 포함합니다. repository가 아니거나 git metadata를 읽을 수 없으면 fallback warning을 보고합니다. 원본 JSONL transcript 내용은 기본적으로 포함하지 않고, 명령은 Codex 호출, enqueue, accept/reject, task state 변경을 수행하지 않습니다.

`cbr review-next`와 `cbr review-next --dry-run`은 `status=completed`이고 `review_status`가 `unreviewed`, `rejected`, `needs_followup`인 task 중 가장 오래된 항목 하나를 선택해 concise review report를 출력합니다. 선택 기준 timestamp는 `completed_at`, fallback으로 `updated_at`, `created_at`, `id`를 사용합니다. `--project`, `--project-root`, `--category`, `--label`은 `list`와 같은 metadata fallback 규칙으로 후보를 좁힙니다. `--json`은 human report와 같은 정보를 structured JSON으로 출력합니다.

`review-next` report는 selected 여부, candidate count, task id, review status, dependency summary, review bundle 핵심 요약, mechanical gates를 포함합니다. Gate는 task status completed, final result status completed, `last_error` 없음, verification list 존재, changed_files list 존재, dependency ready, current git working tree clean, current unpushed commit 없음, task metadata/review bundle에서 감지 가능한 공개/비공개 안전 위반 없음 여부를 확인합니다. `no_unpushed_commits` detail은 current state와 task snapshot을 구분해 예를 들어 `current_has_unpushed=False; snapshot_has_unpushed=True`처럼 표시합니다. Current repository inspection에서 unpushed 상태를 확인할 수 있으면 task `git_status` snapshot의 old ahead/push 정보는 authoritative gate result로 사용하지 않습니다. Dependency summary는 config의 `dependency_requires_accepted_review` 적용 여부와 blocker reason(`not_completed`, `not_accepted`)을 포함합니다. 저장된 reviewer result가 `needs_fix`이면 report-only auto-fix planner도 함께 출력하며, 자동 fix task를 만들 수 있는지와 skip reason 또는 sanitized fix task draft를 보고합니다. Dry-run 명령은 read-only이며 task JSON, review_status, event log, post-mutation trigger를 변경하지 않고, follow-up task를 enqueue하지 않으며, Codex 또는 reviewer Codex를 호출하지 않습니다.

`review-next --apply`는 같은 report/gate 계산을 runner queue lock 아래에서 수행합니다. `--mechanical-auto-accept`, `--reviewer-codex`, config `auto_review_mechanical_accept=true`, config `auto_review_codex_enabled=true` 중 어떤 명시 opt-in도 없으면 task를 변경하지 않고 structured output의 `auto_review.decision=needs_human`으로 보고합니다. 모든 gate가 통과하면 적용 직전에 task `updated_at`, `last_result`, repository head/dirty/ahead 상태, inferred commit 정보가 gate 계산 시점과 같은지 다시 확인합니다. Stale state이면 accept/reject를 적용하지 않습니다. Completion-time task `git_status` snapshot의 old push/ahead 정보만으로 stale state가 되지는 않습니다. Gate 실패, stale state, lock busy 상태는 reviewer Codex 호출 없이 보고됩니다. Reviewer Codex는 `auto_review_codex_enabled=false`와 `auto_review_codex_max_calls_per_run=0`이 기본값인 별도 선택 경로입니다.

`review-next --apply --reviewer-codex`는 config의 reviewer call limit, global/reviewer cooldown, bundle/diff size limit을 통과한 경우에만 reviewer Codex를 한 번 호출합니다. Reviewer 입력은 sanitized review bundle로 제한하고 task 실행 raw log, session id, thread id, private queue contents를 전달하지 않습니다. Reviewer 응답은 decision schema를 엄격하게 검증합니다. `pass` + `confidence=high` + error finding 없음 + required human check 없음 + mechanical/stale-state 재확인 통과인 경우에만 `review_status=accepted`로 바꿉니다. `needs_fix`는 accept하지 않습니다. `auto_review_codex_max_fix_loops_per_task >= 1`, reviewer `auto_fix_allowed=true`, `confidence=high`, `auto_fix_risk=low`, non-empty `suggested_fix_prompt`, repeated finding 없음, fresh state 재확인이 모두 통과하면 별도 fix subtask를 enqueue합니다. 그 외 `needs_fix`, `needs_human`, `failed_review`, invalid schema, rate-limit은 accept하지 않고 sanitized reviewer summary/evidence와 skip reason을 task metadata와 event log에 기록합니다. Rate-limit은 reviewer 전용 cooldown을 설정하며 같은 invocation에서 retry하지 않습니다.

`run-next`의 sequential auto-review phase는 config `auto_review_mechanical_accept=true` 또는 `auto_review_codex_enabled=true`일 때만 켜집니다. Runner는 같은 queue lock 보유 상태에서 completed review candidate 한 건에 대한 apply logic을 먼저 호출합니다. 한 번의 `run-next` invocation은 auto-review accept, reviewer Codex 검토 호출, 또는 구현 task 실행 중 하나만 처리합니다. `run-next --json`은 항상 기존 outcome shape의 JSON object 하나만 출력합니다. Gate 실패나 human review가 필요한 상태처럼 task를 변경하지 않고 reviewer Codex도 호출하지 않는 후보는 starvation guard에 따라 해당 invocation에서 runnable/needs_resume 구현 task 선택으로 넘어갈 수 있습니다. 이 규칙은 오래된 비실행 가능 review candidate 하나가 runnable 구현 작업을 계속 막지 않도록 하기 위한 transient skip이며, manual `review-next` 선택 순서나 task JSON의 review 상태를 변경하지 않습니다. Auto-review accept가 dependency policy상 blocked된 child task를 runnable하게 만들거나 다음 자동 검토 후보가 gate, cooldown, reviewer backoff, size limit 조건상 즉시 처리 가능한 상태로 남아 있으면 lock 해제 뒤 기존 post-run trigger 조건으로 scheduler wake-up hook을 실행할 수 있습니다. 같은 기준으로 구현 task를 완료한 직후에도 runnable follow-up 구현 작업이 없더라도 즉시 actionable auto-review 후보가 남아 있으면 wake hook을 실행할 수 있습니다. Mutation 없는 auto-review skip은 trigger를 실행하지 않습니다. 별도로 global runner pause가 active이면 `run-next`는 queue lock 아래에서 stale `running` recovery만 수행하고 `paused` outcome으로 종료합니다. 이때 새 implementation task, reviewer Codex call, bounded auto-fix enqueue를 시작하지 않으며, 기존 live Codex child를 강제로 종료하지도 않습니다.

`cbr run-loop`은 launchd/operator single-worker용 command입니다. 각 iteration은 config를 다시 로드한 뒤 `run-next`와 같은 one-shot path를 호출하므로 pause, cooldown, lock, capacity, dependency, task cooldown, worktree, review gate를 우회하지 않습니다. `run-next` one-shot의 post-run wake hook은 loop iteration 안에서 실행하지 않습니다. `--json` 출력은 iteration마다 기존 `run-next` outcome shape의 compact JSON object 한 줄을 쓰는 JSONL입니다. Loop는 `empty`, `paused`, `cooldown`, `locked`, `review_needed`, `stale_finalization`처럼 다음 work가 actionable하지 않은 outcome에서 멈춥니다. Progress outcome이 계속 나오더라도 runaway를 막기 위해 `--max-iterations` safety fuse가 있으며 기본값은 100입니다. 이 값은 correctness 조건이 아니라 비정상 반복에 대한 운영 guard이므로 큰 queue를 한 scheduler tick에서 더 오래 drain하려면 높일 수 있습니다.

runner는 각 Codex 호출 후 task에 `last_run` metadata를 저장합니다. 필드는 `command_kind`, `returncode`, `started_at`, `finished_at`, `duration_seconds`, `resume_id_used`, `log_path`입니다. Watchdog이 Codex child를 종료한 경우 `watchdog_reason`도 포함합니다. task-level counters로 `run_count`, `resume_count`, `rate_limit_count`, `failure_count`도 유지합니다.

정상 final JSON 응답을 받은 뒤 runner는 실제 실행 cwd에서 네트워크를 사용하지 않는 local Git inspection을 시도할 수 있습니다. `worktree_mode=disabled`에서는 task `cwd`, `worktree_mode=task`에서는 task worktree가 inspection 대상입니다. repository이면 `git_status`에 `branch`, `upstream`, `comparison_ref`, `ahead`, `behind`, `has_unpushed`, `dirty`, `unpushed_commits`, `warnings`, `inspected_at`을 저장합니다. 비교 기준은 configured upstream을 우선하고, 없으면 local `origin/<branch>` 또는 `origin/main` ref를 사용합니다. runner는 push를 수행하지 않으며, 이 metadata는 운영자가 남은 push 작업을 판단하기 위한 보고용입니다.

`cbr follow TASK_ID`는 저장 중인 attempt JSONL을 read-only polling으로 관찰하는 operator view입니다. `--lines N`은 처음 표시할 기존 JSONL line 수를 제한하고, `--poll-interval SECONDS`는 새 log path와 append된 event 확인 주기를 정합니다. 출력은 compact human stream이며 assistant message, command start/finish, command exit code, final JSON, `turn.failed`/`error`/rate-limit marker 요약을 포함합니다. 사용자 prompt, session/thread id, obvious secret, credential, token, personal user path는 transcript/review sanitization pattern으로 redacted됩니다. task가 `running`이 아니고 더 읽을 새 이벤트가 없으면 종료합니다. 이 명령은 task JSON, runner state, event log, post-mutation trigger를 변경하지 않고 Codex를 호출하지 않습니다.

`cbr transcript TASK_ID`는 저장된 cbr JSONL 로그와, `session_id` 또는 `thread_id`로 찾을 수 있는 Codex 원본 세션 로그에서 주요 대화, tool 호출, patch, final/error event를 사람이 읽기 좋은 형태로 재구성함. `--raw`는 원본 JSONL을 출력함.

`cbr accept TASK_ID`는 `completed` task에만 `review_status=accepted`를 기록함. Worktree-backed task에서는 branch/worktree linkage를 human output에 표시하고 같은 queue lock 안에서 post-accept apply path를 시도함. Fast-forward apply가 가능하면 main에 반영하고 apply metadata를 기록함. Clean stale-base rebase는 re-review로 되돌리고, stale-base conflict는 bounded `worktree_conflict_fix` subtask를 enqueue함. `cbr reject TASK_ID`는 `review_status=rejected`를 기록하고, `--follow-up`을 붙이면 `review_status=needs_followup`을 기록함. Reject는 운영자가 비정상 실행 결과나 후속 처리 필요성을 표시할 수 있도록 non-completed task에서도 허용함. `reject --follow-up`은 새 task를 생성하지 않고 원 task branch/worktree를 가리키는 `review_follow_up` metadata를 기록함.

`cbr resolve TASK_ID --resolution VALUE`는 `failed`, `blocked_user`, 또는 `completed + rejected/needs_followup` task에 운영상 처리 결정을 기록합니다. 허용값은 `wont_fix`, `superseded`, `manual`, `smoke`, `duplicate`입니다. resolution이 있는 task는 기본 `cbr list`와 `review-next` 후보에서 제외되고, `cbr list --all` 또는 `cbr summary TASK_ID`에서 확인합니다.

`cbr rate-limits`는 저장된 sanitized rate-limit evidence event를 조회함. `--json`을 붙이면 evidence JSON 배열을 출력함.

`cbr cooldown show`는 runner state의 `global_cooldown_until`, 활성 여부, approximate remaining duration을 표시합니다. `cbr cooldown clear`는 `global_cooldown_until`을 `null`로 지우고, 즉시 실행 가능한 작업이 있을 수 있으므로 post-mutation trigger를 실행합니다. Set/clear는 작은 sanitized `cooldown_updated` audit event를 기록합니다.

`cbr cooldown set VALUE`는 운영자가 알고 있는 다음 usage/rate-limit reset 시각을 기존 state mechanism의 `global_cooldown_until`에 기록합니다. 입력은 local timezone 기준으로 해석하며, 저장값은 `interpreted_reset_at + 60 seconds`입니다. 이 safety offset은 reset 직전 재시도를 피하기 위한 고정 기본값입니다. 출력은 원본 입력, zero-padded local `interpreted_reset_at`, 실제 저장되는 `effective_cooldown_until`, 그리고 현재 시각 기준 duration을 표시해 잘못 입력한 시간을 운영자가 바로 확인할 수 있게 합니다.

`cbr pause show`는 runner state의 `runner_pause.active`, `reason`, `paused_at`, `paused_by`를 표시합니다. `cbr pause set --reason TEXT`는 expiry 없는 global queue admission pause를 설정합니다. 활성화되면 `enqueue`는 task를 쓰지 않고 `cbr is currently unavailable` 오류로 종료하며, `run-next`/`run-loop`은 새 implementation task, reviewer Codex call, bounded auto-fix enqueue를 시작하지 않습니다. 이 state는 `global_cooldown_until`과 별개이며 rate-limit cooldown semantics를 재사용하지 않습니다. 또한 apply-plan이 task에 기록할 수 있는 task-level `status=paused`와도 다른 control-plane 상태입니다. Pause set은 runner를 깨우지 않습니다. `cbr pause clear`는 pause state를 기본값으로 되돌리고, runnable task 또는 review candidate가 다시 처리 가능해질 수 있으므로 post-mutation trigger를 실행합니다. Set/clear는 작은 sanitized `runner_pause_updated` audit event를 기록하며, state와 event에는 공개 가능한 짧은 reason과 operator id만 저장합니다.

지원 형식은 자연어 parser 없이 제한된 형식만 허용합니다. Time-only 형식은 `H:M`, `HH:M`, `H:MM`, `HH:MM`이며 오늘 해당 local clock time이 미래이면 오늘, 이미 지났으면 내일로 해석합니다. Date-time 형식은 slash `M/D H:M`, `MM/DD HH:MM`, dash `M-D H:M`, `MM-DD HH:MM`, year date `YYYY-MM-DD H:M` 또는 `YYYY-MM-DD HH:MM`을 지원합니다. Slash date는 항상 month/day이며 day/month로 해석하지 않습니다. Timezone이 포함된 ISO datetime은 정확한 advanced input으로 허용합니다. Relative duration은 `+90m`, `+2h30m`, `+1d3h`처럼 day/hour/minute 조합을 지원합니다. Hour는 `0..23`, minute은 `0..59`만 허용합니다. 명시적 date-time이 과거이면 다음 해로 roll forward하지 않고 오류로 종료하며, 해석된 reset 시각이 현재보다 7일을 초과해 먼 경우에도 오류로 종료합니다.

`cbr events`는 append-only event log에서 최근 event를 조회함. 기본 출력은 human-readable table이고, `--json`은 event object 배열을 출력함. `--task-id`로 특정 task event만 필터링할 수 있고 `--limit`으로 최대 출력 개수를 제한함.

`cbr prune --notifier-cursor-state PATH`는 local notifier cursor state를 read-only로 확인한 뒤 old event JSONL deletion safety에 반영합니다. Cursor v1의 canonical identity는 `current_event_file` + `current_byte_offset`이며, `last_processed_event_id`와 timestamp field는 optional checkpoint metadata입니다. Cursor state가 missing, malformed, unreadable이거나 configured `event_dir` 밖의 event file을 가리키면 event pruning은 skipped warning으로 block됩니다. Cursor가 현재 event file을 fully processed 하지 않았으면 해당 file은 삭제되지 않습니다. Core runner에는 external notifier adapter, Telegram token/chat id, ack/snooze/mute schema가 없고, adapter state/config는 public repository 밖의 local/private opt-in surface로 둡니다.

`cbr dashboard`는 local read-only operator overview HTTP server를 실행합니다. 기본 bind는 `127.0.0.1:8765`이며 `--host`, `--port`로 변경할 수 있습니다. Browser를 자동으로 열지 않고, 인증/token 설정을 추가하지 않으며, queue/task/review/event/state를 변경하는 route를 제공하지 않습니다.

Routes:

- `GET /`: dense operator dashboard HTML. 첫 화면은 queue overview(total, active, runnable, needs resume, capacity/running), review-needed backlog, accepted-unapplied, failed/blocked/usage exhausted, running/stale progress, cooldown/rate-limit, recent sanitized events, index warning을 보여줍니다.
- `GET /api/dashboard`: 같은 sanitized read model JSON. HTML은 이 contract를 재사용하며 raw queue/log file을 직접 우회해서 읽지 않습니다.
- `HEAD /`, `HEAD /api/dashboard`: read-only health/header check.
- `POST`, `PUT`, `PATCH`, `DELETE`: `405`와 read-only error를 반환합니다.

Dashboard action affordance는 mutation button/form이 아니라 CLI command hint만 표시합니다. 예시는 `cbr list --needs-review`, `cbr review-next --dry-run`, `cbr worktree apply TASK_ID --dry-run`, `cbr events --limit 10`, `cbr index rebuild --dry-run`처럼 운영자가 terminal에서 별도 실행할 수 있는 명령입니다.

Privacy limits: dashboard data는 `build_dashboard_overview(config)`의 sanitized output만 렌더링합니다. Raw prompt, full log/transcript, session/thread id, credential/token-like value, private worktree/local path, unsanitized event payload는 HTML/API에 포함하지 않습니다. Recent events table은 event type, occurred_at, task id, project id만 표시합니다.

Index warning behavior: SQLite read index가 없거나 schema mismatch, stale source count, unreadable DB처럼 usable하지 않으면 warning을 화면 상단에 계속 표시하고 canonical fallback summary를 사용합니다. 운영자는 `cbr index rebuild --dry-run`으로 rebuild impact를 먼저 확인할 수 있습니다.

`cbr maintenance direct-worktrees`는 cbr task metadata가 없는 direct operator git worktree를 audit하거나 보수적으로 정리합니다. Target repository는 기본적으로 command를 실행한 current working directory의 git root입니다. `--repo-root PATH`를 주면 해당 path의 git root를 target repository로 사용합니다. Queue lock, event log, configured task `worktree_root` 같은 runtime path는 계속 `--config`의 cbr config에서 가져옵니다. Discovery는 target repository에서 `git worktree list --porcelain`로 등록된 worktree만 사용하며, filesystem glob scan은 하지 않습니다. Main worktree와 configured `worktree_root` 아래 task worktree는 제외합니다.

초기 allowlist는 local branch `codex/*`와 current repository의 sibling path 중 basename이 `<repo-name>-`로 시작하는 worktree로 제한합니다. Allowlist 밖 branch/path는 삭제 대상이 아니며 blocked/refused candidate로 보고합니다. Allowlisted candidate는 current target branch 기준 merged 여부와 worktree dirty 여부로 `merged+clean`, `merged+dirty`, `unmerged+clean`, `unmerged+dirty` 중 하나로 분류합니다. `merged+clean`만 cleanup eligible입니다.

`--dry-run`은 eligible과 blocked/refused candidate를 compact human output 또는 structured JSON으로 보고하고 mutation하지 않습니다. `--apply`는 runner lock 아래에서 git worktree registry와 candidate state를 다시 읽은 뒤, 여전히 eligible인 candidate에만 `git worktree remove <path>`와 `git branch -d <branch>`를 실행합니다. Force remove/delete는 지원하지 않으며 fetch, pull, push, rebase, merge, cherry-pick도 실행하지 않습니다. Worktree removal이 성공했지만 local branch deletion이 실패하면 partial result로 보고하고 `git branch -D`로 재시도하지 않습니다. Apply 시 event log에는 branch, sanitized display path, head, classification, target, removal/deletion booleans, blockers만 기록합니다.

`cbr doctor`는 저비용 health check임. resolved `queue_dir`, `log_dir`, `event_dir`, `lock_file`, `state_file` 경로, runtime directory 접근 가능 여부, configured Codex executable path, resolved Codex executable path, executable availability, bounded `codex --version` output, model selection rule 이름과 allowlisted override key, execution target freshness 상태와 stale warning, global cooldown, runner pause, active lock age/pid/liveness, status별 task 수, needs-review completed task 수, resolved failed/blocked task 수, resolved completed-review task 수, runnable task 수, cooldown task 수, mechanical auto-review enable 상태, reviewable completed task 수, startup/no-progress stall evidence를 표시함. Version 확인은 configured executable에 `--version`만 붙여 짧은 timeout으로 실행하며, `codex exec`를 호출하지 않음. Version command 실패, 빈 output, timeout은 warning으로 보고하고 doctor 실패로 취급하지 않음. configured/current project root가 git repository 안에 있으면 branch, dirty status, upstream 또는 local `origin/main` 대비 ahead/behind count도 표시함. `worktree.task_branches` JSON section과 human `task_branches` subsection은 task worktree branch lifecycle을 read-only로 표시합니다. 여기에는 task id, local branch name/existence/head, retained/cleaned/pruned metadata, apply/cleanup/prune status, path existence boolean, and locally known/configured remote task branch refs가 포함됩니다. git check는 local repository metadata만 읽고 fetch/pull/push 같은 network operation을 실행하지 않음. git executable 없음, git repository 아님, upstream 없음, remote ref 조회 불가 같은 상태는 warning으로 보고하고 doctor 실패로 취급하지 않음. `--json`을 붙이면 같은 정보를 JSON으로 출력함. error check가 있으면 non-zero로 종료하고 warning은 종료 코드에 영향을 주지 않음.

`cbr worktree discard-stale-applied TASK_ID --dry-run|--apply --resolution superseded|wont_fix|duplicate|manual --reason REASON`은 retained worktree task의 `execution_apply_status=applied` metadata가 현재 apply target에 포함되지 않는 stale state일 때만 사용합니다. `--dry-run`은 stale applied metadata와 eligibility만 보고합니다. `--apply`는 worktree나 branch를 삭제하지 않고, stale applied metadata를 `execution_stale_applied_discard`에 보존한 뒤 task를 `review_status=rejected`, `resolution=<resolution>`, `execution_apply_status=discarded` 상태로 전환해 기존 discard cleanup path가 처리할 수 있게 합니다. 이후 운영자는 `cbr worktree cleanup TASK_ID --dry-run`으로 discard cleanup 가능 여부를 다시 확인하고, 적합하면 `--apply`로 worktree만 제거합니다. Branch pruning policy는 바뀌지 않으며 discard-cleaned branch는 evidence로 보존됩니다.

`cbr policy-proposals execution-target-freshness`는 configured `execution_targets`의 freshness metadata를 읽고 read-only policy proposal report를 생성합니다. 이 command는 apply mode, config rewrite, task mutation, model replacement, rule replacement를 지원하지 않습니다. JSON report는 `schema_version`, `kind`, `proposal_class`, `mode`, `generated_at`, `mutation`, `summary`, `items`, `proposals`, `decision_cards`, `warnings`, `errors`를 포함합니다. Fresh target은 `items`에만 표시되고 proposal을 만들지 않으며, stale 또는 missing freshness metadata target만 `proposals`에 `review_execution_target_freshness` 또는 `add_execution_target_freshness_metadata` action으로 표시됩니다. `decision_cards`는 실행 보고 상태와 사용자 결정 상태를 분리해 표시합니다. Normal execution target freshness proposal은 `user_decision_status=decision_required`이고, direct model pin migration proposal은 `user_decision_status=approval_blocked`로 표시됩니다. `default_execution_config` 또는 `model_selection_rules`의 direct model pin은 raw model value 없이 `target_kind=direct_model_pin` item으로 표시되고, `migrate_direct_model_pin_to_execution_target` read-only proposal로만 노출됩니다. Direct model pin proposal은 local config freshness apply 대상이 아니며, model/rule 변경에는 별도 operator decision이 필요합니다.

`cbr policy-proposals preview PROPOSAL_JSON`은 기존 `policy_proposal_report` JSON을 읽어 read-only preview report 또는 human summary로 렌더링합니다. 현재 지원 proposal class는 `execution_target_freshness`뿐이며, preview item은 `target`, `recommended_action`, `would_change: none`, `apply_ready: false`, `blocked_reason: preview_only_no_apply_target`을 표시합니다. Preview `decision_cards`는 같은 사용자 결정 축을 유지하되 preview-only apply block reason도 함께 표시합니다. 이 command도 config rewrite, task mutation, model replacement, rule replacement, apply를 수행하지 않습니다.

`cbr policy-proposals approval-template PREVIEW_JSON`은 기존 `policy_proposal_preview` JSON을 읽어 사람이 편집할 수 있는 approval template를 stdout으로 렌더링합니다. Template는 `source_preview_sha256`, 각 approval의 `source_item_sha256`, `proposal_id`, `target`, `recommended_action`, `approved: false`, `reviewer`, `reviewed_at`, `decision_note`를 포함합니다. `decision_cards`는 각 approval entry를 `user_decision_status=decision_pending`으로 표시해 approval file 편집이 사용자 결정 축임을 분리합니다. 이 command는 approval file을 저장하지 않고, config rewrite, task mutation, model replacement, rule replacement, apply도 수행하지 않습니다.

`cbr policy-proposals validate-approval APPROVAL_JSON --preview PREVIEW_JSON`은 사람이 편집한 approval JSON을 source preview JSON과 대조합니다. Validator는 approval/preview schema, `source_preview_sha256`, 각 approval의 `source_item_sha256`, approved item의 `reviewer`, ISO datetime `reviewed_at`, `decision_note`를 검사하고 `policy_proposal_approval_validation` report를 출력합니다. Validation `decision_cards`는 승인된 항목을 `user_decision_status=approved`, 승인되지 않은 항목을 `not_approved`, 검증 오류 항목을 `invalid`로 표시합니다. 이 command는 approval/config/task를 변경하지 않고 apply도 수행하지 않습니다.

`cbr policy-proposals apply APPROVAL_JSON --preview PREVIEW_JSON --config-target CONFIG_JSON --dry-run|--apply [--approve] --json`은 승인된 `execution_target_freshness` proposal을 명시적인 local/private config JSON에만 guarded apply합니다. `--dry-run`은 config를 바꾸지 않고 approval 재검증, config-target eligibility, freshness before/after snapshot, compact diff metadata를 출력합니다. `--apply`는 `--approve` 없이는 실패하며, approval/preview hash, approved item metadata, proposal id/target/action/class, 현재 config freshness 상태를 다시 확인한 뒤 `execution_targets.<alias>.freshness` metadata만 갱신합니다. Repo public source/docs/examples/tests 경로, repo runtime state 경로, missing/unparseable/dirty config, unsupported proposal class/action/schema/target path, missing target은 거부됩니다. Apply 성공 시 sanitized `policy_proposal_applied` event payload에는 hashes, proposal id, target alias, changed freshness keys만 남기고 raw paths, logs, prompts, decision note body는 넣지 않습니다.

Runner lock recovery treats a lock as recoverable immediately when metadata contains a pid for the same hostname and that pid is no longer alive. Unknown host, missing/invalid pid, invalid metadata, and cross-host locks fall back to the age-based stale threshold.

Doctor는 기본적으로 Codex application bundle 안의 별도 executable과 configured CLI를 비교하지 않음. macOS나 특정 app install layout을 가정하지 않기 위함임. 운영자가 app-bundled CLI와 standalone CLI 차이를 확인해야 하는 환경에서는 별도 수동 조사로 처리함. Routine doctor는 대형 binary hash를 계산하지 않음. Hash는 향후 `--verbose` 또는 deep diagnostic check에서 명시적으로 요청할 때만 고려함.
