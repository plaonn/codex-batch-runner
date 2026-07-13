# Events, Index, and Retention Contract

이 문서는 append-only event log, local SQLite read index, prune/retention contract를 정의합니다. Task JSON 파일과 event JSONL 파일은 계속 source of truth입니다.

## Local SQLite read index

SQLite index는 canonical queue migration이 아니라 retained task JSON과 retained event JSONL에서 다시 만들 수 있는 local-only read index/cache입니다. Task JSON 파일과 append-only event JSONL 파일이 계속 source of truth입니다. SQLite DB가 없거나 손상됐거나 schema version이 맞지 않아도 enqueue, run-next, accept, reject, resolve, archive, prune, worktree apply 같은 core mutation command는 SQLite를 요구하지 않아야 하며, read command도 JSON/JSONL fallback을 유지해야 합니다.

DB path는 active runtime root의 `.codex-batch-runner/index.sqlite3`를 기본으로 하며, queue directory가 config로 override된 경우 configured queue directory 옆 local `index.sqlite3`를 사용할 수 있습니다. DB는 git에 포함하지 않는 runtime artifact입니다.

Initial read projection table은 아래 범위를 포함합니다.

- `index_metadata`: schema version, last rebuild timestamp, source/indexed counts.
- `tasks`: task snapshot의 sanitized scalar projection. Index schema v2는 execution evidence contract,
  automatic/override selection cohort, selected/command/provider-reported model, exact reasoning,
  integrity status를 별도 column으로 저장하며 prompt/provider raw output은 저장하지 않습니다.
- `events`: sanitized event projection.
- `task_dependencies`: `depends_on` edges.
- `task_review_state`: review/resolution/chain state projection.
- `task_git_metadata`: branch/head/apply/cleanup status처럼 path와 raw log를 제외한 git metadata projection.

Index에는 prompt, next prompt, transcript, raw Codex JSONL, session id, thread id, stdout, stderr, credentials, environment values, secrets를 저장하지 않습니다. Payload-like values는 event sanitization 기준을 다시 적용합니다. Local task cwd, project root, task source file, event source directory/file, worktree/log path처럼 index query에 필요하지 않은 runtime-private 값도 projection에서 제외합니다.

`cbr index status`는 DB path, expected/found schema version, retained task/event source file counts, indexed task/event counts, last rebuild time, missing/corrupt/schema mismatch/staleness warning을 보고합니다. 이 command는 DB를 고치거나 생성하지 않습니다.

`cbr index rebuild --dry-run`은 retained task/event files를 읽어 rebuild plan과 count를 보고하지만 SQLite 파일을 쓰지 않습니다. `cbr index rebuild --apply`는 retained task JSON과 retained event JSONL에서 deterministic하게 새 SQLite DB를 만든 뒤 atomic replace로 반영합니다. Pruned task/event file은 source of truth에서 사라졌으므로 다음 apply rebuild 뒤 index에서도 사라집니다.


## Event log and derived SQLite index model

dashboard, Telegram notification, automatic review, queue mutation 기능이 커지기 전에 runtime record의 계층을 명확히 둡니다. 지금 단계에서 모든 queue 상태를 full SQL canonical storage로 옮기는 것은 권장하지 않습니다. 초기 source of truth는 계속 task JSON 파일입니다. task JSON 파일은 사람이 읽고 복구할 수 있는 canonical queue state이며, Codex attempt output은 attempt별 JSONL 로그로 보존합니다.

다음 durable audit layer는 append-only event log입니다. event log는 task JSON의 최신 상태를 대체하지 않고, 언제 어떤 상태 변화와 운영 결정이 발생했는지 재구성하기 위한 감사 stream입니다. 현재 minimal implementation은 `event_dir` config 값이 있으면 그 경로를 사용하고, 없으면 runtime directory 아래 date-partitioned JSONL 파일로 저장합니다.

```text
.codex-batch-runner/events/YYYY-MM-DD.jsonl
```

대표 event type:

- `task_created`: task가 queue에 등록됨
- `task_started`: runner가 task 실행을 시작함
- `task_completed`: Codex final JSON이 `completed`를 반환함
- `task_failed`: task가 실패 상태로 전환됨
- `task_needs_resume`: Codex final JSON이 `needs_resume`을 반환함
- `task_blocked_user`: Codex final JSON이 `blocked_user`를 반환함
- `task_reviewed`: 운영자 또는 review workflow가 검토 상태를 기록함
- `task_resolved`: failed/blocked task 또는 completed rejected/needs_followup task에 운영상 resolution이 기록됨
- `task_archived`: task가 archived 상태로 전환됨
- `task_startup_stalled`: Codex startup/no-progress watchdog이 의미 있는 JSONL 진행 없이 child process를 종료함
- `task_mutated`: queue mutation plan 또는 제한된 queue command가 task metadata나 실행 계획을 변경함
- `task_worktree_applied`: accepted worktree task branch가 main worktree에 fast-forward 적용됨
- `dependency_changed`: task dependency graph가 변경됨
- `cooldown_updated`: 운영자가 global cooldown을 수동으로 설정하거나 해제함
- `rate_limit_detected`: rate-limit 또는 usage-limit cooldown이 설정됨
- `git_commit_detected`: task 결과 또는 local inspection에서 관련 commit metadata가 관측됨
- `git_push_detected`: task 결과 또는 local inspection에서 push 상태 변화가 관측됨
- `notification_sent`: notifier가 event에 대한 외부 알림 전송을 완료함

