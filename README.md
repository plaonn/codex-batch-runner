# codex-batch-runner

`codex-batch-runner`는 Codex CLI 작업을 로컬 파일 큐에서 하나씩 실행하는 배치 runner입니다.

목표는 스케줄러가 자주 실행되더라도 실제 처리할 작업이 있을 때만 `codex exec --json` 또는 `codex exec resume ... --json`을 호출하여 불필요한 Codex 토큰 소모를 줄이는 것입니다.

## 현재 상태

현재는 로컬 beta 운영을 목표로 core flow를 구현하고 있습니다. 파일 기반 queue, lock, cooldown, Codex JSONL parsing, 자동 검토, bounded auto-fix, worktree 격리 실행, worktree apply 흐름을 포함합니다. 실제 Codex CLI JSONL schema는 버전별 차이가 있을 수 있으므로 runner는 원본 JSONL 로그를 보존하고, 최종 응답과 session/thread id는 best-effort로 파싱합니다.

구현 기준은 [docs/spec.md](docs/spec.md)에 있습니다. 로컬 beta 설치와 macOS 운영자 설정은 [docs/operator-installation.md](docs/operator-installation.md)를 참고하십시오. 여러 프로젝트에서 beta로 운영하는 실무 흐름은 [docs/beta-operations.md](docs/beta-operations.md)를 참고하십시오.
향후 notification, Telegram adapter, dashboard, optional SQLite index 계획도 [docs/spec.md](docs/spec.md)에 정리되어 있습니다. 개인 운영 환경에서 별도 roadmap 또는 task dashboard가 필요하면 [examples/ROADMAP.local.example.md](examples/ROADMAP.local.example.md)와 [examples/TASKS.local.example.md](examples/TASKS.local.example.md)를 복사해 gitignore되는 로컬 문서로 관리할 수 있습니다.

## 설치

Python 3.11 이상이 필요합니다. runtime dependency는 없습니다.

기본 운영에서는 다른 Codex thread가 전역 skill을 통해 작업을 queue에 등록하고 launchd/systemd 같은 스케줄러가 `run-next`를 호출하는 방식을 권장합니다. 자동화 경로에서는 `PATH`에 의존하지 않고 config와 절대 경로를 사용하는 것이 안전합니다.

개발 checkout에서 바로 실행할 수 있습니다.

```bash
PYTHONPATH=src python3 -m codex_batch_runner --help
```

운영자가 직접 상태를 조회하거나 transcript를 검토하고 `accept`/`reject`를 기록하는 환경에서는 `cbr` console script 설치가 편리합니다. 자동화의 필수 조건은 아닙니다.

```bash
python3 -m pip install -e .
cbr --help
```

macOS `launchd` 설정, config discovery, `doctor` 점검, 다른 프로젝트에서 enqueue/check하는 흐름은 [operator installation guide](docs/operator-installation.md)에 정리되어 있습니다.

테스트 실행:

```bash
PYTHONPATH=src python3 -m unittest discover -v
```

## 기본 사용법

작업 등록:

```bash
PYTHONPATH=src python3 -m codex_batch_runner enqueue --cwd /path/to/repo --title "README 개선" --prompt "README를 개선하고 테스트를 실행해"
```

프로젝트 metadata를 함께 지정할 수 있습니다:

```bash
PYTHONPATH=src python3 -m codex_batch_runner enqueue \
  --cwd /path/to/repo \
  --project codex-batch-runner \
  --category implementation \
  --label queue \
  --created-by operator \
  --title "README 개선" \
  --description "README와 관련 테스트를 함께 확인한다." \
  --prompt "README를 개선하고 테스트를 실행해"
```

prompt 파일로 등록:

```bash
PYTHONPATH=src python3 -m codex_batch_runner enqueue --cwd /path/to/repo --prompt-file task.md
```

실행 비용 힌트를 함께 지정할 수 있습니다. `--profile`은 config의 `execution_profiles`에 정의된 cbr 실행 profile 이름이고, `--model`과 `--codex-profile`은 해당 task에만 적용되는 Codex CLI override입니다.

```bash
PYTHONPATH=src python3 -m codex_batch_runner enqueue \
  --cwd /path/to/repo \
  --profile small \
  --model gpt-5-small \
  --codex-profile batch-small \
  --config-override model_reasoning_effort=low \
  --prompt-file task.md
```

Profile은 비용 힌트이며 correctness를 낮추기 위한 장치가 아닙니다. `--config-override`는 allowlisted Codex config key만 허용하며, 임의의 `-c key=value` 주입은 허용하지 않습니다. 현재 allowlist는 `model_reasoning_effort`, `model_reasoning_summary`, `model_verbosity`입니다.

의존성 있는 작업 등록:

```bash
PYTHONPATH=src python3 -m codex_batch_runner enqueue --cwd /path/to/repo --id task-a --prompt-file a.md
PYTHONPATH=src python3 -m codex_batch_runner enqueue --cwd /path/to/repo --id task-b --depends-on task-a --prompt-file b.md
```

목록 확인:

```bash
PYTHONPATH=src python3 -m codex_batch_runner list
PYTHONPATH=src python3 -m codex_batch_runner list --project codex-batch-runner
PYTHONPATH=src python3 -m codex_batch_runner list --project-root /path/to/repo
PYTHONPATH=src python3 -m codex_batch_runner list --category implementation
PYTHONPATH=src python3 -m codex_batch_runner list --label queue
PYTHONPATH=src python3 -m codex_batch_runner list --unreviewed
PYTHONPATH=src python3 -m codex_batch_runner list --needs-review
PYTHONPATH=src python3 -m codex_batch_runner list --verbose
PYTHONPATH=src python3 -m codex_batch_runner list --graph
PYTHONPATH=src python3 -m codex_batch_runner list --demo
PYTHONPATH=src python3 -m codex_batch_runner list --demo --graph
PYTHONPATH=src python3 -m codex_batch_runner list --color=always
PYTHONPATH=src python3 -m codex_batch_runner list --watch
```

기본 `list` 출력은 운영자가 봐야 할 남은 작업 중심의 compact human list입니다. `archived`, non-worktree `completed + accepted`, applied worktree task, resolution이 기록된 task는 기본 출력에서 숨기고, accepted-but-unapplied worktree task는 표시합니다. 전체 이력이 필요하면 `--all`을 사용합니다. terminal width가 없거나 80 이상이면 첫 줄은 `PROJECT`, `ID`, `STATUS`, `ATT`, `DEPS`, `NOTE`를 표시합니다. 표시 task는 project별 `[project-id]` section 아래에 묶고, `parent_task_id`, `subtask_for`, `root_task_id`, `blocking_subtask_ids`로 parent/root 관계가 확인되는 task는 title row에 ASCII tree connector를 붙여 parent 아래에 표시합니다. 기본 list에서 parent/root task가 표시되면 자체 상태만으로는 숨겨질 completed accepted subtask도 그 parent/root 아래에 함께 표시합니다. 각 task는 최소 두 줄로 표시하며 첫 줄에는 project id, task id, status, attempts, 첫 dependency, 첫 note segment를 표시하고, 둘째 줄부터는 사람이 읽는 title, dependency continuation, note continuation을 같은 physical row에서 병렬로 표시합니다. TTY compact output에서 terminal width가 80보다 작으면 table 대신 project section과 `STATUS:`, `ID:`, `PROJECT:`, `TITLE:`, `DEPS:`, `NOTE:` block layout을 사용해 긴 task id, title, dependency, note가 일반적인 terminal width를 넘지 않게 접습니다. `--json`과 `--verbose`는 이 narrow block layout 영향을 받지 않습니다. 중간 폭 table에서는 `NOTE` 폭을 보존하기 위해 `ID`와 `DEPS`의 긴 task id를 middle ellipsis로 축약할 수 있습니다. `DEPS`는 dependency title이 아니라 task id를 표시하며, dependency가 여러 개이면 한 줄에 하나씩 이어지는 continuation row에 세로로 표시합니다. 완료되어 현재 정책상 만족된 dependency도 숨기지 않으며, 색이 꺼진 출력에서는 `dep-id (done)`처럼 최소 텍스트 표시를 붙입니다. 아직 만족되지 않은 dependency는 `dep-id (blocked)`, `dep-id (not_accepted)`, `dep-id (not_applied)`, `dep-id (missing)`처럼 `DEPS` 열에서 직접 구분하고, `blocked_dependency` 상태에서는 같은 blocker 정보를 `NOTE`에 `blocked by ...`로 반복하지 않습니다. Dependency가 없으면 첫 줄에 `-`를 표시합니다. `NOTE`는 cooldown/resume timing, capacity blocker, non-default scheduling metadata, review 상태, bounded review/fix chain 상태, blocking subtask 요약과 active subtask timing, resolution, failed error, startup stall evidence, running runtime/progress, completed elapsed/duration 같은 운영 정보를 사람이 읽는 segment로 표시하고, segment가 여러 개이면 continuation row의 `NOTE` 열에 이어서 표시합니다. 기본 compact `NOTE`는 `profile=...`, `model=...`, `codex_profile=...` metadata를 표시하지 않습니다. 정보가 없으면 `-`를 표시합니다. 스크립트에서는 사람이 읽는 list 대신 `--json` 출력을 사용해야 합니다.

