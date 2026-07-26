# Orchestration selection funnel

`orchestration-selection-funnel-projection-v1` is a deterministic, read-only
view over trusted durable orchestration evidence. It does not dispatch, enqueue,
warn, block, enforce, select, mutate routing/default/provider/queue policy, or
write runtime state.

```bash
cbr orchestration selection-funnel \
  --manifest intake.json \
  --execution-envelope private-execution.json \
  --selection-receipt private-selection-receipt.json \
  --json
```

The command validates the owner-only selection receipt, recomputes its exact
manifest/D1 binding, validates the private execution envelope, and then reads
current runtime evidence without taking a lock or creating directories.

## Stages and statuses

The fixed stage order is:

1. `durable_eligible`
2. `planned`
3. `selected`
4. `admitted`
5. `completed`
6. `accepted`
7. `applied`
8. `parent_attention_recorded`

Every stage has one fixed status:

- `observed`: exact trusted evidence is present.
- `not_observed`: the trusted source is cleanly absent or a known prerequisite
  has not been reached.
- `unknown`: evidence is malformed, conflicting, unevaluated, stale, or no
  trusted adapter exists.
- `not_applicable`: the literal stage does not apply. In v1 this is used for
  accepted non-worktree execution because the existing post-accept
  `not_worktree` result is not an explicit apply attestation.

The first three stages come only from the exact source-bound
`orchestration-selection-decision-v1` embedded in the immutable receipt.
`durable_eligible` preserves D1's evaluated/null eligibility semantics;
`planned` is the D1 recommended surface; `selected` is the explicit recorded
surface.

For non-CBR surfaces all downstream stages are `unknown` because v1 has no
separately trusted adapter. The report never fabricates evidence for Codex
threads, subagents, or external workers.

## CBR join

Downstream evidence is joined only when the exact recorded selection is
`cbr_batch` with `would_warn=false`.

- `admitted` requires the recomputed dispatch identity to match both the
  immutable `orchestration-dispatch-receipt-v1` and canonical queue task.
- `completed` requires canonical `status=completed` (or an exact passed
  `previous_status=completed` archive chain), `last_result.status=completed`,
  and monotonic terminal timestamps.
- `accepted` requires canonical `review_status=accepted` and a valid review
  timestamp.
- `applied` requires a worktree task with applied status/timestamp/head/target
  and current Git ancestry verification. Metadata alone is insufficient.
- `parent_attention_recorded` requires an exact stable outbox identity bound to
  the task, parent reference, canonical `completed_at`, wake reason, and valid
  delivery-record shape.

Events are ancillary because their writes may be nonfatal. They are not used as
canonical completion, acceptance, or apply evidence.

`parent_attention_recorded` means only that a durable wake/collection request
exists. It does not mean delivery, acknowledgement, parent collection, source
disposition, or root completion. The report fixes all corresponding authority
claims to false.

## Privacy and safety

Output contains stable digests, surface/stage enums, reason codes, and coarse
booleans only. It excludes raw task/dispatch/request/parent identity, prompt,
transcript, logs, local paths, branch/ref details, commands, credentials,
session/thread/user/account identity, and provider quota identity. Private input
or filesystem failures return sanitized errors.

The command performs no queue scan beyond the exact recomputed task, no
dispatch/enqueue, no event or receipt repair, no parent-attention delivery, no
provider call, and no worker invocation. A report digest detects output
tampering but grants no routing or completion authority.
