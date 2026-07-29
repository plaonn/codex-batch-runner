# Report-only retention inventory

`cbr retention-inventory` is a deterministic, sanitized projection over current
canonical task JSON, referenced runtime logs, and event files. It classifies
artifacts as `Hot`, `Warm`, or `Cold-candidate`, reports stable blocker reason
codes, and previews compact/tombstone and restore semantics.

This command is Package 1 only. It has no apply mode and does not write a
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
It does not expand what `cbr prune` may delete. Additive compact records and
physical TTL/deletion remain separate, unimplemented packages requiring new
approval.