`list --graph`는 기본 compact list와 별도의 human dependency edge list를 출력합니다. 각 row의 edge 의미는 `TASK`가 `WAITS_FOR` dependency를 기다린다는 뜻입니다. 출력 열은 `PROJECT`, `TASK`, `STATUS`, `WAITS_FOR`, `DEP_STATE`, `TASK_TITLE`, `DEP_TITLE`이며, project-task-subtask tree connector를 사용하지 않습니다. Dependency가 없는 task는 `WAITS_FOR=-`, `DEP_STATE=none`으로 표시합니다. `DEP_STATE`는 `done`, `blocked`, `not_accepted`, `not_applied`, `missing`을 사용해 기본 `DEPS` 열과 같은 readiness policy를 반영합니다. `--graph`는 human renderer만 바꾸며 `--json`을 함께 쓰면 기존과 같은 raw task JSON 배열을 출력합니다. `--watch --graph`는 같은 graph renderer를 반복 갱신합니다.

`list --demo`는 실제 queue나 runner state를 읽거나 쓰지 않고 in-memory synthetic task set을 기존 list renderer에 통과시킵니다. 기본 compact list, `--graph`, `--verbose`, `--color`, narrow layout, `--json` renderer를 작업이 없는 환경에서도 확인하기 위한 sample surface입니다. Demo JSON task에는 `"demo": true`가 포함됩니다.

`STATUS`는 운영자가 현재 실행 가능 여부를 판단할 수 있는 effective status입니다. 내부 실행 상태를 기본으로 하되, raw status가 `runnable` 또는 `needs_resume`이어도 dependency readiness 정책상 아직 선택될 수 없는 task는 `blocked_dependency`로 표시합니다. `blocking_subtask_ids`에 아직 accepted되지 않은 task가 남아 있으면 parent/root task는 human output에서 `waiting_subtasks`로 표시하고, 그중 failed/blocked/review-failed 계열 subtask가 있으면 `subtasks_blocked`로 표시합니다. 그 밖에 `completed + unreviewed`는 `awaiting_review`, `completed + rejected`는 `review_failed`, `completed + needs_followup`은 `needs_followup`, accepted-but-unapplied worktree task는 `accepted_unapplied`, resolution이 기록된 task는 `resolved`로 표시합니다. Raw task status는 task JSON과 `list --json`에서 그대로 유지됩니다. Startup stall evidence는 현재 재시도 대상이면 retry evidence로, 이미 완료되었거나 해결된 task이면 history로 `NOTE`에 표시해 과거 이력이 현재 장애처럼 보이지 않게 합니다. `needs_resume` task는 cooldown 중이면 `resume in 12m (14:32)`, 바로 실행 가능하면 `resume ready`를 표시합니다. `running` task는 `NOTE`에 `running for 12m` 또는 `running for 1h 04m`처럼 시작 후 경과 시간을 표시하고, progress metadata가 있으면 `last event 35s ago` 또는 `no progress 9m` 같은 최근 활동 상태도 함께 표시합니다. 초 단위는 1분 미만 elapsed/age에만 표시합니다. 완료됐지만 review/apply 등 후속 조치가 남아 list에 보이는 task는 `completed 8m ago`와 `duration 21m` 같은 timing segment를 표시할 수 있습니다. Global cooldown 또는 reviewer Codex cooldown이 active이면 human `list` 상단에 banner를 표시합니다.

Human 출력은 `--color=auto|always|never`를 지원합니다. 기본값 `auto`는 TTY에서만 색을 사용하고 `NO_COLOR`가 설정되어 있으면 색을 끕니다. `always`는 강제로 색을 켜고, `never`는 항상 끕니다. Ordinary task id display는 stable color를 받지만 `DEPS` blocker label은 stable id color가 아니라 dependency task의 effective status style을 사용합니다. Satisfied dependency는 color-enabled 출력에서 dim style로 표시해 inactive dependency임을 구분합니다. Missing dependency는 special danger style과 `:missing` suffix를 사용합니다. Completed-but-unaccepted dependency는 color-enabled 출력에서 `dep-id:not_accepted`, color-off 출력에서 `dep-id (not_accepted)`처럼 명시적 suffix를 유지합니다. `PROJECT`는 title보다 옅은 색 계열로 표시하고, title row는 기본 읽기 색을 유지합니다. `STATUS`는 color-enabled human output에서 foreground 색만이 아니라 background가 있는 label 형태로 표시합니다. Active/attention statuses인 `running`, `awaiting_review`, `reviewing`, `needs_resume`, `waiting_subtasks`, `cooldown`, `usage_exhausted`, `failed`, `review_failed`, `needs_followup`, `blocked_user`, `subtasks_blocked`는 strong colored background를 사용합니다. Passive/informational statuses인 `runnable`, `blocked_dependency`, `completed`, `accepted`, `resolved`, `archived`는 neutral/dark gray background와 colored foreground를 사용합니다. 모든 background label은 가시성을 위해 명시적 foreground 색을 함께 사용합니다. 색이 꺼지면 같은 상태 문자열과 `(done)`, `(blocked)`, `(not_accepted)`, `(missing)` 같은 텍스트 상태 표시만 사용하며, `--json` 출력에는 ANSI color code를 넣지 않습니다.

Small/light execution profile task는 default compact title 앞에 `[S]` marker를 붙이고, deep/high-cost profile task는 `[D]` marker를 붙입니다. Default/unspecified profile은 marker가 없습니다. 이 marker는 ASCII만 사용합니다.

`list --verbose`는 사람이 읽는 table에 `PROFILE`, `RAW_STATUS`, `LAST_RESULT`, `LAST_RUN`, `LAST_ERROR` 열을 추가합니다. `PROFILE`은 compact `NOTE`에서 숨긴 `profile=...`, `model=...`, `codex_profile=...` metadata를 표시합니다. `RAW_STATUS`는 task JSON에 저장된 원래 status를 표시해 effective `STATUS`와 구분합니다. 나머지 열은 `last_result.status`, `last_result.summary`, optional commit/push metadata, task `git_status`, `last_run`의 command/returncode/duration, `last_error` 한 줄 요약을 표시하며, 값이 없으면 `-`를 표시합니다. `list --json` 출력은 `--verbose` 여부와 관계없이 JSON task 배열을 그대로 출력하며, 새 task에는 `title`과 `description` metadata가 포함될 수 있습니다.

`list --watch`는 현재 filter 조건으로 compact human list를 반복 갱신합니다. 기본 refresh interval은 2초이며 `--interval`로 조정할 수 있습니다. Watch mode는 표시 제어만 제공하며 `q`는 종료, `r`은 즉시 refresh, `+`/`-`는 interval 조절입니다. Queue 상태를 바꾸는 accept/reject/archive/run-next 같은 mutation은 watch mode에서 수행하지 않습니다. `--watch`와 `--json`은 함께 사용할 수 없습니다.

다음 실행 가능한 작업 하나 처리:

```bash
PYTHONPATH=src python3 -m codex_batch_runner run-next
```

작업 상세:

```bash
PYTHONPATH=src python3 -m codex_batch_runner show task-a
PYTHONPATH=src python3 -m codex_batch_runner summary task-a
PYTHONPATH=src python3 -m codex_batch_runner review-bundle task-a
PYTHONPATH=src python3 -m codex_batch_runner review-bundle task-a --json
PYTHONPATH=src python3 -m codex_batch_runner review-next --dry-run
PYTHONPATH=src python3 -m codex_batch_runner review-next --dry-run --project codex-batch-runner --json
PYTHONPATH=src python3 -m codex_batch_runner review-next --apply --mechanical-auto-accept --json
PYTHONPATH=src python3 -m codex_batch_runner review-next --apply --reviewer-codex --json
```

`review-bundle`은 향후 reviewer Codex 또는 사람 검토자가 현재 대화 문맥 없이 task 결과를 재검토할 수 있도록 Markdown-like report를 출력합니다. 기본 출력과 `--json` 모두 task metadata, sanitized prompt excerpt, status/review/resolution, dependencies/blockers, bounded review/fix chain metadata, `last_result`, `last_run`, worktree/follow-up linkage, changed files, verification, `last_error`, relevant log paths, completion-time `task_git_status_snapshot`, `current_git_repository`, inferable commit information, safely scoped commit or working tree diff/stat, and public/private safety policy를 포함합니다. Commit information에는 보고된 task commit과 현재 `HEAD`의 ancestry 상태도 포함합니다. `equal`은 보고된 commit이 현재 `HEAD`와 같다는 뜻이고, `ancestor`는 보고된 commit 위에 후속 commit이 더 쌓인 정상 상태로 취급합니다. `not_reachable`은 현재 `HEAD`에서 보고된 commit에 도달할 수 없다는 뜻이므로 자동 검토에서는 human check 대상입니다. Worktree-backed task에서는 compatibility alias인 `current_git_repository`가 task worktree state를 가리키며, `current_main_repository`와 `current_task_worktree_repository`를 별도로 표시합니다. Compatibility를 위해 JSON에는 legacy `git_status`와 `git_repository` aliases도 유지됩니다. 원본 JSONL transcript 내용은 기본적으로 포함하지 않으며, 명령은 read-only이고 Codex를 호출하거나 task를 accept/reject하지 않습니다.

