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

Dispatch evaluates manifest, envelope, request binding, deterministic identity,
plan/surface/authority, confirmation, config, then runtime state in that order.
Binding and config-independent plan/authority failures do not discover config.
Binding failures keep request and dispatch IDs null; later failures may report
the validated IDs.

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

Every queue creation path publishes a fully-written same-directory temporary
JSON file with an atomic exclusive no-clobber operation. Concurrent ordinary
enqueue and orchestration dispatch therefore cannot replace one another.
Exclusive-create failure makes dispatch re-read and classify the deterministic
task. Immediately before receipt creation, dispatch re-reads the task and
requires the complete immutable identity to match; missing or drifted state
cannot produce a receipt.

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

## D3 guarded reconciliation shadow

`cbr [--config CONFIG] orchestration reconcile-shadow --policy PRIVATE_PATH
--trigger PRIVATE_PATH --manifest MANIFEST
--execution-envelope PRIVATE_PATH [--json]` is the D3-0 read-only contract and
status surface. It validates one immutable trigger bundle against one exact
local/private policy revision, recomputes the D1 plan and D2 identity, and reads
already-existing task, receipt, and parent-attention state. It never acquires
the queue lock, creates directories or records, dispatches a task, repairs a
receipt or outbox, invokes a trigger/adapter/subprocess, or changes a
coordination surface. Public output omits prompt, cwd, parent reference, input
paths, and raw policy contents.

The exact `orchestration-guard-policy-v1` object has top-level keys
`schema_version`, `contract`, `policy_id`, `revision`, `active`,
`activation_mode`, `source`, `scope`, `evidence`, and `rollout`.

- `activation_mode` is `shadow` or `guarded`. D3-0 accepts the versioned value
  but reports `activation_not_implemented` for `guarded`; there is no guarded
  mutation path in this phase.
- `source` binds exact `source_id` and `adapter_revision`.
- `scope` binds non-empty allowlists for source kind, project, canonical
  repository root, work kind, decision authority, impact, worker mutation,
  isolation, work verification, and capacity pool. It also binds the required
  verification-scope subset. Required prohibited mutations are exactly
  `external_state` and `destructive`; worker `runtime_state` mutation is not an
  allowed D3 policy value.
- `evidence` records `provenance=operator_attested_explicit_d2`, a public-safe
  cohort ID, successful explicit D2 dispatch count, identity-conflict count,
  and safety-violation count. The policy author owns this attestation; D3-0
  does not infer success from receipt existence. Eligibility requires at least
  five attested successful explicit dispatches and zero conflicts or
  violations.
- `rollout.max_new_admissions_per_run` must be exactly one. The value is a
  future guarded-activation ceiling, not permission for D3-0 to admit work.

Policy fingerprints are SHA-256 over the normalized exact policy object. Unknown
fields, invalid enum/list values, relative or nonexistent repository roots,
duplicate list items, and broader rollout values fail closed.

The exact immutable `orchestration-trigger-v1` object contains
`schema_version`, `contract`, `trigger_id`, `source_id`,
`source_adapter_revision`, `source_event_id`, `explicit_opt_in`, `policy_id`,
`policy_revision`, `policy_fingerprint`, `request_id`,
`request_fingerprint`, `execution_fingerprint`, and timezone-aware
`created_at`. `trigger_id` is deterministic from the trigger contract, source
ID, and source event ID. The D2 manifest idempotency key must be the fixed D3
namespace plus the trigger digest. Policy revisions are bindings, not part of
trigger identity: changing a policy cannot turn the same source event into a
new dispatch.

Shadow eligibility additionally requires:

- an active policy and explicit trigger opt-in;
- exact source, adapter, policy, request, execution, and idempotency bindings;
- `bounded_automatic`, resolved `delegated_decision` or
  `bounded_experiment`, low/medium impact, and exact allowlist membership;
- required external/destructive prohibitions and verification scope;
- D1 `ready` with exact `cbr_batch`; and
- a non-conflicting D2 preview.

Any missing opt-in, insufficient evidence, policy/source/request drift,
repository/scope mismatch, unsupported activation, unreadable attention state,
unreadable D2 task/receipt state, or D2 block/conflict returns `blocked` with
`mutation={"allowed":false,"applied":false}`. The shadow report keeps queue
admission, execution, review, apply, attention delivery, attention
acknowledgement, and source disposition as separate fields. A D2 receipt can
therefore produce `queue_admission=admitted` while execution remains
`runnable`, review/apply remain unstarted, and attention remains un-emitted.
Unreadable attention files are reported as `unknown` with
`attention_state_unreadable`; they are never treated as absence.

D3-0 does not implement durable reconciliation-state writes, receipt or
attention repair, retry leases, guarded dispatch, disposition outbox delivery,
parent-attention delivery/acknowledgement, non-CBR adapters, or coordination
surface mutation. Those operations require a later activation contract and
must reuse the deterministic D2 task/receipt identity rather than introducing a
second queue-admission path.

## D3-1 local ingress and durable shadow state

`cbr [--config CONFIG] orchestration publish-local-ingress --bundle
PRIVATE_PATH (--dry-run | --apply) [--confirm-source-event-id EVENT_ID]
[--json]` is the only implemented source publisher. It accepts one exact
runtime-private `orchestration-local-ingress-v1` bundle and never dispatches a
task. The producer identity is fixed to `cbr-local-operator-ingress` revision
`cbr-local-ingress-v1`; arbitrary adapter, connector, task-dashboard, planning,
handoff, issue, or thread records are not source inputs.