현재 구현은 snake_case 이름을 사용합니다. `task.accepted`, `task.rejected` 같은 세부 review decision은 `task_reviewed` event의 `review_status` payload field로 표현합니다. `lock.stale_recovered` 같은 더 세부적인 상태 변화는 추가 event type 또는 `task_mutated` subtype/status field로 표현할 수 있습니다.

각 event payload는 consumer에 필요한 최소 안전 필드만 포함합니다.

- `event_id`: 중복 처리 방지용 고유 id
- `occurred_at`: event 발생 시각
- `task_id`: task 관련 event일 때의 task id
- `project_id`: project routing metadata
- `status`: task 상태 전이와 관련 있을 때의 status
- `review_status`: accept/reject/follow-up review 상태와 관련 있을 때의 review state
- `resolution`: resolved task의 운영상 처리 결정
- `attempts`: event 시점의 task attempt count
- `summary_excerpt`: 사람이 알림에서 읽을 수 있는 짧은 요약

Event envelope includes `schema_version`, `event_id`, `event_type`, `occurred_at`, optional `task_id`, optional `project_id`, optional `project_root`, `actor`, `source`, `summary`, and sanitized `payload`.

## Parent-attention durable outbox

Parent linkage가 있는 worker attention state는 일반 audit event와 별도로 `parent_attention_outbox_dir`의 event별 JSON 파일에 durable outbox record로 저장합니다. Record는 opaque `parent_ref`, `work_item_ref`, deterministic `event_id`, `completion_id`, `wake_reason`, sanitized summary/evidence reference, delivery state와 attempt metadata를 포함합니다. 동일 linkage/completion/reason 수집은 같은 id를 생성하므로 중복 파일이나 중복 delivery를 만들지 않습니다.

Delivery state는 `pending`, `retry_wait`, `delivered`, `acknowledged`, `unavailable`, `failed`를 구분합니다. Adapter failure는 configured maximum까지 exponential backoff하며, 성공은 acknowledgement와 분리합니다. Command 미설정은 `unavailable`, bounded retry 소진은 `failed`로 남습니다. `cbr parent-attention deliver EVENT_ID`는 configured argv command에 public-safe JSON을 stdin으로 전달하며 raw prompt, transcript, path, secret을 전달하지 않습니다. Adapter는 `event_id`를 idempotency key로 사용해야 합니다.

현재 Codex App parent task에 메시지를 보내는 stable non-UI integration surface는 이 저장소에서 검증되지 않았습니다. 따라서 Codex-specific hidden adapter는 제공하지 않고 local operator가 검증한 command만 opt-in할 수 있습니다. Parent wake는 collection/disposition 요청이며 전체 root goal 완료나 archive 승인이 아닙니다.

Payload 원칙:

- event는 작고 구조화된 record로 유지합니다.
- transcript, raw Codex JSONL log, prompt 원문, session id, thread id, credential, Telegram token/chat id, 환경 변수 값, secret으로 볼 수 있는 문자열을 넣지 않습니다.
- private prompt는 기본적으로 저장하지 않습니다. 꼭 필요한 경우 operator가 명시적으로 제공한 sanitized summary 또는 짧은 excerpt만 저장합니다.
- Git metadata는 commit hash, subject excerpt, ahead/behind, pushed 여부처럼 필요한 최소 정보만 저장하고 diff 전문은 event에 넣지 않습니다.
- 알림이나 dashboard에서 더 자세한 확인이 필요하면 operator가 로컬에서 `cbr summary`, `cbr review-bundle`, `cbr transcript`를 직접 실행합니다.

Minimal implementation emits events from `enqueue`, `run-next` task transitions, `accept`, `reject`, `resolve`, `archive`, manual cooldown changes, and rate-limit detection. Event write failures are non-fatal warnings; canonical task JSON remains the source of truth.

Event log가 필요한 이유:

