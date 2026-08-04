# Execution-target selector decision envelope

`execution-target-selector-decision-envelope-v1` is an independently versioned,
task/attempt-scoped, report-only binding between the canonical task selector input,
an authoritative `routing_override` observation (including explicit authoritative
absence), and one complete
`capacity-target-ordering-activation-simulation-v1` report. It does not change the
runtime selector, select for dispatch, persist task evidence, reserve capacity, or
call a provider.

The strict producer request is
`execution-target-selector-decision-envelope-request-v1`. It exact-binds:

- `task_id`, canonical task source revision, attempts before claim, and
  `attempt = task_attempts_before_claim + 1`;
- project, repository, task class, and opt-in scope identity;
- requirement revision, inventory snapshot, selector policy revision, and a
  deterministic selector-input digest;
- the canonical task-source projection and its digest. `status=present` requires
  the complete value accepted by the existing `routing_override_value` validator.
  `status=authoritative_absence` requires an explicit JSON `null`; missing fields,
  `{}`, or a consumer assertion are not absence evidence;
- the fixed selector producer id/revision, task source revision, source-attested
  observation/expiry interval, source-projection digest, and currentness digest;
- the complete standalone-validated ordering-v1 report, its exact digest, immutable
  baseline decision digest, selected automatic baseline target, and ordered eligible
  target ids.

The deterministic dispositions are:

| Source state | Disposition | Report-only target |
| --- | --- | --- |
| authoritative absence | `authoritative_absence` | ordering-v1 counterfactual target |
| eligible preference | `operator_preference` | exact override target |
| eligible pin | `operator_pin` | exact override target |
| unavailable preference with fallback | `operator_preference_fallback` | immutable automatic baseline target |
| unavailable pin, disabled/exhausted fallback, or invalid evidence | `fail_closed` | none |

Every non-null target must already occur in the exact ordering-v1 baseline order.
An override never admits, revives, or reorders another target. Presence of any valid
override skips capacity ordering, including fallback: fallback uses the immutable
automatic selector baseline, not the capacity counterfactual.

## Sanitized request example

The following is the public-safe outer shape. `baseline_report` must be the complete
validated report; an ellipsis is shown only to keep this documentation readable and
is not accepted by the CLI.

```json
{
  "schema_version": 1,
  "contract": "execution-target-selector-decision-envelope-request-v1",
  "evaluated_at": "2030-01-02T04:00:00+00:00",
  "task": {
    "task_id": "task-example",
    "canonical_task_source_revision": "task-source-r1",
    "task_attempts_before_claim": 0,
    "attempt": 1
  },
  "scope": {
    "project_id": "project-example",
    "repository_id": "repository-example",
    "task_class": "implementation",
    "opt_in_scope_id": "scope-example"
  },
  "selector_inputs": {
    "requirement_revision": "requirement-r1",
    "inventory_snapshot_id": "inventory-r1",
    "selector_policy_revision": "execution-target-selector-v1",
    "selector_input_digest": "sha256:<64 lowercase hex>"
  },
  "manual_override_source": {
    "status": "authoritative_absence",
    "producer_id": "execution-target-selector",
    "producer_revision": "execution-target-selector-decision-envelope-producer-v1",
    "source_revision": "task-source-r1",
    "source_projection": {
      "task_id": "task-example",
      "canonical_task_source_revision": "task-source-r1",
      "routing_override": null
    },
    "source_projection_digest": "sha256:<64 lowercase hex>"
  },
  "currentness": {
    "producer_id": "execution-target-selector",
    "producer_revision": "execution-target-selector-decision-envelope-producer-v1",
    "source_revision": "task-source-r1",
    "identity_authority": "source_attested",
    "observed_at": "2030-01-02T03:59:00+00:00",
    "expires_at": "2030-01-02T04:05:00+00:00",
    "source_projection_digest": "sha256:<64 lowercase hex>",
    "currentness_digest": "sha256:<64 lowercase hex>"
  },
  "baseline_report": { "contract": "capacity-target-ordering-activation-simulation-v1", "...": "complete report required" }
}
```

Build and standalone-validate an envelope with:

```bash
cbr execution-target-selector-decision-envelope --request-json request.json --json
```

The command rejects `--config`, reads no runner state, and writes nothing. A JSON
object merely claiming the fixed producer labels does not gain selector authority;
the envelope is consumable only at the bounded producer/consumer boundary described
here. The standalone validator replays the embedded producer request and recomputes
the disposition, target, input/currentness/baseline bindings, and artifact digest.
Unknown keys, primitive-type aliases, stale evidence, task/attempt/scope/revision
drift, conflicting artifacts, and forged replay outputs are rejected.

`report_only=true` and `simulation_only=true`. Activation, selection, dispatch,
reservation, feedback mutation, automatic half-open/retry, provider call, and
promotion authority are all false. Reservation, retry, queue, config, cooldown,
wake, selection, dispatch, and routing mutation arrays are independently empty.
