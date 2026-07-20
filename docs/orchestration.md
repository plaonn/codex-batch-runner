# Closed-loop orchestration planner

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
