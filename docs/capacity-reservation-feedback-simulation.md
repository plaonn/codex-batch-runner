# Capacity reservation and feedback simulation

`capacity-reservation-feedback-simulation-v1` is a deterministic, public-safe,
report-only contract. It previews an exact-bound reservation, append-only
observational feedback, half-open eligibility, and retry safety without
reserving capacity, calling a provider, or changing a task.

Current prerequisite gap: the upstream selector report does not express a
manual-override binding. This simulator records
`manual_override_binding_resolved=false` and fails closed with
`manual_override_binding_not_expressed_by_selector_report`. The implementation
cannot be accepted or activated until that predecessor policy gap is resolved
in the authoritative selector artifact. This simulator does not invent the
missing policy axis.

The strict request has these authoritative sections:

- `mapping` and `admission_policy` embed complete
  `provider-resource-mapping-v2` and
  `provider-resource-admission-policy-v1` artifacts. Existing standalone
  validators run before any preview. Exactly one source-attested mapping
  binding must be current for the exact target at replay time, and an enabled
  target policy rule must admit its provider and window.
- `currentness_evidence` is `{body, evidence_digest}`. Its body binds the exact
  target, canonical resource, opted-in scope, mapping/policy/currentness
  revisions, stable mapping and policy artifact digests, source observation and
  expiry timestamps, and `identity_authority=source_attested`. Caller-authored
  status labels are not authority.
- `selector_binding.activation_report` embeds a complete standalone-validated
  `capacity-target-ordering-activation-simulation-v1` report. The binding
  exact-matches its digest, hard constraints, exact-target eligibility, quality
  floor, immutable baseline and order, selected target, selector revision, and
  resume target/revision to this request scope. It explicitly retains
  `manual_override_binding_resolved=false`.
- `gates` embeds a validated typed gate state and exactly two validated
  canonical gate decisions. The global and target tuples each contain the exact
  `{resource_key, decision_key, wake_key, status}` derived from that evidence.
  Arbitrary public-safe strings are not accepted.
- `reservation`, every predecessor event, feedback, optional recovery
  evidence, and retry budget use `{body, evidence_digest}` with
  `evidence_digest == stable_digest(body)`. SHA-256 values are exact lowercase
  64-hex digests.

Unknown fields, 0/1 boolean substitutions, booleans in integer fields,
duplicate/conflicting or broken lineage, stale or future source evidence,
revision/artifact drift, and malformed bindings reject the request.

Global gate evidence is evaluated first. Target gate evidence is considered
only when the global tuple is `allowed`, followed by selector and retry safety.
The reservation body binds mapping, policy, currentness, selector, and gate
digests plus task, attempt, target, resource, and policy. Its `expires_at` must
be the parsed-time earliest of authoritative reset, authoritative wake,
currentness expiry, and mapping/resource expiry; mixed UTC offsets are compared
as instants rather than strings.

Feedback retains `failure` and `unknown` as append-only observations and never
infers quota, quality, promotion, capacity, or routing. IDs and digests cannot
be reused across feedback and predecessor lineage. Recovery requires a fresh
source-attested evidence body binding observation/expiry, currentness
revision/digest, target/resource/scope, both global and target decision/wake
keys, predecessor event, and identity authority. At most one exact resource
could be a half-open candidate; while manual override remains unresolved, the
reported candidate list is always empty.

Retry safety is deliberately independent from provider quota and task attempt
limits. Its evidence exact-binds task/attempt, resume target/revision, and retry
policy revision, and proves cooldown inactive, dependencies satisfied,
resume/operator stops inactive, and task-attempt boundary preservation.
Version 1 requires `automatic_retries=0`; any unsafe state fails closed and
reports no retry.

The report embeds the normalized validated request, binds `input_digest`, and
its standalone validator reconstructs the exact report. It rejects a forged
replay even if the caller recomputes `replay_digest`. All mutation arrays are
separate and empty. Exact authority flags are `simulation_only=true`; and
`activation_authority`, `runtime_reservation`, `runtime_feedback_mutation`,
`automatic_half_open`, `automatic_retry`, `queue_mutation`, `config_mutation`,
`cooldown_mutation`, `wake_mutation`, `selection_mutation`,
`dispatch_authority`, `provider_call`, and `promotion_authority` are all
`false`.

Run it with:

```bash
cbr capacity-reservation-feedback-simulate --request-json request.json --json
```