`review-next`의 기본 동작은 read-only report입니다. `completed` task 중 `review_status`가 `unreviewed`, `rejected`, `needs_followup`인 가장 오래된 항목 하나를 골라 concise review report를 출력합니다. `--project`, `--project-root`, `--category`, `--label`로 대상을 좁힐 수 있고, `--json`은 같은 report를 structured JSON으로 출력합니다. Report에는 review bundle의 핵심 정보와 `status=completed`, final result status, `last_error`, verification, changed files, dependency readiness, current git dirty status, current unpushed commit 여부, commit ancestry, task metadata/review bundle에서 감지 가능한 공개/비공개 안전 위반 여부에 대한 mechanical gate가 포함됩니다. Worktree-backed dependency는 `completed + accepted`만으로 ready가 되지 않고 integration target에 `execution_apply_status=applied`가 기록된 뒤 ready가 됩니다. Stored task `git_status`는 completion-time snapshot evidence로 표시되며, current repository inspection에서 unpushed 상태를 확인할 수 있으면 stale snapshot의 old ahead/push 정보는 gate 결과로 사용하지 않습니다. 저장된 reviewer `needs_fix` result가 있으면 bounded auto-fix planner가 report-only로 skip reason 또는 sanitized fix task draft를 함께 보여줍니다. 기본 report는 task JSON을 변경하지 않고, follow-up task를 만들지 않으며, Codex를 호출하지 않습니다.

`review-next --apply`는 runner와 같은 queue lock 아래에서 실행되는 순차 자동 검토 phase입니다. 기본적으로는 적용을 거부하고 `needs_human`을 보고합니다. `--mechanical-auto-accept`를 함께 지정하거나 config에서 `"auto_review_mechanical_accept": true`를 명시한 경우에만 모든 local mechanical gate가 통과한 task를 `review_status=accepted`로 변경합니다. Gate가 실패하거나 상태가 모호하면 reviewer Codex를 호출하지 않고 `review_status`를 그대로 둡니다. 적용 직전에는 task `updated_at`, `last_result`, current repository head/dirty/ahead 상태, inferred commit 정보가 gate 계산 시점과 같은지 다시 확인하여 stale state이면 accept를 적용하지 않습니다.

Reviewer Codex 안전 모델은 [docs/spec.md](docs/spec.md)에 정리되어 있습니다. Reviewer Codex는 명시적 config opt-in 또는 `review-next --apply --reviewer-codex`, `auto_review_codex_max_calls_per_run >= 1`, cooldown, bundle/diff 크기 제한이 모두 충족될 때만 한 command invocation에서 completed task 한 건에 대해 호출됩니다. Reviewer 입력은 sanitized review bundle, prompt/result 요약, commit diff/stat, verification summary로 제한하며 raw logs, secrets, session/thread ids는 기본적으로 전달하지 않습니다. Reviewer decision은 `pass`, `needs_fix`, `needs_human`, `failed_review` 중 하나이고, 자동 accept는 high-confidence `pass`와 mechanical/stale-state gate 통과가 모두 확인될 때만 수행됩니다. `needs_fix`는 기본적으로 task를 accept하지 않습니다. 다만 `auto_review_codex_max_fix_loops_per_task >= 1`, reviewer `auto_fix_allowed=true`, `confidence=high`, `auto_fix_risk=low`, 구체적인 `suggested_fix_prompt`, 반복 finding 없음, fresh state 재확인이 모두 통과하면 runner가 `subtask_type=auto_review_fix`인 별도 fix subtask를 enqueue할 수 있습니다. 이 subtask는 `depends_on`을 사용하지 않고 `subtask_for`, `root_task_id`, `parent_task_id`, `blocks_root_completion=true`로 root chain에 연결합니다. Reviewer Codex는 파일을 수정하거나 queue를 직접 변경하지 않으며, 조건을 통과하지 못한 `needs_fix`, `needs_human`, invalid schema, rate-limit은 sanitized skip/enqueue evidence와 chain metadata만 기록하고 human-visible pending state로 남깁니다.

`run-next`는 기본적으로 runnable/needs_resume 구현 작업만 처리합니다. Config에서 `"auto_review_mechanical_accept": true` 또는 `"auto_review_codex_enabled": true`를 명시하면 같은 queue lock 안에서 completed task 한 건에 대한 자동 검토 phase를 먼저 실행합니다. 자동 검토가 worktree task를 accept하면 같은 lock 안에서 post-accept worktree apply path도 시도합니다. Fast-forward apply가 성공한 뒤에야 dependent task가 ready가 되며, stale-base clean rebase는 `review_status=unreviewed`로 되돌려 re-review를 요구합니다. Stale-base conflict는 직접 conflict marker를 편집하지 않고 bounded `worktree_conflict_fix` subtask를 최대 한 개 enqueue합니다. 자동 검토가 task를 accept하거나 reviewer Codex를 호출해 검토 작업을 소비한 경우, 해당 `run-next` invocation은 추가 구현 작업을 시작하지 않습니다. Reviewer Codex가 `needs_human`, `failed_review`, 또는 자동 follow-up으로 이어질 수 없는 `needs_fix`를 반환하면 task에 현재 review fingerprint 기준 backoff marker를 기록합니다. `needs_fix`가 자동 follow-up으로 이어지면 runner는 bounded prompt를 가진 별도 fix task를 enqueue하고 같은 invocation에서는 그 fix task를 실행하지 않습니다. 이후 `run-next` 자동 검토는 task/result/git 상태가 바뀌지 않은 같은 후보를 다시 reviewer Codex에 보내지 않고 다음 검토 후보 또는 runnable 구현 작업으로 넘어갑니다. Gate 실패처럼 task 상태를 변경하지 않는 비실행 가능한 검토 후보만 있는 경우에도 같은 invocation에서 runnable 구현 작업을 계속 선택할 수 있어 오래된 검토 후보 하나가 전체 queue를 계속 막지 않습니다. 자동 검토가 dependent task를 새로 runnable하게 만들거나 auto-fix/conflict-fix task를 enqueue하거나 다음 자동 검토 후보가 바로 처리 가능한 상태로 남아 있으면 기존 post-run trigger 규칙에 따라 scheduler wake-up hook이 실행될 수 있습니다. 구현 task를 완료한 직후에도 global cooldown, task cooldown, pause, dependency gate를 모두 통과한 후속 구현 작업 또는 즉시 actionable한 auto-review 후보가 남아 있으면 같은 wake-up hook이 실행될 수 있습니다. 별도로 운영자가 `pause set`으로 global runner admission pause를 걸어 두면 `run-next`는 queue lock을 잡고 stale `running` recovery만 수행한 뒤 `paused`로 종료하며, 새 구현 작업, auto-review Codex 호출, auto-fix enqueue는 시작하지 않습니다.

로그 확인:

```bash
PYTHONPATH=src python3 -m codex_batch_runner logs task-a
PYTHONPATH=src python3 -m codex_batch_runner logs task-a --cat
```

실행 대화 요약 확인:

```bash
PYTHONPATH=src python3 -m codex_batch_runner follow task-a
PYTHONPATH=src python3 -m codex_batch_runner follow task-a --lines 40 --poll-interval 1
PYTHONPATH=src python3 -m codex_batch_runner transcript task-a
PYTHONPATH=src python3 -m codex_batch_runner transcript task-a --raw
```

`follow`는 실행 중인 task의 attempt JSONL을 polling으로 따라가며 assistant message, command start/finish, command exit code, final JSON, error/rate-limit marker 요약을 compact stream으로 출력합니다. 이 명령은 task state를 변경하지 않고 Codex를 호출하지 않으며 post-mutation trigger를 실행하지 않습니다. task가 아직 실행 중이고 attempt log가 뒤늦게 생기는 경우에도 log directory와 task metadata를 다시 확인합니다. task가 더 이상 `running`이 아니고 현재 로그에서 새 이벤트가 없으면 cleanly 종료합니다.

`transcript`는 기본적으로 cbr JSONL 로그와, `session_id` 또는 `thread_id`로 찾을 수 있는 Codex 원본 세션 로그에서 사용자 메시지, assistant 메시지, tool 호출, patch, final/error event를 사람이 읽기 좋은 형태로 재구성합니다. `--raw`를 붙이면 원본 JSONL 로그를 출력합니다.

배치 결과 검토:

```bash
PYTHONPATH=src python3 -m codex_batch_runner accept task-a --reason "verified locally"
PYTHONPATH=src python3 -m codex_batch_runner reject task-a --reason "tests are missing"
PYTHONPATH=src python3 -m codex_batch_runner reject task-a --follow-up --reason "needs follow-up task"
PYTHONPATH=src python3 -m codex_batch_runner resolve task-a --resolution manual --reason "handled outside cbr"
```

Codex가 `completed`를 반환하면 실행은 완료되지만, 검토 상태는 `unreviewed`로 남습니다. 운영상 진짜 완료로 판단한 뒤 `accept`로 `review_status=accepted`를 기록하는 흐름을 권장합니다.
`accept`는 `completed` task에만 사용할 수 있습니다. Worktree-backed task에서는 `accept`가 task branch/worktree linkage를 삭제하거나 main에 merge하지 않고, human output에 linkage를 함께 표시합니다. `reject`와 `reject --follow-up`은 운영자가 실행 중이거나 실패한 결과를 명시적으로 부정하거나 후속 처리가 필요하다고 표시할 수 있도록 더 넓은 상태에서 사용할 수 있습니다. `reject --follow-up`은 새 task를 자동 생성하지 않으며, 원 task의 branch/worktree를 가리키는 minimal follow-up linkage metadata를 기록합니다.

