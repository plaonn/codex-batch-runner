# Closed-loop orchestration

`cbr orchestration plan --manifest PATH [--json]` is a manifest-only,
deterministic, read-only planner. It does not load CBR configuration, read or
write queue/event/state/config runtime data, invoke adapters or subprocesses,
create threads, or dispatch work. Global `--config` is a usage error for this
command. Invalid input is printed to stdout with exit 2; valid `ready`,
`needs_user_decision`, and `blocked` outcomes exit 0.

## Intake: `orchestration-intake-v1`

The input is one UTF-8 JSON object no larger than 64 KiB. Its exact top-level
keys are `schema_version` (`1`), `contract` (`"orchestration-intake-v1"`),
`request_id`, `idempotency_key`, `source`, `summary`, `authority`, `work`,
`mutation`, `automation_boundary`, and `surface_preferences`. Identifiers match
`^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$`.

- `source` has exactly `kind` (`codex_parent_thread`, `codex_user_owned_thread`,
  `todoist_task`, `operator`, `automation`, `other`) and `collection_owner`
  (`source_parent`, `source_user`, `operator`, `external_owner`). Valid pairs are
  parent/source_parent, user-owned/source_user, todoist/operator,
  operator/operator, automation/operator, and other/external_owner.
- `summary` has exactly `root_goal`, `requirement`, `stop_condition`, and
  `done_means`: each is non-empty issuer-sanitized opaque text of at most 512
  characters. It is never classified, echoed, or used for eligibility.
- `authority` has exactly `decision_authority`
  (`proposal_only`, `recommend_and_pause`, `delegated_decision`,
  `bounded_experiment`), `resolution` (`resolved`, `needs_user_decision`,
  `blocked_external`), `impact` (`low`, `medium`, `high`), and `approval_state`
  (`not_required`, `granted`, `required`).
- `work` has exactly `kind` (`architecture_policy`, `discussion`,
  `implementation`, `review`, `verification`, `operations`), `interaction`
  (`none`, `user_required`, `external_required`), `duration` (`short`,
  `bounded`, `long`), `persistence` (`turn_bound`, `durable`), `resume`
  (`not_needed`, `required`), `dependency` (`none`, `soft`, `hard`),
  `collection` (`immediate_parent`, `durable_attention`, `user_continuation`,
  `external_callback`), `context` (`parent_context`, `self_contained`,
  `independent_context`), `isolation` (`none`, `worktree`, `required`),
  `verification` (`none`, `objective`, `semantic`, `mixed`),
  `external_worker_boundary` (`unavailable`, `verified_bounded`), and
  `repository_scope` (`none`, `present`).
- `mutation.allowed` and `mutation.prohibited` are unique enum lists (maximum
  eight) of `read_only`, `local_files`, `tracked_files`, `runtime_state`,
  `external_state`, `destructive`. `surface_preferences` is a non-empty unique
  list (maximum eight) of `codex_parent_thread`, `codex_user_owned_thread`,
  `codex_subagent`, `cbr_batch`, `external_worker`.
- `automation_boundary` is `manual_only`, `advisory_only`, or
  `bounded_automatic`.

Unknown/missing fields and nesting are rejected. Source references, repository
paths, task/thread/session IDs, raw prompts, transcripts, logs, credentials,
environment dumps, commands, and argv fields are not accepted.

Cross-field rules: resolved requires not-required or granted approval; required
approval requires `needs_user_decision`; blocked-external requires external
interaction or callback; mutation lists cannot overlap; allowed `read_only` must
be its only item and prohibited cannot contain it. `proposal_only` requires only
read-only mutation, not-required approval, and manual/advisory automation.
`recommend_and_pause` permits only read-only/local-files and manual/advisory
automation. `bounded_experiment` permits only read-only/local/tracked files.
Bounded automation requires delegated-decision or bounded-experiment, resolved,
and non-required/granted approval. `external_worker` is only eligible with a
verified bounded worker boundary.

## Canonical plan and validation error

