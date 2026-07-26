# Bounded capacity target-ordering canary mechanism

`capacity-target-ordering-canary-policy-v1` is a default-disabled, explicitly
scoped claim-time mechanism. It may move one exact target to the front only
after the unified selector has already applied hard constraints, trust,
quality-floor ordering, manual override, and resume pinning.

The mechanism consumes an exact
`capacity-target-ordering-activation-simulation-v1` report. The report must bind
the current requirement revision, inventory snapshot, selector policy, baseline
eligible order, provider-resource mapping, and a source-attested mapping that
is still current at the runner-owned dispatch evaluation time. Caller-provided
timestamps cannot establish freshness. Invalid, stale, missing, ambiguous, or
out-of-scope input preserves the baseline.

## Operator policy

The optional `capacity_target_ordering_canary_policy` config object has this
strict shape:

```json
{
  "schema_version": 1,
  "contract": "capacity-target-ordering-canary-policy-v1",
  "revision": "capacity-target-ordering-canary-policy-v1",
  "enabled": false,
  "assignment_percent": 5,
  "hard_ceiling_percent": 10,
  "kill_switch_active": true,
  "max_evidence_age_seconds": 300,
  "allowed_scopes": [
    {
      "project_id": "public-project",
      "repository_id": "public-repository",
      "task_class": "bounded-readonly-objective"
    }
  ]
}
```

Omission is equivalent to disabled with the kill switch active. An enabled
initial policy requires exactly 5 percent assignment and at least one exact
scope. The hard ceiling is fixed at 10 percent. Assignment is deterministic
from a control-plane-issued immutable task nonce plus scope and policy
identifiers; caller-selected task ids and request fields are not cohort seeds.

## Claim and evidence boundary

The runner evaluates the request once under the canonical queue lock before it
claims the task. The selector only reads and revalidates the resulting exact
decision; it never appends evidence. Decision and outcome histories are
append-only. They bind task, attempt, scope, selector revisions, immutable
baseline order, counterfactual order, exact dispatch target, outcome, and
rollback reason.

Exact scope uses canonical queue metadata: `project_id`, the basename of
`project_root` as the repository id, and `category` as the task class.

Manual routing overrides and resume attempts precede capacity and are never
reordered. A missing terminal outcome, adverse outcome, eligibility drift, or
kill switch causes deterministic baseline-only reconstruction and stops new
canary decisions.

This mechanism does not authorize global or default routing, provider priority,
queue policy mutation, worker/provider promotion, provider calls, deployment,
or publication. Shipping the mechanism does not activate a natural canary.