기본 dependency readiness는 이전 버전과 같이 dependency task의 `status=completed`만 요구합니다. 즉 config의 기본 호환성 정책은 `"dependency_requires_accepted_review": false`이며, dependency가 `completed`이면 `review_status`가 아직 `unreviewed` 또는 `awaiting_review`로 표시되는 completed-but-unreviewed 상태여도 ready로 봅니다. 이는 batch 운영에서 의도한 throughput/latency 선택입니다. 독립적인 후속 작업은 review backlog가 있다는 이유만으로 멈추지 않고 계속 진행할 수 있어야 합니다.

운영상 accepted review까지 완료된 작업만 후속 dependency로 인정하려면 config에 `"dependency_requires_accepted_review": true`를 설정합니다. 이 모드는 dependent work에 더 엄격하고 안전하지만, 검토가 밀리면 더 많은 task가 dependency blocked 상태로 남아 전체 처리량이 낮아질 수 있습니다. 옵션을 켜면 `run-next`는 dependency가 `completed`여도 `review_status=accepted`가 아니면 dependent task를 건너뛰며, `list`, `summary`, `review-bundle`, `review-next`, `doctor`는 미완료 dependency와 completed-but-unaccepted dependency를 구분해 표시합니다. 마이그레이션은 먼저 기본값 `false`로 기존 queue를 검토하고, review workflow가 `accept`를 안정적으로 기록한 뒤 옵션을 `true`로 전환하는 순서를 권장합니다.

`failed`, `blocked_user`, 또는 `completed + rejected/needs_followup` task를 운영상 더 추적하지 않아도 되면 `resolve`로 `resolution`을 기록할 수 있습니다. resolution이 기록된 task는 기본 `list`와 `review-next` 후보에서 제외되고, `list --all`이나 `summary`에서 확인할 수 있습니다.

각 Codex 실행 뒤에는 task에 `last_run` metadata가 기록됩니다. 여기에는 `command_kind`, `returncode`, 시작/종료 시각, `duration_seconds`, 사용한 resume id, log path가 포함됩니다. Execution profile이 적용된 경우 resolved profile, source, reason, model/profile, override key 이름도 함께 기록됩니다. `run_count`, `resume_count`, `rate_limit_count`, `failure_count` counters도 함께 유지됩니다.

Runner는 Codex stdout JSONL을 저장하는 동안 startup/no-progress watchdog도 함께 실행합니다. 이 watchdog은 일반적인 긴 작업 timeout이 아닙니다. 장시간 테스트나 실제 작업은 JSONL에서 의미 있는 진행 신호가 나온 뒤라면 기본적으로 종료하지 않습니다. 기본 동작은 Codex가 시작 후 아무 stdout도 쓰지 않거나, `session.started`/`thread.started`/`turn.started` 같은 startup event만 쓰고 의미 있는 event를 내지 않는 경우를 보수적으로 감지하는 것입니다. 기본값은 startup stall 약 4분, `turn.started` 이후 첫 meaningful event 약 7분이며, mid-run idle은 warning metadata만 남기고 자동 종료하지 않습니다.

Meaningful progress includes assistant/agent messages, command/tool execution start or completion, file change events, `turn.completed`, `turn.failed`, `error`, and the final JSON result. Startup/no-progress stall이 감지되면 runner는 Codex child에 `SIGTERM`을 보내고 짧은 grace period 뒤 필요할 때만 `SIGKILL`을 보냅니다. 이 class는 기본적으로 permanent failure로 처리하지 않습니다. session/thread id가 있으면 `needs_resume`, 없으면 짧은 cooldown이 붙은 `runnable` 상태로 되돌리고 `last_error`에는 `codex startup stalled before meaningful JSONL events` 같은 명확한 메시지를 기록합니다. task에는 `last_progress` metadata가 저장되고, sanitized `task_startup_stalled` event가 append-only event log에 기록됩니다.

Watchdog 관련 config key:

- `codex_startup_stall_seconds` default `240`
- `codex_first_meaningful_timeout_seconds` default `420`
- `codex_mid_run_idle_seconds` default `1800`
- `codex_mid_run_idle_kill_enabled` default `false`
- `codex_total_runtime_timeout_seconds` default `null`
- `codex_watchdog_grace_seconds` default `5`
- `codex_startup_stall_cooldown_seconds` default `60`

rate-limit evidence 확인:

```bash
PYTHONPATH=src python3 -m codex_batch_runner rate-limits
PYTHONPATH=src python3 -m codex_batch_runner rate-limits --json
```

recent event log 확인:

```bash
PYTHONPATH=src python3 -m codex_batch_runner events
PYTHONPATH=src python3 -m codex_batch_runner events --task-id task-a --limit 10
PYTHONPATH=src python3 -m codex_batch_runner events --json
```

`events`는 `.codex-batch-runner/events/YYYY-MM-DD.jsonl`에 append-only로 저장된 sanitized audit events를 최근순으로 보여줍니다. JSON 출력은 event object 배열을 반환하고, 기본 출력은 `occurred_at`, event type, task id, summary만 표시합니다. Event log는 task JSON 파일을 대체하지 않는 감사 stream입니다.

runner state 확인:

```bash
PYTHONPATH=src python3 -m codex_batch_runner state
```

global runner pause 확인 및 수동 설정:

```bash
PYTHONPATH=src python3 -m codex_batch_runner pause show
PYTHONPATH=src python3 -m codex_batch_runner pause set --reason "operator maintenance window"
PYTHONPATH=src python3 -m codex_batch_runner pause clear
```

`pause set --reason ...`은 expiry 없는 global admission pause를 설정합니다. 이 switch는 rate-limit cooldown과 별개이며 `global_cooldown_until`을 재사용하지 않습니다. Task-level `status=paused`와도 다른 개념으로, queue mutation plan이 task를 멈추는 상태와 runner 전체 admission gate를 구분합니다. Pause 동안 이미 실행 중인 Codex child는 강제로 종료하지 않지만, 이후 `run-next`는 stale `running` recovery만 수행하고 새 implementation task, auto-review Codex call, auto-fix enqueue를 시작하지 않습니다. `pause set`은 post-mutation trigger를 실행하지 않고, `pause clear`는 runnable work가 다시 생길 수 있으므로 기존 configured post-mutation trigger를 실행합니다. Pause state와 audit event에는 `active`, public-safe `reason`, `paused_at`, optional `paused_by`만 저장합니다.

global cooldown 확인 및 수동 설정:

```bash
PYTHONPATH=src python3 -m codex_batch_runner cooldown show
PYTHONPATH=src python3 -m codex_batch_runner cooldown set 7:6
PYTHONPATH=src python3 -m codex_batch_runner cooldown set "6/22 7:06"
PYTHONPATH=src python3 -m codex_batch_runner cooldown set +2h30m
PYTHONPATH=src python3 -m codex_batch_runner cooldown clear
PYTHONPATH=src python3 -m codex_batch_runner cooldown clear --reviewer-codex
```

`cooldown show`는 현재 `global_cooldown_until`, 활성 여부, 남은 시간을 표시합니다. `cooldown set VALUE`는 운영자가 알고 있는 usage/rate-limit reset 시각을 local timezone 기준으로 해석하고, 해석된 reset 시각에 60초 safety offset을 더한 값을 `global_cooldown_until`에 저장합니다. 지원 형식은 local time-only `H:M`/`HH:MM`, month/day date-time `M/D H:M` 또는 `M-D H:M`, year date-time `YYYY-MM-DD H:M`, timezone이 포함된 ISO datetime, 그리고 `+90m`, `+2h30m`, `+1d3h` 같은 상대 duration입니다. Time-only 입력은 오늘 해당 시각이 미래이면 오늘, 이미 지났으면 내일로 해석합니다. Date-time 입력이 과거이거나 reset 시각이 7일보다 멀면 오류로 종료합니다. `cooldown set`은 optional one-shot wake 설정 상태를 함께 출력하며, `cooldown clear`는 global cooldown을 지우고 즉시 실행 가능한 작업이 있을 수 있으므로 기존 configured post-mutation trigger를 계속 실행합니다. Reviewer Codex 사용량을 수동으로 reset한 경우에는 `cooldown clear --reviewer-codex`로 `reviewer_codex_cooldown_until`만 지웁니다. `last_reviewer_codex_rate_limit_at`은 최근 rate-limit 진단 이력으로 보존합니다.

beta health check:

```bash
PYTHONPATH=src python3 -m codex_batch_runner doctor
PYTHONPATH=src python3 -m codex_batch_runner doctor --json
```

`doctor`는 config/runtime path, event directory, configured Codex executable path, resolved executable path, executable availability, bounded `codex --version` output, execution profile 이름과 allowlisted override key, global cooldown, runner pause, active lock, task status counts, review/resolution/cooldown/runnable counts, 자동 검토 enable 상태와 reviewable completed task 수, startup/no-progress stall evidence를 점검합니다. Lock metadata에 현재 host의 pid가 있으면 pid와 liveness도 표시합니다. Version 확인은 configured executable에 `--version`만 붙여 짧은 timeout으로 실행하며, `codex exec`를 호출하거나 network operation을 수행한다고 가정하지 않습니다. Version command가 실패하거나 timeout되면 warning으로 보고하지만 doctor 실패로 취급하지 않습니다. configured/current project root가 git repository 안에 있으면 branch, dirty status, upstream 또는 local `origin/main` 대비 ahead/behind count도 표시합니다. git metadata는 local repository state만 읽고 network operation을 실행하지 않습니다. 다른 프로젝트에서 상세 transcript를 열기 전에 queue 상태를 낮은 비용으로 확인하는 용도입니다. error check가 있으면 non-zero로 종료하고, warning은 종료 코드를 실패로 만들지 않습니다.