The bundle contains exact `producer`, `source_event_id`, `explicit_opt_in`,
timezone-aware `created_at`/`expires_at`, policy, manifest, and execution
envelope fields. It is at most 384 KiB. The publisher derives the D3 trigger;
callers cannot supply a replacement trigger. The event ID fixes the trigger
and D2 idempotency identity. A bundle is publishable only when all of the
following initial-lane constraints hold:

- source and collection owner are exactly `operator`;
- surface preference is exactly `cbr_batch` and automation is
  `bounded_automatic`;
- the exact policy has singleton source/project/repository/work/authority/
  impact/mutation/isolation/verification/capacity scope matching the request;
- work and task category are `verification`, verification is `objective`, no
  dependency or interaction is present, and worker mutation is exactly
  `read_only`;
- impact is low, runtime/external/destructive worker mutation is prohibited,
  and the policy keeps the existing one-admission future rollout ceiling; and
- the event is not more than five minutes in the future, has not expired, and
  has a lifetime no longer than 24 hours.

`--dry-run` neither loads a queue lock nor creates runtime state. `--apply`
requires the exact event-ID confirmation, uses the existing runner lock, and
publishes a fully written record with exclusive no-clobber semantics below
`orchestration-ingress/`. The directory and records must be regular,
current-user-owned, and inaccessible to group/other users. Matching retries
return `already_published`; malformed, insecure, or divergent records fail
closed. Publish does not emit an orchestration receipt, task, event, outbox,
trigger command, subprocess, or adapter call.

`cbr [--config CONFIG] orchestration reconcile-local-shadow
--source-event-id EVENT_ID (--dry-run | --apply)
[--confirm-source-event-id EVENT_ID] [--json]` reloads the published bundle,
derives its exact trigger, and reuses D3-0 reconciliation. Dry-run is strictly
read-only. Apply requires exact confirmation and atomically stores only a
sanitized `orchestration-reconciliation-state-v1` record below
`orchestration-reconciliation/`. The record contains source/trigger/bundle
identity, shadow decision and reason codes, the already separated lifecycle
projection, observation timestamps, and observation count. It never contains
prompt, cwd, parent reference, or input paths. Matching observations update the
same state atomically; malformed or identity-drifted prior state is not
overwritten.

The local ingress publisher and reconciliation-state writer are not guarded
activation. They do not poll for records, acquire retry leases, call D2
dispatch, repair task/receipt/outbox state, deliver or acknowledge attention,
write source disposition, mutate coordination systems, or expand policy from
observations. `activation_mode=guarded` remains rejected. A later activation
contract must retain exact source/event/D2 identity, define bounded consumer
leases and retry/disposition semantics, and receive separate authorization.

## D3-2 explicit one-event guarded consumer

`cbr [--config CONFIG] orchestration consume-local-ingress
--source-event-id EVENT_ID (--dry-run | --apply)
[--confirm-source-event-id EVENT_ID] [--json]` is the first guarded activation
slice. It consumes exactly one named D3-1 event and delegates queue admission to
the existing D2 dispatcher. It never scans the ingress directory and does not
add a second queue writer.

Eligibility requires an unexpired exact D3-1 bundle with
`activation_mode=guarded`, the existing read-only verification singleton lane,
and at least one matching durable D3-1 shadow observation whose only blocker
was `activation_not_implemented`. The consumer rechecks source, policy, trigger,
request, execution, idempotency, D2 state, and private record identity on every
attempt. Generic `reconcile-shadow` continues to reject guarded activation;
only this one-event consumer can opt into the guarded evaluator.

Apply requires exact event-ID confirmation. Under the existing runner lock it
claims a private `orchestration-consumer-state-v1` lease, then releases the lock
before calling D2, which acquires the same lock for its atomic admission. The
lease lasts 120 seconds. At most three admission attempts are allowed, with
30-second and 120-second retry waits. Only `lock_busy` and `runner_paused` are
automatic retry reasons. Expired leases are reclaimable; unknown failures,
identity drift, policy violations, expiry, and D2 conflicts fail closed. If a
process crashes after D2 admission, the next attempt recovers from the existing
deterministic task and immutable D2 receipt rather than creating another task.

Terminal consumption writes one immutable
`orchestration-source-disposition-v1` record with result `admitted`,
`blocked_terminal`, or `retry_exhausted`. It contains only source/trigger/bundle/
dispatch/task identity and sanitized reason codes. The mutable consumer state
can be reconstructed from that disposition after a crash. Neither record
contains prompt, cwd, parent reference, raw error, transcript, or credentials.
`cbr doctor` reports consumer phase counts, expired leases, disposition count,
and invalid private records without repairing them.

An admitted disposition proves source consumption and D2 queue admission only.
Execution, completion, review, apply, parent-attention delivery/acknowledgement,
and source-system delivery remain separate. D3-2 does not poll, modify launchd
or runtime configuration, invoke a task in the same operation, deliver a
disposition externally, mutate a coordination surface, or expand policy from
observations. Automatic ingress discovery is a separate later rollout gate.
