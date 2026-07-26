# Orchestration selection shadow evidence

`orchestration-selection-decision-v1` compares an explicitly supplied selected
surface with the existing D1 `orchestration-plan-v1` result. It is additive,
report-only evidence: D1 remains the only source of candidates, eligibility,
recommended surface, reason codes, required preflight, and collection owner.
The shadow does not select, dispatch, enqueue, warn, intercept, or mutate queue,
provider, routing, default, or policy state.

## Preview

```bash
cbr orchestration selection-preview \
  --manifest intake.json \
  --source-contract-digest sha256:... \
  --selected-surface codex_subagent \
  --evaluated-at 2026-07-26T11:30:00+09:00 \
  --json
```

The manifest digest must equal the stable digest of the validated intake and
the policy revision defaults to
`orchestration-selection-shadow-policy-v1`. The deterministic
`orchestration-selection-preview-v1` contains the exact candidate order,
explicit eligible set, eligibility snapshot, recommendation and selection,
stable decision and preview digests, optional exact-bound override, and
`mutation=false`. Early blocked D1 outcomes preserve candidate eligibility as
unevaluated (`evaluated=false`, `eligible=null`) instead of inventing
ineligibility.

Selection reason codes are fixed:

- `selected_recommended_surface`
- `selected_authorized_override`
- `selection_missing`
- `selected_surface_not_candidate`
- `selected_surface_ineligible`
- `selected_without_valid_override`
- `override_authority_insufficient`
- `override_scope_mismatch`
- `override_expired`
- `source_binding_mismatch`
- `policy_revision_mismatch`

`would_warn=false` means the selection matches the recommendation or uses a
valid exact-bound override. It is `true` for an advisory mismatch and `null`
for invalid, blocked, or missing decisions. No warning is emitted.

An override has exact fields `actor_kind`, `authority`, `reason_code`, `scope`,
and optional `expires_at`. Scope binds the request fingerprint, policy
revision, and selected surface. Invalid override data is reduced to stable
reason codes and is not retained in evidence.

## Explicit private recording

```bash
cbr orchestration selection-record \
  --manifest intake.json \
  --preview preview.json \
  --dry-run \
  --json
cbr orchestration selection-record \
  --manifest intake.json \
  --preview preview.json \
  --apply \
  --confirm-decision-id sha256:... \
  --json
```

Before loading runtime configuration, both modes revalidate the preview and
recompute it from the supplied manifest's canonical D1 result. A self-consistent
but forged or drifted preview fails closed. Dry-run does not create directories,
receipts, events, locks, queue tasks, or other runtime state. Apply requires the
exact decision digest and writes one
owner-only immutable `orchestration-selection-receipt-v1` below the runtime
state parent plus one sanitized `orchestration_selection_recorded` event.
Exact retry returns the same receipt and repairs a missing event; malformed or
divergent identity fails closed.

Receipt paths and contents are runtime-private. Public evidence excludes raw
prompt, transcript, logs, task/thread/session/user/account identifiers, personal
paths, commands, credentials, raw Todoist content, provider quota identity, and
arbitrary actor names. A receipt proves only that this selection shadow was
recorded. It does not prove dispatch, execution, review, completion, collection,
parent completion, or authority to activate routing.