오래된 완료/보관 task 정리 후보 확인:

```bash
PYTHONPATH=src python3 -m codex_batch_runner prune
PYTHONPATH=src python3 -m codex_batch_runner prune --older-than-days 60 --json
PYTHONPATH=src python3 -m codex_batch_runner prune --notifier-cursor-state .codex-batch-runner/notify-state.json --json
```

`prune`은 기본적으로 dry-run입니다. `archived` task와 `completed + review_status=accepted` task 중 지정한 age보다 오래된 항목을 task/log 후보로 보고하며, task JSON 파일과 task에 기록된 log path를 함께 표시합니다. configured `event_dir` 아래의 오래된 `*.jsonl` event log 파일은 별도의 event 후보로 보고합니다. 실제 삭제는 `--apply`를 명시한 경우에만 수행합니다. Optional notifier cursor state paths can be supplied by config or repeated `--notifier-cursor-state` flags; they are local-only state files and are not enabled by default.

queue mutation plan 검증:

```bash
PYTHONPATH=src python3 -m codex_batch_runner apply-plan queue-plan.json --dry-run
PYTHONPATH=src python3 -m codex_batch_runner apply-plan queue-plan.json
PYTHONPATH=src python3 -m codex_batch_runner apply-plan queue-plan.json --dry-run --json
PYTHONPATH=src python3 -m codex_batch_runner apply-plan queue-plan.json --apply
```

`apply-plan`은 기본적으로 read-only dry-run으로 동작합니다. `--dry-run`을 생략해도 task JSON을 쓰지 않고 Codex를 실행하지 않으며 post-mutation hook도 호출하지 않습니다. Dry-run은 plan schema, 지원 operation 이름, 대상 task 존재 여부, running task 대상 금지, operation별 `expected` stale check, dependency cycle 가능성, plan 또는 operation 단위 `reason` 존재 여부를 확인하고 human report 또는 JSON report를 출력합니다. Report는 raw prompt, log path, session/thread id, credential/token 같은 민감한 plan 값을 redaction합니다.

실제 queue 변경은 `--apply`를 명시한 경우에만 수행됩니다. Apply mode는 runner와 같은 queue lock을 잡은 뒤 같은 validation을 다시 실행하고, 검증이 통과한 경우에만 제한된 field(`title`, `description`, `category`, `labels`, `depends_on`, `status`, `execution_profile`, `routing_reason`, `routing_risk_factors`, `routing_experiment`, `routing_size`, `routing_risk`, `verification_scope`)를 atomic JSON write로 갱신합니다. `running` task 대상 mutation과 `status=running` 전환은 거부하고, `execution_profile`이 제공되면 config의 `execution_profiles`에 정의된 이름인지 검증하며, `routing_size`/`routing_risk`는 allowlisted enum 값만 허용합니다. 적용된 변경은 sanitized `task_mutated` event로 기록하고, 변경이 있었을 때 configured `post_mutation_trigger_command`를 실행합니다. Profile mutation이 자주 필요해지면 별도 convenience wrapper command를 추가할 수 있지만, 현재 canonical safe mutation surface는 계속 `apply-plan`입니다.

task별 git worktree 준비와 정리는 명시적 명령으로도 수행할 수 있습니다. `worktree_mode=task`에서는 `run-next`가 실행 가능한 task를 처리하기 직전에 같은 prepare/recovery guard를 적용하고, 통과한 경우 task worktree를 Codex 실행 `cwd`로 사용합니다. `worktree_mode=disabled`는 기존처럼 task의 원래 `cwd`에서 실행합니다.

```bash
PYTHONPATH=src python3 -m codex_batch_runner worktree prepare task-a --dry-run
PYTHONPATH=src python3 -m codex_batch_runner worktree prepare task-a --apply
PYTHONPATH=src python3 -m codex_batch_runner worktree apply task-a --dry-run
PYTHONPATH=src python3 -m codex_batch_runner worktree apply task-a --apply
PYTHONPATH=src python3 -m codex_batch_runner worktree cleanup task-a --dry-run
PYTHONPATH=src python3 -m codex_batch_runner worktree cleanup task-a --apply
```

`worktree prepare --apply`는 `worktree_mode=task`일 때만 task branch와 worktree를 만들거나 기존 연결 상태를 재확인하고, runner와 같은 queue lock 아래에서 task metadata를 갱신합니다. 기본 branch 이름은 `cbr/<task-id>`를 Git ref 규칙에 맞게 sanitize한 값입니다. `run-next`도 같은 규칙으로 worktree를 만들거나 재사용하며, prepare/recovery check가 실패하면 Codex를 호출하지 않고 task를 `failed`로 표시해 운영자 점검을 요구합니다. `needs_resume` task는 기존 retained worktree metadata가 유효할 때만 같은 worktree에서 resume하며, metadata가 없거나 stale이면 새 worktree를 조용히 만들지 않습니다. Worktree-backed task가 `completed`를 반환했고 task worktree에 변경이 남아 있으면 runner는 final JSON의 `changed_files`에 보고된 안전한 상대 경로만 stage하여 task branch에 local commit을 만듭니다. 이 commit은 review unit으로 쓰기 위한 것이며, remote push 또는 main 반영은 별도 명시 작업입니다.

`worktree apply TASK_ID --dry-run`은 dry-run report로, `completed + accepted` worktree task branch를 main worktree에 fast-forward로 반영할 수 있는지 확인합니다. Report에는 branch, `execution_base_head`, branch head, main head, 적용 대상, apply strategy, commit range summary, gate 결과, errors, warnings가 포함됩니다. Main HEAD가 정확히 `execution_base_head`와 같으면 `--apply`는 main worktree에서 `git merge --ff-only <execution_branch>`만 실행하고, 성공하면 task에 apply metadata를 기록한 뒤 sanitized `task_worktree_applied` event를 남깁니다.

Main HEAD가 `execution_base_head` 이후로 forward-only 이동한 stale-base 상태이면 `worktree apply`는 더 보수적인 rebase/re-review path를 사용합니다. Main worktree와 task worktree가 모두 clean이고 task branch가 `execution_base_head` 위에 있으며 detached temporary worktree preflight에서 clean rebase가 확인되면, `--dry-run`은 stale-base rebase 계획을 보고합니다. `--apply`는 task branch/worktree에서만 `git rebase <current-main-head>`를 실행하고 task metadata의 base/head rebase fields를 갱신하며 `review_status=unreviewed`로 되돌립니다. 같은 command에서 main fast-forward apply를 이어서 수행하지 않습니다. 운영자는 re-review 후 다시 `accept`하거나 post-accept apply path가 다시 실행되게 해야 합니다. Rebase conflict는 branch/worktree를 abort로 복구한 뒤 `execution_rebase_status=blocked`를 기록하고, parent/root chain에 연결된 bounded `worktree_conflict_fix` subtask를 최대 한 개 enqueue합니다. 이 subtask는 `depends_on`으로 parent를 self-block하지 않고 자기 worktree에서 parent branch 변경을 current main 위로 port한 뒤 일반 review/apply chain을 거칩니다. Guard failure는 main과 task branch를 변경하지 않고 명확한 report error로 남깁니다. Merge commit, in-command conflict marker editing, cherry-pick, remote push는 수행하지 않습니다.

`worktree cleanup --dry-run`은 기본 read-only report이며, `worktree cleanup --apply`는 두 가지 보수적 cleanup 경로만 허용합니다. 첫째, `execution_apply_status=applied` metadata가 있는 `completed + accepted` 또는 `archived` worktree task는 기존처럼 applied cleanup 후보입니다. 둘째, result를 적용하지 않기로 명시 결정한 retained worktree task는 discard cleanup 후보가 될 수 있습니다. Discard cleanup은 `completed`/`archived` task의 `review_status=rejected`이거나 terminal discard resolution이 `superseded`, `wont_fix`, `duplicate`, `manual` 중 하나일 때만 허용합니다. Resolution-based discard cleanup은 terminal task status(`failed`, `blocked_user`, `completed`, `archived`)에서만 허용합니다. Archived `needs_followup` worktree task도 explicit rejected/resolution signal이 없으면 계속 cleanup 대상이 아닙니다. `smoke` resolution은 적용 포기 의미가 명확하지 않아 discard cleanup allowlist에 포함하지 않습니다.

Cleanup은 configured `worktree_root` 아래에 있고 Git worktree registry와 task metadata가 일치하는 worktree만 제거합니다. Local branch, task JSON, runtime log, event log, private state는 삭제하지 않습니다. Discard cleanup도 branch를 보존하고 task metadata에 `execution_cleanup_kind=discard`, cleanup reason, branch-retained/result-not-applied metadata를 기록합니다. Missing/stale metadata, `recovery_required` metadata, 또는 path/registry 상태 불일치는 명확한 거부 사유로 보고하고 자동 정리하지 않습니다.

## 설정

config 탐색 순서는 다음과 같습니다.

