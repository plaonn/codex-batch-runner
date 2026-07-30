# Worktree hibernation compatibility report

`cbr worktree hibernation-plan [TASK_ID] [--project PROJECT_ID] [--json]` is a
deterministic, read-only compatibility report. It reconciles canonical task
metadata with the local Git branch and worktree registry. It is the read-only
preflight surface used by the separately explicit hibernate/reattach commands.

The v1 report separates four questions:

- `branch_only_review`: whether an exact `execution_base_head..checkpoint`
  review unit can be reconstructed from the retained task branch without
  relying on the worktree directory.
- `hibernation`: whether the current task is a conservative hibernation
  candidate. The task must be completed, attached, clean, checkpointed, and
  covered by non-ambiguous scoped mutation provenance.
- `reattach`: compatible only for `execution_worktree_status=hibernated` tasks
  carrying the exact `worktree-hibernation-v1` intent record whose retained
  branch still equals the recorded checkpoint.
- `resume`: a `needs_resume` task is compatible only while its same retained
  worktree remains attached. A recreated cwd is not treated as resumable.
- `pool_lease`: validates an explicitly pooled task's lease independently from
  task result and worktree attachment state. Inconsistent lease metadata blocks
  hibernation compatibility but is not repaired by this report.

`reconciliation.status` distinguishes an attached current worktree, an
intentional current hibernation, a missing
path with a retained branch, a missing path with a missing branch, registry
mismatch, dirty or uncheckpointed state, and terminal cleanup. A missing path
is evidence of inconsistency, never evidence of intentional hibernation.

`cbr worktree hibernate TASK_ID --dry-run|--apply` is an explicit, single-task
mutation. Under the queue lock it repeats the compatibility gates, records a
recovery guard, then either releases the exact pooled lease or removes the
exact disposable worktree. It preserves the task branch, base, and checkpoint,
records `execution_worktree_status=hibernated`, and does not change result or
review disposition.

`cbr worktree reattach TASK_ID --dry-run|--apply` accepts only that intentional
hibernation record. It verifies the exact branch/base/checkpoint, refuses a
branch already checked out elsewhere, and for pooled tasks requires the same
tracked preparation-policy fingerprint. Apply creates a disposable attachment
or acquires a compatible pool slot, records the new path as `retained`, and
does not launch or resume a worker.

Both commands fail closed. A Git/pool failure after the recovery guard leaves
the task `recovery_required`; missing paths and legacy metadata never authorize
reattach. There is no bulk mode, automatic lifecycle hook, TTL action, GC,
branch deletion, migration, repair, or recreated-cwd resume path.

The JSON contract is `worktree-hibernation-plan-v1`. Its validator requires
canonical fields, stable ordering, known reason codes, a recomputed summary,
and a report digest. Repository identities are opaque digests; raw paths,
prompts, transcripts, session/thread ids, credentials, and account identities
are not emitted.

The `hibernation-plan` command itself has no `--apply` form. It does not create or remove worktrees,
change branches, release pool leases, edit task/event/config state, prune
branches, run GC, migrate metadata, or install lifecycle hooks.

See
[`examples/worktree-hibernation-plan-v1.example.json`](../examples/worktree-hibernation-plan-v1.example.json)
for a sanitized report.
