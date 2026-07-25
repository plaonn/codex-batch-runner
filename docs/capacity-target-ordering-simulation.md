# Capacity target-ordering activation simulation

`capacity-target-ordering-activation-simulation-v1` is a deterministic,
report-only handoff for evaluating how complete capacity evidence would reorder
an already-eligible exact-target set. It does not replace or call the runtime
selector.

## Safety precedence

The simulator accepts only an exact
`capacity-shadow-evaluation-request-v1` and its deterministically reproduced
`capacity-shadow-evaluation-report-v1`. The request binds:

- an explicitly opted-in public-safe task class, project, repository, and scope;
- requirement, inventory, selector, mapping, authority, capacity-bundle,
  currentness, and simulation-policy revisions;
- the immutable baseline decision digest, selected target, and ordered eligible
  target set;
- the shadow request digest and report digest; and
- the fixed rollback rule
  `keep-immutable-baseline-on-any-ineligible-input-v1`.

Baseline hard constraints, exact-target eligibility, quality floor, and
resume-target pinning precede capacity. Capacity never adds a target to the
eligible set. A non-trivial counterfactual can only move one existing eligible
target to the front while retaining every other target in baseline order.

## Decisions

The output decision is one of:

- `keep_baseline`: complete evidence agrees with the baseline, or the exact
  baseline target is pinned for resume;
- `would_select_alternative`: complete comparable evidence would move another
  already-eligible target to the front; or
- `fail_closed`: a global gate fails, a resume binding conflicts, or the
  immutable shadow report falls back because evidence is unknown, stale,
  missing, ambiguous, conflicting, revision-drifted, incomparable,
  untrusted, or otherwise ineligible.

Malformed requests, forged reports, changed digests, ineligible target
injection, or unknown fields are rejected by strict validators. Rejection is
not an alternative recommendation.

The report preserves the baseline object and order separately from the
counterfactual target and order. It embeds the canonical validated request, and
the standalone report validator replays that request before accepting the
decision. Stable `input_digest` and `simulation_digest` values bind the replay.

## Non-authority boundary

Every report states:

```text
simulation_only=true
activation_authority=false
live_routing=false
default_routing=false
automatic_substitution=false
selection_or_dispatch_authority=false
worker_promotion=false
provider_promotion=false
actual_canary=false
synthetic_evidence_authority=false
```

Queue, config, reservation, cooldown, wake, defer, hard-exclusion, retry,
selection, dispatch, and routing mutation arrays are always empty. The module
does not call a provider, acquire credentials, read private runtime state,
mutate the queue or configuration, deploy, publish, or activate Branch 1B
routing.

Synthetic fixtures test mechanics only. They are not natural execution
evidence and cannot authorize certification, promotion, or a live canary.
