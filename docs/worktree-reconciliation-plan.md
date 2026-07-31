# Legacy worktree reconciliation plan

`cbr worktree reconciliation-plan [TASK_ID] [--project PROJECT_ID] [--json]`
produces a deterministic, sanitized, report-only action plan for current and
grandfathered worktree metadata. It reads canonical task metadata, local
branch/head state, the Git worktree registry, and the existing hibernation and
cleanup classifications. It does not acquire the queue lock or change any
state.

Each item has exactly one stable action class:

- `no_action`: the attachment is current, intentionally hibernated, terminally
  cleaned, or not a task worktree.
- `manual_review`: evidence is stale, active/resumable,
  dirty/uncheckpointed, provenance-incomplete, or otherwise insufficient for
  an exact repair.
- `exact_repair_candidate`: the only inconsistency is
  `execution_worktree_status=retained|recovery_required` after an independently
  proven accepted+applied terminal cleanup receipt. The branch still equals the
  recorded checkpoint, the recorded base resolves and is an ancestor of that
  checkpoint, `execution_applied_at` is present, the apply target contains the
  exact applied head, the Git registry confirms the path is absent, and
  mutation provenance is complete. A pooled row additionally requires the
  canonical slot/policy binding and released lease receipt; an active,
  missing, or conflicting lease blocks the candidate.
- `unrecoverable_without_owner_decision`: repository, registry, branch,
  base/checkpoint, or registry/path identity evidence is missing or
  contradictory.

An exact candidate projects only this enum-level delta:

```json
{
  "field": "execution_worktree_status",
  "before": "retained",
  "after": "cleaned"
}
```

The projection is not repair authority. The schema always reports
`repair_authority_granted=false`, `repair_supported=false`, and
`mutation_performed=false`. There is no `--apply` form.

The `source_snapshot` contains allowlisted enum values, observed booleans,
resolved base/checkpoint/branch/apply-target commits, and opaque digests for
repository, branch, registry paths, resolution/follow-up state, timestamps,
cleanup receipt, branch-prune receipt, conflict-fix apply linkage, pool
slot/policy/lease evidence, and mutation-provenance history. Its digest binds
the source facts used by the action classifier; a separate report digest binds
ordering, summary, authority boundary, and all items. The strict offline
validator cannot re-run Git. It enforces internal relationships among the bound
observations, then recomputes reconciliation, canonical cleanup eligibility,
provenance completeness, apply containment, pool consistency, action, reasons,
and delta. A re-digested change to a derived claim is rejected when it does not
match those source facts.
Absolute paths, raw branch names, prompts, transcripts, session/thread ids,
credentials, account identity, and arbitrary task values are not emitted.

A missing path never means `hibernated`, `cleaned`, accepted, applied,
rejected, resolved, archived, or deletion-eligible. Grandfathered rows are
marked and reported only. `cleaned` is current only with a matching terminal
cleanup receipt; `hibernated` is current only with the exact hibernation
contract, kind, base, checkpoint, timestamp, branch, and registry evidence.
Pooled cleanup/hibernation also requires `execution_worktree_lease_status` to
be `released` with slot, policy fingerprint, and released-at evidence, plus an
exact matching idle slot in the local pool state with its release timestamp.
An observed lease must match the canonical task, branch, slot, and policy.
Non-pooled rows carrying injected pool metadata are inconsistent.

Terminal cleanup remains owned by the existing cleanup/apply/branch-prune
lifecycle. A valid cleaned row may retain its branch, or may carry the exact
`pruned` status/head/time/reason receipt with the branch absent. A parent
applied through a linked conflict-fix may bind an applied head different from
the parent checkpoint when the apply-via task, conflict-fix task/status/queued
time, accepted chain state, and current apply-target containment all agree.
These legitimate terminal forms are `no_action`; malformed receipts remain
manual review.

The command does not create/remove worktrees, alter
branches, release/reset pool slots, mutate tasks/events/configuration, run
cleanup/GC/TTL/retention/archive logic, migrate/backfill metadata, start a
worker, install a hook, or grant future repair authority.

Any future C2 repair must acquire the queue lock, rebuild this exact source
snapshot, require the approved digest to match, and revalidate every live fact
before mutation. This C1 report does not implement or authorize that step.

See
[`examples/worktree-reconciliation-plan-v1.example.json`](../examples/worktree-reconciliation-plan-v1.example.json)
for a sanitized report.