1. `--config path/to/config.json`
2. `CBR_CONFIG` 환경 변수
3. `~/.config/codex-batch-runner/config.json`
4. 현재 작업 디렉터리 기준 기본값

config가 없을 때의 기본 runtime 디렉터리는 현재 작업 디렉터리의 `.codex-batch-runner/`입니다. 이 디렉터리는 gitignore 대상입니다.

Optional `event_dir` can override the append-only event log directory. If omitted, it defaults to `.codex-batch-runner/events` under the active runtime root.

Optional `notifier_cursor_state_paths` can point to local notifier cursor state JSON files used only by `prune` event-log safety checks. It is disabled by default. The generic cursor schema is intentionally small:

```json
{
  "schema_version": 1,
  "current_event_file": ".codex-batch-runner/events/2000-01-02.jsonl",
  "current_byte_offset": 1234
}
```

`last_processed_event_file` may be used instead of `current_event_file` when a notifier records only whole-file progress. Cursor event file paths may be absolute or relative to `event_dir`, but they must resolve inside configured `event_dir`. If a configured cursor state file is missing, malformed, unreadable, or references an event file outside `event_dir`, `prune` reports a warning and skips old event JSONL deletion rather than failing the whole command.

Optional git worktree mode is disabled by default. When enabled with `worktree_mode=task`, `run-next` prepares or reuses a task-specific worktree and runs Codex with that worktree as the process `cwd`; task metadata still preserves the original task `cwd` separately from `execution_worktree_path`. 이 목적은 completed-but-unreviewed 결과와 독립적인 후속 작업이 main worktree를 더럽히거나 서로 다른 task state를 섞지 않고 공존할 수 있게 하는 것입니다. Worktree-backed task는 completed/accepted 상태만으로 dependency-ready가 되지 않고 accepted result가 integration target에 applied된 뒤 ready가 됩니다. Completed worktree task의 보고된 변경은 task branch local commit으로 고정되어 review-bundle/review-next가 원자적인 branch diff를 검토할 수 있습니다.

Config may set `root` to make relative runtime paths independent of the process current working directory. When `root` is set, relative `queue_dir`, `log_dir`, `event_dir`, `lock_file`, `state_file`, `worktree_root`, and notifier cursor state paths are resolved under that root. Without `root`, cbr keeps the built-in fallback behavior of resolving relative paths from the current working directory.

```json
{
  "root": "/path/to/codex-batch-runner",
  "worktree_mode": "task",
  "worktree_root": ".codex-batch-runner/worktrees"
}
```

`worktree_root` may be relative to the active runner root. Public docs and examples should keep this path generic and local-only.

Optional execution profiles are disabled by default. `default_execution_profile` applies to implementation tasks that do not specify `execution_profile`; `review_execution_profile` applies to reviewer Codex calls. A task-level `execution_profile` selects a named profile, and task-level `model`, `codex_profile`, or `codex_config_overrides` override values from that profile. If `default_execution_profile` is set and a task category or label is one of `runner`, `runner-state`, `lock`, `resume`, `reviewer-codex`, `reviewer-safety`, `queue-mutation`, `worktree-critical`, `worktree-apply`, `worktree-recovery`, `stale-base`, or `rebase`, cbr uses a configured `deep` profile by fallback when it exists. General routing labels such as `worktree` or `docs` do not trigger `deep` by themselves. Each Codex execution records the resolved profile and fallback reason in `last_run`.

Enqueue can also record public-safe routing decision metadata without changing execution policy:

```bash
PYTHONPATH=src python3 -m codex_batch_runner enqueue \
  --cwd /path/to/repo \
  --profile small \
  --routing-reason "docs-only bounded change" \
  --routing-risk-factor public-docs \
  --routing-risk-factor low-blast-radius \
  --routing-experiment downshift_probe \
  --routing-size small \
  --routing-risk low \
  --verification-scope unit \
  --verification-scope docs \
  --prompt-file task.md
```

`routing_experiment` is an audit label, not an enforced policy. Common labels are `baseline`, `downshift_probe`, `upshift_guard`, and `manual`. `routing_size` is an allowlisted pre-enqueue size estimate (`tiny`, `small`, `medium`, `large`, `xlarge`), `routing_risk` is an allowlisted implementation risk estimate (`low`, `medium`, `high`), and `verification_scope` is a repeatable allowlisted tag (`none`, `docs`, `lint`, `typecheck`, `unit`, `integration`, `e2e`, `smoke`, `manual`, `build`) describing expected verification coverage.

`routing-report` is a read-only profile routing report for tuning profile selection over time. It groups recent tasks by profile, category, label, profile/category pair, routing experiment, routing size, routing risk, routing risk factor, verification scope, routing decision tuple (`routing_size` + `routing_risk` + `verification_scope`), profile/routing decision tuple, and profile/experiment pair, then reports accepted counts, first-pass accepted counts, needs-fix/rejected rates, auto-fix task frequency, attempts, duration, and a simple cost proxy based on attempts/runs/duration. It supports the same project/category/label narrowing style as list:

```bash
PYTHONPATH=src python3 -m codex_batch_runner routing-report --project codex-batch-runner
PYTHONPATH=src python3 -m codex_batch_runner routing-report --project codex-batch-runner --limit 100 --json
```

Routing policy changes are operator decisions, not automatic report side effects. The conservative downshift/upshift criteria are maintained in [docs/spec.md](docs/spec.md).

```json
{
  "default_execution_profile": "normal",
  "review_execution_profile": "review",
  "execution_profiles": {
    "small": {
      "model": "gpt-5-small",
      "codex_profile": "batch-small",
      "config_overrides": {
        "model_reasoning_effort": "low"
      },
      "token_budget_hint": "small documentation or test-only task"
    },
    "normal": {
      "model": "gpt-5",
      "codex_profile": "batch-normal"
    },
    "deep": {
      "model": "gpt-5",
      "codex_profile": "batch-deep"
    },
    "review": {
      "model": "gpt-5",
      "codex_profile": "batch-review"
    }
  }
}
```

Profile options are inserted into both `codex_command` and `codex_resume_command` after `codex exec`, preserving `resume {session_id}` ordering. If no execution profile or task override is configured, command construction is unchanged.

Capacity config controls implementation task admission while preserving the current single-runner default. The default contract is equivalent to:

```json
{
  "max_total_running": 1,
  "max_running_per_project": 1,
  "capacity_pools": {
    "codex": {
      "max_running": 1
    }
  }
}
```

`max_total_running` limits all concurrent implementation task executions, `max_running_per_project` limits executions for the same project, and `capacity_pools` names scarce execution resources such as `codex`. Each task has `capacity_pool` metadata, defaulting to `codex`; `cbr enqueue --capacity-pool POOL` can set another configured pool. `run-next` now claims one admissible implementation task under the queue lock, releases the lock while Codex or shell execution runs, and reacquires it only to finalize the matching `active_run_id`. A task whose dependencies, cooldown, project capacity, total capacity, or pool capacity are not ready is skipped without changing its task status.

Priority scheduling is deterministic. Each task has `task_priority`, defaulting to `normal`; `cbr enqueue --priority asap|high|normal|low|background` can override it. Config can set `project_priorities` by project id or normalized project root, `default_project_priority` (default `100`), and `project_priority_aging_hours` (default `24`; `0` means strict project tiers). Selection filters by runnable status, dependencies, cooldown, and capacity, then orders by effective project priority, raw project priority, task priority, `created_at`, and task id. Lower project priority numbers run first; aging gradually improves older ready projects so lower-priority projects do not starve.

Actual parallel execution still requires multiple scheduler workers, overlapping `run-next` invocations, or an external service repeatedly invoking `run-next`; cbr does not start an in-process dispatcher. Worktree isolation is the state isolation layer for concurrent project work. With the default capacity values, behavior remains effectively one implementation task at a time.

Optional `post_mutation_trigger_command` can run a generic scheduler wake-up hook after successful queue mutations and after `run-next` finishes one task when another task is eligible to run. The value is an argv list, not a shell string, so it is not shell-expanded. It is disabled by default.

```json
{
  "post_mutation_trigger_command": ["systemctl", "--user", "start", "codex-batch-runner.service"]
}
```

The hook runs after durable task state writes for commands such as `enqueue`, `accept`, `reject`, `resolve`, `archive`, and successful `apply-plan --apply` mutations. `run-next` may also run it after releasing the runner lock, but only after processing one implementation task, accepting one completed task whose result is actually available, or enqueueing one bounded auto-fix/conflict-fix task, and only when there is eligible follow-up work and no active global cooldown or runner pause. After an implementation task finishes, eligible follow-up work includes either another runnable implementation task or an immediately actionable auto-review candidate; paused work, dependency-blocked-only queues, cooldown-only queues, mutation-free auto-review skips, and accepted-but-unapplied worktree results with no new actionable follow-up still do not qualify. Hook failures print a warning to stderr but do not fail the core operation. Polling remains the fallback, and duplicate wake-ups are safe because `run-next` still enforces the runner lock, cooldown checks, empty-queue behavior, dependency checks, and single-task execution.

수동 cooldown deadline에는 optional one-shot wake도 설정할 수 있습니다. 기본값은 disabled이며, polling scheduler가 항상 fallback입니다. 이 hook은 Codex를 직접 실행하지 않고, cooldown이 끝난 뒤 기존 scheduler service 또는 `run-next` entrypoint를 깨우는 용도로만 사용해야 합니다.

