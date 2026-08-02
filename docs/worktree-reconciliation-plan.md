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
The preserved idle slot directory and Git registry entry are valid only when
their opaque path binding matches that exact observed slot and the task branch
has no registry entry. An observed lease must match the canonical task, branch,
slot, and policy.
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

## Exact single-task repair

`cbr worktree reconciliation-repair TASK_ID --approved-source-digest DIGEST
[--apply] [--json]` is the separate C2 mutation surface. It is dry-run by
default and always requires the exact C1 item `source_snapshot_digest`. Apply
acquires the canonical queue lock, confirms lock ownership, reloads the exact
task, rebuilds and validates the C1 item, recalculates its action immediately
before writing, and requires both the approved digest and full task-document
CAS to match.

Only an `exact_repair_candidate` can proceed. The only task change is
`execution_worktree_status=retained|recovery_required -> cleaned`; even
`updated_at` is preserved. The task JSON update uses same-directory atomic
replace, repair-local directory durability, and exact readback. The command
does not change result, review, resolution,
archive, cleanup receipts, branch-prune receipts, conflict-fix linkage, or any
other task field.

The repair records sanitized append-only `task_mutated` audit events with
subtype `worktree_reconciliation_exact_repair_v1`. They use one protected,
bounded JSONL-content `.audit` file in the configured event directory's
`worktree-reconciliation-repair-v1/` namespace, named with an opaque hash of
the task id; the raw task id remains only in the sanitized event envelope. The
exact task id deterministically locates this safety history for repair and
recovery validation. Ordinary event iteration, index, notifier cursor, prune,
retention inventory, and retention compaction exclude it: those date-event
lifecycle semantics do not own or delete repair recovery evidence. A
deterministic operation binds the approved C1 source digest and exact task
preimage/postimage digests.
The event directory and protected namespace are opened with no-follow
directory handles. Namespace and file device/inode bindings are rechecked
before append and after durability operations; symlink, non-directory,
hard-linked file, or replaced bindings fail closed before task mutation.
Platforms without the required directory-relative and no-follow primitives do
not use a path-based fallback.
`prepared` is written before the task CAS and `committed` after verified
readback. Append uses a single append handle, file fsync, and event-directory
fsync. A task-write failure leaves the source candidate unchanged and the
prepared operation retryable. A committed-event failure that leaves no event
bytes leaves the exact postimage automatically recoverable: retry verifies the
live terminal C1 classification and exact postimage, appends the missing
committed phase, and performs no second task write. A fully committed duplicate
is an idempotent no-op.

Safety reads of this audit file are strict and bounded. Unreadable/non-regular
files, malformed UTF-8/JSON, noncanonical envelope or payload fields,
conflicting operations, duplicate phases, and committed-before-prepared order
fail closed. Exact duplicate events are rejected rather than guessed benign.
A final line without its newline is a torn tail: the command performs no
automatic truncation or append and requires operator recovery to the last
verified newline. This includes a committed append that wrote only a partial
line: the task postimage remains intact, but automatic audit recovery is
blocked until the ambiguous tail is repaired deliberately. This preserves
append-only evidence.

Digest drift, a non-candidate live action, dirty/active/resumable/missing or
ambiguous evidence, pool conflict, task CAS drift, malformed audit history, or
lost queue-lock ownership fails closed before task mutation. Rejection does not
create an audit event when detected before preparation. Drift detected by the
mandatory post-prepare C1 rebuild leaves only the durable prepared audit, with
no task write or committed event. The command never creates/removes a worktree, changes a
branch or Git registry, leases/releases/resets a pool slot, runs cleanup,
GC/TTL, migration/backfill, review, resolution, archive, or worker actions.

The C1 command remains report-only and has no `--apply` mode. Its report does
not itself grant repair authority; C2 still requires the operator to supply
the separately approved exact digest.

On POSIX systems the repair fsyncs the written file and containing directory.
Python does not expose an equivalent portable directory-fsync guarantee on
Windows, so Windows power-loss durability remains unverified. The contract
claims process-visible atomic replacement plus retry/recovery ordering, not
universal power-loss atomicity.

See
[`examples/worktree-reconciliation-plan-v1.example.json`](../examples/worktree-reconciliation-plan-v1.example.json)
for a sanitized report.