All input strings are NFC-normalized. The validated manifest is canonical JSON:
UTF-8, keys sorted lexicographically, `ensure_ascii=false`, separators `(',', ':')`.
Mutation lists are ordered `read_only`, `local_files`, `tracked_files`,
`runtime_state`, `external_state`, `destructive`; preference order is preserved.
`request_fingerprint` is `sha256:` plus lowercase SHA-256 of those bytes; it does
not expose the canonical bytes, idempotency key, or summary text.

A valid plan has exactly these keys: `schema_version`, `contract`, `request_id`,
`request_fingerprint`, `decision_status`, `recommended_surface`,
`fallback_surfaces`, `reason_codes`, `excluded_surfaces`,
`unresolved_constraints`, `required_preflight`, `collection_owner`,
`execution_constraints`, `mutation`. `execution_constraints` copies decision
authority, normalized allowed/prohibited mutation classes, and automation boundary.
`mutation` is always `{"allowed":false,"applied":false}`.

An invalid plan has exactly: `schema_version`, `contract`
(`orchestration-plan-error-v1`), `request_id` (`null`), `decision_status`
(`invalid`), `recommended_surface` (`null`), `fallback_surfaces` (`[]`),
`reason_codes` (`["manifest_invalid"]`), `validation_errors`,
`excluded_surfaces` (`[]`), `unresolved_constraints`
(`["valid_manifest_required"]`), `required_preflight` (`[]`),
`collection_owner` (`null`), and the false/false mutation object. Validation codes
are unique and ordered: `input_unreadable`, `input_too_large`, `input_not_utf8`,
`input_json_invalid`, `input_not_object`, `fields_invalid`,
`value_type_invalid`, `value_enum_invalid`, `value_bounds_invalid`,
`unsafe_identifier`, `sensitive_field_forbidden`, `duplicate_list_item`,
`empty_surface_preferences`, `mutation_overlap`, `cross_field_conflict`.

## Eligibility and selection

Global user-decision gate returns no surface/fallback/exclusions, ordered applicable
reasons `authority_resolution_requires_user_decision`, `approval_required`,
`unresolved_constraints=["user_decision_required"]`, and preflight
`obtain_user_decision`. External block gate similarly uses ordered applicable
`authority_blocked_external`, `external_interaction_required`, unresolved
`external_blocker`, and `resolve_external_blocker`.

Otherwise only preferred surfaces are evaluated, in supplied order. Parent is
eligible if architecture/discussion **or** parent context **or** immediate-parent
collection; the OR-group failure is `context_incompatible`. User-owned is eligible
for user interaction **or** user continuation; its OR-group failure is
`interaction_incompatible`. Subagent requires no interaction, short/bounded,
turn-bound, no resume, none/soft dependency, immediate parent collection, low/medium
impact, and no external/destructive allowed mutation. CBR requires no interaction,
non-architecture/discussion work, and one of long/durable/resume-required/hard
dependency/durable-attention; that five-way OR failure is `persistence_incompatible`;
it also requires low/medium impact and no external/destructive mutation. External
worker requires no interaction, verified boundary, objective/mixed verification,
low/medium impact, and no external/destructive mutation.

Failed predicate codes are ordered: `interaction_incompatible`,
`work_kind_incompatible`, `duration_incompatible`, `persistence_incompatible`,
`resume_incompatible`, `dependency_incompatible`, `collection_incompatible`,
`context_incompatible`, `impact_incompatible`, `mutation_boundary_incompatible`,
`verification_incompatible`, `external_worker_boundary_unverified`. The first
eligible preference is selected; later eligible preferences are fallbacks. No
eligible surface returns `blocked`, `no_eligible_surface`, and
`surface_constraints_unsatisfied`. A non-preferred surface is never evaluated or
emitted. Ready reason codes are `selected_first_eligible_surface` plus one selected
surface code. Preflight is respectively none, `confirm_user_continuation`,
`verify_immediate_parent_collection`, `verify_cbr_admission`, or
`verify_external_worker_contract`. `collection_owner` only assigns result
collection/disposition responsibility; it does not prove root-goal completion.

## Explicit CBR dispatch

`cbr [--config CONFIG] orchestration dispatch-cbr --manifest MANIFEST
--execution-envelope PRIVATE_PATH (--dry-run | --apply)
[--confirm-request-id REQUEST_ID] [--json]` is the only D2 dispatch surface.
It recomputes the D1 plan and fails closed unless the exact recommended surface
is `cbr_batch`. It does not accept a fallback surface. Dispatch supports only
the Codex execution backend.

