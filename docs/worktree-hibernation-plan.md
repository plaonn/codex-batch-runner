# Worktree hibernation compatibility report

`cbr worktree hibernation-plan [TASK_ID] [--project PROJECT_ID] [--json]` is a
deterministic, read-only compatibility report. It reconciles canonical task
metadata with the local Git branch and worktree registry before any future
hibernation design is considered.

The v1 report separates four questions:

- `branch_only_review`: whether an exact `execution_base_head..checkpoint`
  review unit can be reconstructed from the retained task branch without
  relying on the worktree directory.
- `hibernation`: whether the current task is a conservative future hibernation
  candidate. The task must be completed, attached, clean, checkpointed, and
  covered by non-ambiguous scoped mutation provenance.
- `reattach`: always incompatible in v1. Intentional hibernation metadata and
  reattachment mutation belong to a later, separately approved contract.
- `resume`: a `needs_resume` task is compatible only while its same retained
  worktree remains attached. A recreated cwd is not treated as resumable.
- `pool_lease`: validates an explicitly pooled task's lease independently from
  task result and worktree attachment state. Inconsistent lease metadata blocks
  hibernation compatibility but is not repaired by this report.

`reconciliation.status` distinguishes an attached current worktree, a missing
path with a retained branch, a missing path with a missing branch, registry
mismatch, dirty or uncheckpointed state, and terminal cleanup. A missing path
is evidence of inconsistency, never evidence of intentional hibernation.

The JSON contract is `worktree-hibernation-plan-v1`. Its validator requires
canonical fields, stable ordering, known reason codes, a recomputed summary,
and a report digest. Repository identities are opaque digests; raw paths,
prompts, transcripts, session/thread ids, credentials, and account identities
are not emitted.

The command has no `--apply` form. It does not create or remove worktrees,
change branches, release pool leases, edit task/event/config state, prune
branches, run GC, migrate metadata, or install lifecycle hooks.

See
[`examples/worktree-hibernation-plan-v1.example.json`](../examples/worktree-hibernation-plan-v1.example.json)
for a sanitized report.
