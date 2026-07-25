# Scoped readonly certification projection

`scoped-readonly-certification-policy-v1` is an additive, report-only advisory
policy for real `readonly-objective` executions. It does not change
`worker-certification-matrix-v1`, select or dispatch a worker, activate a
canary, promote a provider, or mutate routing, queue, cooldown, wake,
reservation, retry, or runner configuration.

Each counted sample exact-binds:

- a current non-synthetic `cbr-natural-execution-attestation-v1` objective run;
- its `cbr-execution-mutation-provenance-v1` record and all three worktree
  snapshots;
- the exact task, attempt, execution/review cohort, worker, target and target
  snapshot;
- the immutable `cbr-execution-delegation-contract-v1` and runner-owned
  `cbr-preexecution-delegation-receipt-v1` admitted and appended before the
  pre-worker snapshot. The contract must classify the task as
  `readonly-objective` and deny CBR-controlled repository writes, external
  mutation, credential access, deployment/publication, and destructive action.

Projection input is task-aware: it carries source IDs, not detached evidence
objects. The projector resolves exactly one matching natural, mutation, and
delegation receipt from the task-owned append histories and re-runs the
contract, receipt, natural attestation, mutation provenance, and canonical
phase-order validators. A caller-built timestamp or detached self-issued
authority is never accepted. A report must receive the original candidate and
sample bundle and deterministically rebuild the projection; a detached
self-digested projection is not reporting authority.

The repository/worktree evidence must be isolated and `no_mutation`, with no
pre-existing dirt, unsafe or unreported paths, worker-created commits,
resume/crash gaps, conflicts, missing snapshots, or schema/digest mismatch.
Synthetic boundary, provider-only, natural-boundary-only, manual, expired,
mixed, conflicting, missing, or malformed evidence does not satisfy the
sample floor.

Identical duplicate sample IDs are deduplicated. Distinct records for the same
task, attempt, and execution evidence are a conflict: the whole execution is
excluded and the advisory result is `disabled`, independent of input order.

One homogeneous cohort needs at least 20 non-expired objective samples, a pass
ratio of at least 95%, and zero adverse signals. The advisory result is
`insufficient`, `eligible-scoped-readonly`, or `disabled`. Cohort identity
includes worker/target/task-class mapping, resolved execution configuration,
execution and review revisions, attestor, mutation producer, delegation
contract/authority/policy/execution/review revisions, receipt producer, and
projection policy revision.

The verified scope is only `cbr-controlled-task-repository-worktree`.
External services, provider accounts, credential stores, arbitrary filesystem
locations, and network effects remain unknown. Every projection and report
therefore states:

```text
global_provenance=unknown
actual_canary=false
promotion_authority=false
routing_mutation_allowed=false
worker_selection_or_dispatch_allowed=false
queue_or_config_mutation_allowed=false
```

`cbr execution-report --purpose diagnostic|audit` exposes the report bundle in
`summary.scoped_readonly_certification`. The default routing report receives an
empty scoped bundle, so this advisory never changes routing comparability.
Each report retains the canonical public-safe worker, target-snapshot, and task
class candidate binding so an operator can attribute the advisory without
recovering private identity or execution details.

The 30-day period is report freshness for this advisory projection, not
activation authority. Actual readonly canary and promotion remain a separate
user-owned decision.