```json
{
  "manual_cooldown_wake_scheduler": "macos_launchd",
  "manual_cooldown_wake_command": ["launchctl", "start", "com.example.codex-batch-runner"]
}
```

현재 scheduler adapter는 `disabled`와 `macos_launchd`를 지원합니다. `macos_launchd`는 `cooldown set` 시점에 `launchctl submit`으로 launchd가 관리하는 one-shot job을 등록하고, job 안에서 `effective_cooldown_until`까지 sleep한 뒤 configured wake command를 실행합니다. `launchctl kickstart -k`는 사용하지 않습니다. Wake command는 정상 runner entrypoint를 실행하게 해야 하며 `codex` executable을 직접 지정하면 warning과 event를 남기고 schedule하지 않습니다. Schedule 성공, skip, 실패는 각각 `cooldown_wake_scheduled`, `cooldown_wake_skipped`, `cooldown_wake_failed` event로 기록됩니다. Schedule 실패는 warning으로만 처리되며 `global_cooldown_until` 저장은 유지됩니다.

설정 파일 예시는 [examples/config.example.json](examples/config.example.json)에 있습니다. 이 예시는 `--sandbox workspace-write`를 사용하는 안전한 기본값입니다.

```bash
PYTHONPATH=src python3 -m codex_batch_runner --config examples/config.example.json run-next
```

완전 비대화형 자동화가 필요하고 운영자가 로컬 전체 접근 위험을 명시적으로 수용한 환경에서는 [examples/config.automation.example.json](examples/config.automation.example.json)을 참고할 수 있습니다. 이 예시는 Codex CLI에 `--dangerously-bypass-approvals-and-sandbox`를 전달하므로 approval prompt와 sandbox를 모두 비활성화합니다. 즉, 배치 작업이 해당 사용자 권한으로 접근 가능한 로컬 파일과 명령을 제한 없이 사용할 수 있습니다.

```bash
PYTHONPATH=src python3 -m codex_batch_runner --config examples/config.automation.example.json run-next
```

Automation mode는 approval prompt에서 멈추는 작업이나 sandbox 권한 부족으로 반복 실패하는 작업을 줄여 pending queue와 오래 유지되는 lock 정체를 완화할 수 있습니다. 대신 실행 후에는 `summary`, 필요한 경우 `transcript`, 대상 repository의 검증 명령, `doctor`를 신중하게 사용해 결과와 queue 상태를 확인해야 합니다. `accept`는 변경 내용과 검증 결과를 운영자가 확인한 뒤에만 기록합니다.

## macOS launchd 예시

macOS에서는 cron보다 launchd 운영을 기본으로 권장합니다. `StartInterval`은 평상시 실행 주기이고, rate-limit 이후의 긴 대기는 runner 내부 global cooldown으로 처리합니다.

예시 plist는 [examples/com.example.codex-batch-runner.plist](examples/com.example.codex-batch-runner.plist)에 있습니다.

설치 예:

```bash
cp examples/com.example.codex-batch-runner.plist ~/Library/LaunchAgents/com.example.codex-batch-runner.plist
launchctl load ~/Library/LaunchAgents/com.example.codex-batch-runner.plist
```

사용 전 plist의 `ProgramArguments`, `WorkingDirectory`, config 경로는 로컬 환경에 맞게 수정해야 합니다. 개인 수정본은 `*.local.plist` 이름으로 두면 gitignore됩니다.

Queue mutation 직후 또는 task 처리 후 eligible follow-up work가 있을 때 launchd job을 깨우려면 config에 generic hook을 추가할 수 있습니다. label은 로컬 환경에 맞게 바꿉니다. Active runner를 kill하지 않도록 `launchctl`의 force-kill option인 `-k`는 사용하지 않습니다. 해당 option은 task가 `status=running`으로 기록된 뒤 final result 처리 전에 active runner를 종료시켜 running task 또는 lock state를 남길 수 있습니다.

```json
{
  "post_mutation_trigger_command": ["launchctl", "kickstart", "gui/UID/com.example.codex-batch-runner"]
}
```

이 hook은 latency를 줄이기 위한 보조 수단입니다. launchd `StartInterval` polling은 fallback으로 유지하는 것을 권장합니다. Duplicate non-killing wake-up은 안전합니다. `run-next`가 runner lock, cooldown, empty queue, dependency, single-task execution 규칙을 계속 강제하기 때문입니다.

## Linux systemd user service 예시

Linux user service로 운영하는 경우 같은 hook으로 systemd service를 즉시 시작할 수 있습니다.

```json
{
  "post_mutation_trigger_command": ["systemctl", "--user", "start", "codex-batch-runner.service"]
}
```

이 hook은 latency를 줄이기 위한 보조 수단입니다. systemd timer나 cron polling은 fallback으로 유지하는 것을 권장합니다.

launchd는 사용자 shell의 `PATH`를 그대로 상속하지 않습니다. `codex`가 `/Users/you/.local/bin/codex`처럼 기본 launchd `PATH` 밖에 있으면 config의 `codex_command`와 `codex_resume_command`에는 `codex` 대신 절대 경로를 사용하는 것이 안전합니다.

## cron fallback

cron을 써야 하는 환경에서는 아래처럼 실행할 수 있습니다.

```cron
*/10 * * * * cd /path/to/codex-batch-runner && /path/to/venv/bin/cbr run-next >> .codex-batch-runner/runner.log 2>&1
```

## 안전 모델

`run-next`는 한 번 실행될 때 task 하나만 처리합니다.

## Shell task backend

Most tasks use the default `codex` backend and invoke Codex. Simple local checks can be enqueued with the `shell` backend to run an argv-list command without consuming Codex tokens:

```bash
cbr enqueue --cwd /path/to/repo --backend shell --command-json '["pytest", "tests/test_queue.py"]'
cbr enqueue --cwd /path/to/repo --backend shell --shell-timeout 300 --command python -m pytest tests/test_queue.py
```

`--command` must be the final cbr option because every following token becomes command argv. cbr does not implicitly evaluate shell strings. To use shell features such as pipes, redirects, or `&&`, make that explicit in argv, for example `--command bash -lc 'pytest && cbr doctor'`.

Shell tasks participate in the normal queue lifecycle: dependency ordering, runner lock, stale running recovery, attempts/run counters, task logs, terminal events, post-run wake triggers, and dependent unblocking all use the same task state model. Exit code `0` marks the task `completed` with `review_status=unreviewed`; nonzero exit, missing executable, or timeout marks it `failed`. A failed shell dependency remains a normal unmet dependency for downstream tasks.

Full stdout and stderr are written to the task attempt log under `log_dir`. Task JSON and event payloads keep compact metadata only: command argv, return code, duration, timeout flag, output byte counts, log path, and a short result summary. Do not put secrets in shell argv; command metadata is stored in the task JSON.

`shell_task_timeout_seconds` configures the default timeout for shell tasks and defaults to `900`. A task-specific `--shell-timeout` overrides it.

Future maintenance workflows such as Codex CLI update checks can use shell tasks as ordered dependency gates. A future `exclusive` or `maintenance_pause` mode should pause new admissions before running a solo maintenance task, clear the pause on success, and keep the pause on failure. That solo mode is not implemented by the current shell backend.

Codex를 호출하지 않는 조건:

- queue가 비어 있습니다.
- 다른 runner가 lock을 보유 중입니다.
- global cooldown 중입니다.
- 모든 task가 dependency blocked 상태입니다.
- 모든 runnable task가 task cooldown 중입니다.

동시 실행 방지는 `.codex-batch-runner/runner.lock` atomic create로 처리합니다. lock metadata에 같은 host의 dead pid가 기록되어 있으면 즉시 복구하고, host나 pid를 확인할 수 없으면 age 기반 stale lock 기준으로 복구합니다. 기본 stale 기준은 6시간입니다.

task와 state 파일은 같은 디렉터리에 임시 파일을 쓴 뒤 `os.replace`로 교체합니다. Codex JSONL 로그는 attempt별 파일로 저장합니다.

Core state-changing commands also append sanitized audit events. Initial event types include `task_created`, `task_started`, `task_completed`, `task_failed`, `task_needs_resume`, `task_blocked_user`, `task_reviewed`, `task_resolved`, `task_archived`, `task_startup_stalled`, `task_worktree_prepared`, `task_worktree_committed`, `task_worktree_applied`, `task_worktree_cleaned`, `cooldown_updated`, and `rate_limit_detected`. Event payloads are intentionally small and redact prompt text, raw transcripts, session/thread ids, secrets, credentials, and token-like fields. Event write failures are warnings; queue operations continue to rely on canonical task JSON files. In `worktree_mode=task`, `task_started`/terminal task events may include sanitized worktree execution metadata.

`prune`은 삭제 동작이 있는 명령이므로 기본값이 비파괴 dry-run입니다. `--apply`가 없으면 파일을 삭제하지 않습니다. `--apply`가 있어도 resolved path가 configured `queue_dir`, `log_dir`, 또는 `event_dir` 밖에 있는 파일은 삭제하지 않으며, report에 blocked 항목으로 남깁니다. Event pruning only considers `*.jsonl` files under `event_dir`; notifier cursor/state files and other non-JSONL files are skipped. When notifier cursor state is configured, old event files that may not be fully consumed are reported with a skipped reason instead of being deleted.

