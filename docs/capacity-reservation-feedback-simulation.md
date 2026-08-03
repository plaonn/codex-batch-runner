# Capacity reservation and feedback simulation

`capacity-reservation-feedback-simulation-v1` is a deterministic, public-safe,
report-only contract. It previews an exact-bound reservation, append-only
observational feedback, half-open eligibility, and a retry budget without
reserving capacity, calling a provider, or changing a task.

The request exact-binds opted-in project/repository/task class/task/attempt/
target, canonical resource key, mapping/currentness/policy/selector/resume
revisions, immutable selector baseline digest, global admission keys, replay
clock, ordered predecessor events, reservation evidence digest, and feedback.
Unknown fields, duplicate/conflicting or broken lineage, stale/ambiguous
currentness, revision drift, and malformed values reject or fail closed.

Global admission and selector eligibility are evaluated before reservation.
Reservation expiry must be the earliest authoritative wake, resource
currentness, or source-currentness boundary; reaching it is revalidation only.
Feedback retains `failure` and `unknown` as append-only observations and never
infers quota, quality, promotion, capacity, or routing. A half-open preview is
possible only for fresh exact-bound recovery and contains at most one candidate
for the exact canonical resource key.

Retry budget is deliberately independent from provider quota and task attempt
limits. Version 1 always reports `automatic_retries=0`; it cannot weaken
cooldowns, dependencies, resume bindings, or operator stops.

The report embeds the validated request, binds `input_digest`, and its
standalone validator reconstructs the exact report. All mutation arrays are
empty and `simulation_only=true`; activation, runtime mutation, automatic
retry, provider calls, routing, and promotion are false.

Run it with:

```bash
cbr capacity-reservation-feedback-simulate --request-json request.json --json
```
