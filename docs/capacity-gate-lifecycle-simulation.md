# Capacity gate-lifecycle activation simulation

`capacity-gate-lifecycle-activation-simulation-v1` is a deterministic,
report-only replay contract for evaluating provider-resource gate lifecycle
mechanics without changing runtime state.

## Bound inputs

The request embeds and validates:

- an explicitly opted-in project, repository, task class, target, and exact
  provider-resource tuple;
- an immutable `provider-resource-gate-state-v1`, its digest, and the
  append-only typed decision evidence that backs every active gate;
- current `provider-resource-mapping-v2` and
  `provider-resource-admission-policy-v1` artifacts;
- exact mapping, admission-policy, currentness, and lifecycle-policy
  revisions;
- the standalone-validated
  `capacity-target-ordering-activation-simulation-v1` report and digest, which
  exact-bind the existing selector hard constraints, eligible target order,
  quality floor, immutable baseline target, and resume target;
- ordered global-gate observations and a predecessor-bound typed event
  sequence; and
- an explicit replay clock, admission-policy reset grace, and rollback rule.

The standalone report validator embeds the canonical validated request and
replays it. `input_digest` and `replay_digest` bind the complete request and
report. Unknown fields, malformed literals, conflicting decision duplicates,
and forged reports are rejected.

## Replay precedence

Each event uses the latest global-gate observation at or before its event time.
The simulator applies this order:

1. global gate;
2. source-attested identity, mapping, and currentness;
3. canonical resource, decision, and wake keys plus the one-active-gate rule;
4. selector eligibility and resume binding; and
5. lifecycle counterfactual.

A covering global reset produces `covered_by_global` and no target wake
preview. An unknown or terminal non-covering global gate fails closed before
target-scoped capacity evaluation.

## Lifecycle previews

The report uses one of these preview values:

- `no_change`;
- `would_defer`;
- `covered_by_global`;
- `would_supersede_gate`;
- `would_revalidate_wake`;
- `would_release`;
- `would_hard_exclude`; or
- `fail_closed`.

Threshold-at-or-below evidence can only preview a defer. It cannot preview hard
exclusion. A later reset may replace the one active gate only when the typed
decision names the exact predecessor and produces new canonical decision and
wake keys. Equal or older resets fail closed, while an identical decision is
an idempotent no-op.

Wake time is a revalidation boundary, not release authority. Before
`reset_at + reset_grace_seconds`, the gate remains active. At or after that
boundary, fresh exact-bound recovery evidence may preview release. Continued
low-resource evidence must create a later predecessor-bound gate preview.

## Confirmed-exhaustion boundary

Version 1 has no trusted natural or external confirmed-exhaustion source.
Non-synthetic confirmed-exhaustion input is rejected. Deterministic synthetic
fixtures may exercise hard-exclusion mechanics, but every report keeps:

```text
hard_exclusion_authority=false
natural_evidence_authority=false
```

Synthetic evidence does not authorize certification, promotion, a live
canary, or runtime exclusion.

## Rollback and non-authority

Rollback stops new typed evaluation while preserving the immutable baseline,
append-only evidence, and the legacy scalar's `global_gate_only` role. It never
projects a target gate into that scalar.

Every report also states:

```text
simulation_only=true
activation_authority=false
runtime_gate_mutation=false
automatic_defer=false
automatic_wake=false
live_routing=false
default_routing=false
worker_promotion=false
provider_promotion=false
```

Queue, config, cooldown, wake, defer, hard-exclusion, selection, dispatch,
routing, reservation, and retry mutation arrays are always empty. The
counterfactual gate state, task disposition, and wake-registry fields are
separate preview data and are never persisted or scheduled.