The execution envelope is a runtime-private UTF-8 JSON object no larger than
256 KiB. It has exact keys `schema_version=1`,
`contract=orchestration-cbr-execution-v1`, `request_id`,
`request_fingerprint`, `prompt`, `cwd`, `origin_parent_ref`, and `task`.
`task` has exact keys `title`, `description`, `project_id`, `category`,
`labels`, `depends_on`, `verification_scope`, `capacity_pool`, and `priority`.
The prompt is non-empty and at most 128 KiB after NFC normalization. The cwd is
an existing absolute directory and is resolved strictly without `~` or
environment expansion. The parent reference is opaque, non-empty, and at most
512 characters.

Task title and description use collapsed whitespace and are bounded to 80 and
2,048 characters. Public-safe identifiers use
`^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$`. Labels and dependencies are unique,
at most 32 items, and sorted. Verification values are unique and ordered as
`docs`, `lint`, `typecheck`, `unit`, `integration`, `e2e`, `smoke`, `manual`,
`build`. Priority is `asap`, `high`, `normal`, `low`, or `background`.
Commands, credentials, environment dumps, session/thread IDs, transcripts,
logs, and arbitrary nested metadata are rejected.

`--dry-run` rejects confirmation, does not acquire the queue lock, and does not
create directories, tasks, receipts, events, triggers, subprocesses, threads,
or adapter calls. It reads only already-existing config-derived paths and
returns `orchestration-dispatch-preview-v1`. `ready` and
`already_dispatched` exit 0; `blocked` and `conflict` exit 2. Capacity pressure
and existing-but-not-ready dependencies are ordered advisory
`admission_blockers`; missing dependencies, deterministic self-dependency,
unknown capacity pools, worktree isolation mismatch, pause, and identity
conflicts block admission.

`--apply` requires an exact request-id confirmation. Under the queue lock it
revalidates the plan, envelope, authority, config-derived gates, pause, task,
and receipt. It permits only `delegated_decision` or `bounded_experiment`,
manual or bounded-automatic issuer boundaries, resolved low/medium-impact work
without external or destructive worker mutations. Proposal, recommend-and-pause,
advisory-only, fallback, shell, external-command, and automatic dispatch are
not supported.

The normalized immutable execution projection is canonical JSON with sorted
keys, UTF-8, `ensure_ascii=false`, and compact separators.
`execution_fingerprint` is its SHA-256. A private SHA-256 digest of the manifest
idempotency key and the fixed `cbr_batch` surface produces deterministic
`od-<32 hex>` dispatch and `orch-<32 hex>` task IDs. The raw idempotency key,
its digest, and canonical bytes are never stored or returned. A changed
manifest or envelope under the same idempotency key conflicts.

The task's first atomic write includes `origin_parent_ref`,
`orchestration_dispatch_id`, `orchestration_request_fingerprint`, and
`orchestration_execution_fingerprint`. Those values and the execution
projection are immutable. Orchestrated admission suppresses `task_created` and
emits a best-effort `orchestration_task_admitted` event containing only
dispatch/task IDs, surface, and the two fingerprints.

Receipts are stored below the configured runtime at
`orchestration-dispatch-receipts/{dispatch_id}.json` as exact immutable
`orchestration-dispatch-receipt-v1` objects. A matching retry returns the exact
stored receipt without rewriting it. A task created before a process
interruption but missing its receipt is matched from immutable task fields and
recovered without creating another task. A receipt without a task, malformed
receipt, or task/provenance drift conflicts. Receipts prove only queue admission:
execution, review, root-goal completion, parent-attention creation, delivery,
and acknowledgement remain separate canonical states.

After each successful apply invocation (created, recovered, or matching retry),
the existing post-mutation trigger is invoked once after lock release on a
best-effort basis. Trigger delivery is not receipt truth. When the queued task
later reaches an attention state, the stored opaque parent reference makes it
eligible for the existing runtime-private `parent_attention_required` outbox.
D2 neither delivers nor acknowledges that record.
