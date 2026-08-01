# Retention inventory and additive compact records

`cbr retention-inventory` is a deterministic, sanitized projection over current
canonical task JSON, referenced runtime logs, and event files. It classifies
artifacts as `Hot`, `Warm`, or `Cold-candidate`, reports stable blocker reason
codes, and previews compact/tombstone and restore semantics.

The inventory command is Package 1 only. It has no apply mode and does not write a
tombstone, compact record, restore index, task, event, config, queue state, or
runtime directory. It does not delete or move any artifact. Canonical task JSON
is always reported as `protected` and is never deletion-eligible.

## Usage

```console
cbr retention-inventory
cbr retention-inventory --proposal-age-days 60
cbr retention-inventory --proposal-age-days 60 --project example-project --json
cbr retention-inventory --proposal-age-days 60 \
  --notifier-cursor-state path/to/notify-state.json --json
```

An omitted `--proposal-age-days` means no artifact becomes age-eligible. A value
such as 60 is an explicit projection input, not an adopted TTL, scheduled
default, or deletion authority.

The JSON contract is `retention-inventory-report-v1`. It includes:

- a `report-only` mutation boundary;
- sanitized aggregate counts and stable lifecycle classes;
- per-task raw-log eligibility and blocker reason codes;
- canonical task protection and a source digest;
- a non-writing tombstone candidate with its own digest;
- restore-capability claims that never promise reconstruction of deleted raw
  artifacts;
- cursor-safe event candidates; and
- a digest over the complete report.

A sanitized empty-runtime fixture is available at
[`retention-inventory-report-v1.example.json`](../examples/retention-inventory-report-v1.example.json).

Artifact references are hashes. Absolute runtime paths, raw prompts,
transcripts, stdout/stderr, commands, environment values, session/thread/account
identities, and credentials are not projected.

## Fail-closed boundaries

Active/running work, resume or user decisions, pending review,
accepted-but-unapplied worktrees, recovery-required work, unresolved failures,
missing or invalid timestamps, unsupported terminal states, absent age input,
recent artifacts, cursor uncertainty, and artifacts outside configured roots
remain retained.

The report reuses the current `prune` terminal-state and cursor-safety owners.
It does not expand what `cbr prune` may delete. Physical TTL/deletion remains a
separate, unimplemented package requiring new approval.

## Additive compact apply

`cbr retention compact` is a separate Package 2 command. It consumes a fresh,
unfiltered `retention-inventory-report-v1` JSON file for exactly one task. The
default is dry-run:

```console
cbr retention-inventory --proposal-age-days 60 --json > inventory.json
cbr retention compact --inventory-report inventory.json --task-id TASK_ID --json
cbr retention compact --inventory-report inventory.json --task-id TASK_ID \
  --apply --confirm-operation-id RETENTION_OPERATION_ID --json
```

Apply requires both `--apply` and the exact operation id emitted by dry-run.
It acquires the canonical queue lock, revalidates the report digest, freshness,
all-project scope, proposal input, task/event/cursor source scope, selected task
digest, and live terminal eligibility immediately before writing. Project-filtered
reports, cursor uncertainty, malformed unrelated task/event sources, active or
resumable work, unresolved review/failure/fix chains, unapplied or unclean
worktrees, unreleased pool leases, and recovery-required metadata fail closed.

The command writes only sanitized additive records in a retention directory
beside (not inside) the configured queue directory:

- an immutable compact bundle containing a logical tombstone;
- a lookup-only restore index entry; and
- a deterministic transaction journal.

The durable order is bundle, prepared journal, restore index, then committed
journal marker. A retry recovers bundle-only or prepared partial writes and the
stable operation identity prevents duplicates for the same bound snapshot.
Every file is atomically published or replaced. A restore index never points to
a missing or unvalidated bundle.

The tombstone records that source artifacts are unchanged. The restore index
does not implement restore and explicitly marks raw log/transcript restore as
unsupported. This command never deletes, moves, rewrites, or cold-stores a
canonical task, raw log, transcript, or event file. It does not adopt a TTL,
scheduler, background GC, external storage, or credential/provider contract.