## Codex CLI maintenance

`codex-batch-runner` does not automatically update the Codex CLI. CLI updates can change JSONL event shape, resume behavior, permission and sandbox handling, or final response behavior. A bad update can also waste usage-limit tokens before the operator notices, and rollback may be unclear when the installed CLI came from a standalone package, an app bundle, or another local installation method.

Recommended maintenance policy:

- Check or update the Codex CLI only while the queue is idle.
- Treat the queue as idle only when there is no runner lock, no active global cooldown, and no `runnable`, `needs_resume`, or `running` task.
- Record `cbr doctor --json` output before and after a manual update so the configured path, resolved path, and `codex --version` output are visible.
- After a manual update, run `cbr doctor` and the focused test or smoke command relevant to the runner deployment before allowing queued work to continue.
- Do not compare against an app-bundled Codex binary by default. If a system has a bundled CLI inside an installed Codex application, inspect it only as an optional manual investigation.
- Do not hash large binaries during routine checks. A hash could be added later as an explicit verbose/deep diagnostic, not as default doctor behavior.

## Rate-limit 처리

runner는 Codex usage remaining을 조회할 수 있다고 가정하지 않습니다.

Codex 출력이나 stderr에서 rate-limit/usage-limit로 보이는 실패를 감지하면:

- task를 실패 처리하지 않습니다.
- resume id가 있으면 task 상태를 `needs_resume`으로 돌려 cooldown 이후 이전 Codex thread를 resume합니다.
- resume id가 없으면 task 상태를 `runnable`로 돌려 기존처럼 새 실행으로 재시도합니다.
- `needs_resume` task에 `next_prompt`가 있지만 resume id가 없으면 runner는 신규 실행으로 이어가며 `resume_unavailable` metadata를 남깁니다.
- task `cooldown_until`을 설정합니다.
- global cooldown을 설정합니다.
- cooldown 전까지 Codex를 호출하지 않습니다.
- `.codex-batch-runner/rate-limits/` 아래에 sanitized evidence JSON을 저장합니다.

초기 기본값은 launchd 10분 주기, rate-limit 이후 30분 cooldown입니다.

rate-limit evidence에는 task id, detected time, attempt, matched markers, cooldown deadline, 짧은 stderr/error excerpt, 원본 log path만 저장합니다. prompt, 전체 JSONL, session/thread id, secrets는 저장하지 않습니다.

Codex가 표시한 reset 시각을 운영자가 확인한 경우에는 `cbr cooldown set VALUE`로 global cooldown을 더 정확히 맞출 수 있습니다. 이 명령은 reset 시각을 local timezone 기준으로 해석하고 60초 뒤를 실제 cooldown 만료 시각으로 저장하므로, reset 직전의 불필요한 재시도를 줄이면서 reset 이후에는 낮은 latency로 재개할 수 있습니다. 설정값은 최대 7일 이내만 허용됩니다.

## Codex 최종 응답 계약

runner는 task prompt를 wrapper로 감싸 Codex에 넘깁니다. Codex는 마지막에 JSON object만 반환해야 합니다.

```json
{
  "task_id": "string",
  "status": "completed | needs_resume | blocked_user | failed",
  "summary": "string",
  "next_prompt": "string",
  "changed_files": ["string"],
  "verification": ["string"],
  "commits": ["string, optional"],
  "push_status": "string or object, optional"
}
```

`commits`와 `push_status`는 선택 필드입니다. 기존 응답처럼 이 필드를 생략해도 정상 처리됩니다. 포함된 경우 runner는 값을 변환하지 않고 `last_result`에 저장하며, `summary`와 `list --verbose`에서 짧게 표시합니다.

`needs_resume`은 사용량 부족 전용 상태가 아닙니다. Codex가 작업을 일부 진행했고 후속 실행이 필요하다고 판단할 때 반환하는 상태입니다. rate-limit은 runner가 실패 로그에서 별도로 감지합니다.

## 검토 모델

배치 작업의 `status=completed`는 Codex 실행이 완료되었다는 뜻입니다. 실제 작업 품질이 확인되었다는 뜻은 아닙니다.

검토 상태는 `review_status` 필드로 별도 관리합니다.

- `unreviewed`: Codex 실행은 완료되었지만 아직 검토되지 않았습니다.
- `accepted`: 운영자가 결과를 검토하고 완료로 인정했습니다.
- `rejected`: 결과를 완료로 인정하지 않았습니다.
- `needs_followup`: 후속 작업이 필요합니다.

관련 프로젝트에서 배치 결과를 점검할 때는 먼저 `summary`로 `last_result.summary`, changed files, verification, commit/push metadata, last_error를 확인합니다. task 실행 후 runner는 네트워크를 사용하지 않는 로컬 Git inspection으로 branch, upstream 비교 기준, ahead/behind, unpushed commit 요약을 `git_status`에 저장할 수 있습니다. 실제 push는 자동화하지 않습니다. 더 자세한 실행 대화가 필요하면 `transcript`와 `logs`, 필요한 테스트 명령을 함께 확인한 뒤 `accept` 또는 `reject`를 사용합니다.

Dependency readiness policy는 config의 `dependency_requires_accepted_review`로 제어합니다. 기본값은 `false`이며, 기존 자동화와 호환되도록 dependency가 `status=completed`이면 ready로 봅니다. 이 기본값에서는 `review_status`가 아직 `unreviewed` 또는 `awaiting_review`로 표시되는 completed-but-unreviewed dependency도 후속 작업을 막지 않습니다. 이 선택은 batch 운영에서 review backlog 때문에 독립적인 후속 작업의 latency가 늘어나는 것을 피하고 처리량을 유지하기 위한 정책입니다. `true`로 설정하면 dependency는 `status=completed`와 `review_status=accepted`를 모두 만족해야 ready입니다. 이 모드는 dependent work의 안전성을 높이지만, 검토가 밀리면 더 많은 task가 dependency blocked 상태로 남을 수 있습니다. 전환 전에 오래된 completed task를 `accept` 또는 `reject --follow-up`으로 정리하지 않으면 후속 runnable task가 dependency blocked 상태로 남을 수 있습니다. Optional worktree isolation roadmap은 이 기본 정책과 함께 completed-but-unreviewed 결과와 독립적인 후속 작업이 main worktree를 더럽히거나 서로 다른 task state를 섞지 않고 공존하도록 만드는 방향입니다.

자동 검토는 runner와 같은 queue lock을 사용하는 순차 phase로만 동작합니다. 기본값은 report-only이고, `"auto_review_mechanical_accept": true` 또는 `review-next --apply --mechanical-auto-accept`처럼 명시적으로 허용한 경우에만 local mechanical gate를 모두 통과한 task를 자동 accept합니다. Reviewer Codex는 `"auto_review_codex_enabled": true` 또는 `review-next --apply --reviewer-codex`와 `auto_review_codex_max_calls_per_run >= 1`이 함께 충족될 때만 호출됩니다. Gate가 실패하거나 상태가 stale이면 human fallback으로 남기며, reviewer Codex는 기본적으로 비활성화되어 token을 소비하지 않습니다. Commit ancestry gate는 보고된 commit이 현재 `HEAD`와 같거나 현재 `HEAD`의 ancestor이면 통과하고, 현재 `HEAD`에서 도달할 수 없으면 human check 대상으로 실패합니다. 자동 fix task 생성도 기본값은 disabled이며 `auto_review_codex_max_fix_loops_per_task`를 1 이상으로 설정하고 reviewer가 high-confidence low-risk `needs_fix`와 bounded prompt를 반환한 경우에만 수행됩니다. 생성된 fix task는 외부 dependency가 아니라 blocking subtask metadata로 parent/root chain에 연결되므로 `dependency_requires_accepted_review=true`에서도 runnable 상태를 유지합니다. 일반 `depends_on` child task는 기존대로 unaccepted dependency에 blocked 됩니다. 반복 fingerprint, loop 한도 초과, prompt 누락, stale state, high-risk blocker는 자동 enqueue 없이 human-visible pending state와 sanitized event로 남깁니다. 자세한 설계는 [docs/spec.md](docs/spec.md)의 automatic review bundle section과 bounded automatic review-fix loop section을 기준으로 합니다.

spec 변경 후 이미 등록된 작업을 안전하게 재계획하는 queue mutation control plane도 [docs/spec.md](docs/spec.md)에 설계 기준을 둡니다. 기본은 plan patch dry-run validation이며, 실제 적용은 `apply-plan --apply`를 명시한 경우에만 제한된 metadata/status/dependency 변경으로 수행합니다.

여러 프로젝트가 하나의 중앙 queue를 공유하는 운영을 위해 project metadata와 review 대상 필터를 제공합니다. 관련 프로젝트에서는 `list --project-root /path/to/repo --needs-review`처럼 먼저 자기 작업만 좁힌 뒤, 필요한 task에 대해서만 `show`나 `transcript`를 읽는 흐름을 권장합니다. 세부 설계는 [docs/spec.md](docs/spec.md)의 project routing metadata와 operational triage plan을 기준으로 관리합니다.

## 로컬 작업 메모

공개 repo에 올리지 않을 작업 메모는 `ROADMAP.local.md`처럼 `*.local.md` 파일로 관리합니다. 템플릿은 [examples/ROADMAP.local.example.md](examples/ROADMAP.local.example.md)에 있습니다.
