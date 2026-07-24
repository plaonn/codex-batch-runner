# Scoped execution mutation provenance

`cbr-execution-mutation-provenance-v1` is additive, append-only, report-only
evidence for the repository/worktree root that CBR assigned to one task attempt.
It does not authorize a run and does not change queue admission, worker
selection, retry, routing, review, apply, promotion, or canary policy.

For isolated task worktrees the runner records three sanitized observations:

1. after worktree preparation and before worker invocation;
2. immediately after worker return and before any CBR-created review commit;
3. after execution-result finalization.

The observations bind the task, attempt, execution evidence, repository and
worktree identity digests, Git head/status digests, dirt counts, reported-file
coverage, terminal task/review-state digest, timestamps, and producer revision.
Raw paths, prompts, transcripts, commands, credentials, private identities, and
session/thread identifiers are excluded.

The record separates worker-observed changes, CBR-created commit/state changes,
pre-existing dirt, retained recovery state, unsafe or unreported files, and
worker-created commits. Missing or conflicting snapshots, non-isolated roots,
ambiguous repository identity, resume/crash gaps, and digest/revision mismatch
fail closed to `unknown` or `mutation_possible`.

The verified scope is deliberately narrow. External services, provider
accounts, credential stores, arbitrary filesystem locations, and network side
effects remain globally `unknown`. A natural execution attestation can bind the
scoped record while retaining `evidence.mutation_provenance=unknown`; therefore
the binding is visible in reports but cannot enter worker certification,
promotion, canary, or live/default routing.

Historical executions without this evidence remain valid historical reports
with legacy unknown provenance. CBR does not synthesize or destructively
backfill missing proof.
