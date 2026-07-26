# Source-goal reconciliation

`source-goal-manifest-v1` is a public-safe desired-state contract owned by a
source project or its explicit adapter. `goal-reconciliation-report-v1` is a
deterministic, read-only CBR projection. Neither makes the queue a planning
source or grants completion authority.

```bash
cbr orchestration goal-reconcile \
  --goal-manifest examples/source-goal-manifest-v1.example.json --json
```

An optional `--evidence PATH` accepts only `goal-reconciliation-evidence-v1`,
whose node records contain an already validated CBR
`orchestration-selection-funnel-projection-v1`. The evidence and manifest must
have the exact same `(goal_id, revision, manifest_digest)`. Every evidence node
also carries `executable_contract_digest`, which must exactly equal both its
manifest node digest and the funnel `source_contract_digest`; a funnel for a
different executable contract fails closed. Without trusted CBR evidence, all
lifecycle axes other than contract binding are `unknown`.

## Contract rules

- The manifest has an opaque source identity, positive monotonic revision,
  canonical digest, stable node IDs, authority, dependencies, required outcome,
  verification references, terminal references, and a read-only automation
  boundary. Unknown fields, malformed digests, dangling/cyclic dependencies,
  and privacy-sensitive keys or values are rejected.
- A new revision identifies its exact predecessor in `supersedes`. The
  validation helper rejects an in-place executable/authority/dependency rewrite
  or removal for a node already observed as admitted; use a new node generation
  instead.
- CBR funnel evidence may populate only selection, admission, execution, review,
  apply, and attention-recorded. Delivery, acknowledgement, parent collection,
  and source disposition stay independent and are never inferred from it.
- Axis values are `observed`, `not_observed`, `unknown`, or `not_applicable`.
  Only trusted CBR evidence may be observed. A non-CBR adapter is unknown.

`terminal_candidate` is deliberately advisory and currently always `false` in
Package 1. Dispatch, review, apply, delivery, acknowledgement, collection,
source disposition, and root completion authority claims are all `false`.

## Safety and privacy

The command loads no CBR config, lock, queue, runtime state, source adapter, or
network client. It writes no queue/config/runtime/source file, does not scan for
goals, and never dispatches, delivers, acknowledges, applies, or completes.
Its stable report digest is calculated from canonical semantic output.

Public fixtures and output exclude prompts, transcripts, credentials, account,
user/session/thread identifiers, commands, and private paths.
Absolute paths (including `/tmp/...`) and secret-looking assignments such as
`api_key=...` are rejected from all textual values. Relative or opaque
references such as `tests-v1` and `acceptance-v1` remain valid.