- Queue mutation/replan: task가 왜 pause, dependency change, supersede, follow-up 상태가 되었는지 append-only history로 남길 수 있습니다.
- Review bundle: reviewer가 현재 task JSON만으로 알기 어려운 상태 변화 순서와 운영 결정을 self-contained하게 재구성할 수 있습니다.
- Telegram notifications: notifier가 task 파일 polling만으로 놓치기 쉬운 edge-triggered 변화를 cursor 기반으로 중복 없이 처리할 수 있습니다.
- Dashboard: status counts, recent activity, unresolved failures, review backlog, rate-limit history를 매번 전체 JSONL transcript에서 재계산하지 않아도 됩니다.
- Post-hoc debugging: runner crash, stale lock recovery, rate-limit, git metadata 관측, notification failure 같은 운영 사건을 나중에 시간순으로 확인할 수 있습니다.

Notifier는 각자 cursor와 전송 상태를 public repository 밖에 저장합니다. 예를 들어 notifier는 `.codex-batch-runner/notify-state.json`이나 사용자 local config/state 파일에 마지막 처리 event file, byte offset, 마지막 event id, 전송 실패 retry metadata를 저장할 수 있습니다. Notifier state는 adapter별로 독립적이어야 하며, 한 notifier의 장애가 다른 notifier의 cursor를 변경하지 않아야 합니다.

Notifier cursor v1 core는 read-only cursor loader/validator/advance planner skeleton입니다. External adapter, Telegram 전송, token/chat id, ack/snooze/mute schema, public/private config schema 변경은 이 core contract 범위 밖이며 local/private opt-in future work입니다.

Canonical cursor identity는 `current_event_file` + `current_byte_offset`입니다. `last_processed_event_id`, `last_processed_occurred_at` 같은 timestamp/checkpoint field는 운영자가 상태를 읽거나 adapter가 retry state를 정리할 때 도움을 주는 optional metadata일 뿐 primary cursor가 아닙니다. Event replay와 prune safety는 `occurred_at + event_id` 기준으로 파일 위치를 추정하지 않고 byte offset을 기준으로 진행합니다.

Duplicate suppression은 먼저 `event_id`를 사용합니다. Cursor state는 bounded `recent_event_ids` ring/list를 보존할 수 있으며, 이미 본 key는 notifier adapter가 다시 전송하지 않아야 합니다. Legacy 또는 malformed event에 `event_id`가 없으면 fallback key로 `(event_type, task_id, occurred_at)`를 사용할 수 있지만, 이는 best-effort 중복 억제일 뿐 canonical event identity가 아닙니다. 이 fallback을 사용해도 cursor advance는 계속 `current_byte_offset` 기반입니다.

Cursor state가 missing, malformed, unreadable이거나 `current_event_file`이 configured `event_dir` 밖을 가리키면 event pruning은 block되어야 합니다. Cursor가 현재 event file을 완전히 처리하지 않았으면 해당 file을 삭제하지 않아야 합니다. Existing `cbr prune --notifier-cursor-state` safety는 이 v1 contract의 일부입니다.

JSONL Codex attempt logs와 event logs는 장기 운영에서 계속 커질 수 있습니다. Retention policy는 review와 audit 요구사항이 충족된 뒤 오래된 runtime logs/events를 정리할 수 있어야 합니다. 장기 기본 정책은 60일보다 오래된 runtime log와 event file을 cleanup 후보에 포함하는 방향입니다. Current `cbr prune` reports old event JSONL files under configured `event_dir` as distinct event candidates and deletes them only when `--apply` is explicit. If notifier cursor state paths are configured, event pruning checks them before deleting old event files. Cursor state상 아직 처리되지 않았거나 fully processed 여부가 불확실한 event file은 삭제하지 않고 skipped warning으로 보고합니다. Event pruning 이후에는 retained file만으로 전체 과거 event history를 다시 만들 수 있다고 보장하지 않습니다. Rebuild 보장은 retained canonical task JSON과 retained event JSONL set에 적용합니다.

SQLite는 초기 source of truth가 아니라 derived read index/cache입니다. SQLite index는 retained task JSON 파일과 retained event log에서 재생성 가능해야 하며, dashboard, notification, search, automated review workflow가 빠르게 조회하기 위한 optional layer로 둡니다. SQLite에는 prompt, transcript, raw Codex JSONL, session id, thread id, credential, environment value 같은 sensitive raw fields를 저장하지 않고 sanitized projection만 저장합니다. SQLite 파일이 없거나 손상되어도 `cbr enqueue`, `cbr list`, `cbr run-next`, `cbr accept/reject`, `cbr prune` 같은 core command는 canonical task JSON 파일과 event log만으로 계속 동작해야 합니다. 복구 방법은 손상된 SQLite 파일을 삭제하고 retained task JSON 및 retained event log에서 index를 다시 build하는 것입니다.

Telegram integration은 optional adapter입니다. Core runner는 Telegram에 직접 의존하지 않고 append-only event log만 기록합니다. Telegram token, chat id, enable flag, rate limit, formatting option은 local-only config나 runtime state에만 저장하며 public docs와 examples에는 실제 값을 포함하지 않습니다.
