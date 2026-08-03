"""Deterministic, non-mutating capacity reservation/feedback previews."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime
from typing import Any

from .provider_resource_authority import (
    resource_gate_key,
    validate_admission_policy,
    validate_mapping_v2,
)
from .provider_resource_report import ProviderResourceValidationError

REQUEST_CONTRACT = "capacity-reservation-feedback-simulation-request-v1"
REPORT_CONTRACT = "capacity-reservation-feedback-simulation-v1"
POLICY_REVISION = "capacity-reservation-feedback-simulation-policy-v1"
MANUAL_OVERRIDE_BINDING_GAP = "manual_override_binding_not_expressed_by_selector_report"
MUTATION_FIELDS = (
    "queue_mutations",
    "config_mutations",
    "cooldown_mutations",
    "wake_mutations",
    "selection_mutations",
    "dispatch_mutations",
    "routing_mutations",
    "reservation_mutations",
    "feedback_mutations",
    "retry_mutations",
)
SAFE = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:+-")


class CapacityReservationFeedbackSimulationError(ValueError):
    pass


def stable_digest(value: object) -> str:
    try:
        raw = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise CapacityReservationFeedbackSimulationError(
            "value is not stable-digest serializable"
        ) from exc
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()


def validate_capacity_reservation_feedback_simulation_request(
    value: object,
) -> dict[str, Any]:
    r = _obj("request", value)
    _keys(
        "request",
        r,
        {
            "schema_version",
            "contract",
            "scope",
            "revisions",
            "currentness",
            "selector_binding",
            "global_admission",
            "mapping",
            "admission_policy",
            "resource",
            "replay",
            "predecessor_events",
            "reservation",
            "feedback",
            "retry_budget",
        },
    )
    _int_literal("request.schema_version", r.get("schema_version"), 1)
    _lit("request.contract", r.get("contract"), REQUEST_CONTRACT)
    scope = _scope(r.get("scope"))
    revisions = _revisions(r.get("revisions"))
    currentness = _currentness(r.get("currentness"), revisions)
    try:
        mapping = validate_mapping_v2(r.get("mapping"))
        admission_policy = validate_admission_policy(r.get("admission_policy"))
    except ProviderResourceValidationError as exc:
        raise CapacityReservationFeedbackSimulationError(
            "mapping or admission policy invalid"
        ) from exc
    _lit(
        "request.revisions.mapping_revision",
        revisions["mapping_revision"],
        mapping["mapping_revision"],
    )
    _lit(
        "request.revisions.policy_revision",
        revisions["policy_revision"],
        admission_policy["policy_revision"],
    )
    selector = _selector(r.get("selector_binding"), scope, revisions)
    global_admission = _global(r.get("global_admission"))
    resource = _resource(r.get("resource"), revisions, mapping, admission_policy, scope)
    replay = _replay(r.get("replay"))
    events = _events(r.get("predecessor_events"), replay["evaluated_at"])
    reservation = _reservation(
        r.get("reservation"), scope, resource, revisions, currentness
    )
    feedback = _feedback(r.get("feedback"), scope, resource, revisions, events)
    retry = _retry(r.get("retry_budget"), scope)
    return {
        "schema_version": 1,
        "contract": REQUEST_CONTRACT,
        "scope": scope,
        "revisions": revisions,
        "currentness": currentness,
        "mapping": deepcopy(mapping),
        "admission_policy": deepcopy(admission_policy),
        "selector_binding": selector,
        "global_admission": global_admission,
        "resource": resource,
        "replay": replay,
        "predecessor_events": events,
        "reservation": reservation,
        "feedback": feedback,
        "retry_budget": retry,
    }


def simulate_capacity_reservation_feedback(value: object) -> dict[str, Any]:
    return validate_capacity_reservation_feedback_simulation_report(
        _build(validate_capacity_reservation_feedback_simulation_request(value))
    )


def validate_capacity_reservation_feedback_simulation_report(
    value: object,
) -> dict[str, Any]:
    report = _obj("report", value)
    required = {
        "schema_version",
        "contract",
        "evaluated_at",
        "scope",
        "preview",
        "reason_codes",
        "reservation_preview",
        "feedback_preview",
        "half_open_preview",
        "retry_preview",
        "event_results",
        "simulation_request",
        "input_digest",
        "replay_digest",
        "simulation_only",
        "activation_authority",
        "runtime_reservation",
        "runtime_feedback_mutation",
        "automatic_half_open",
        "automatic_retry",
        "queue_mutation",
        "config_mutation",
        "cooldown_mutation",
        "wake_mutation",
        "selection_mutation",
        "dispatch_authority",
        "provider_call",
        "promotion_authority",
        "live_routing",
        "default_routing",
        "worker_promotion",
        "provider_promotion",
        "manual_override_binding_resolved",
        *MUTATION_FIELDS,
    }
    _keys("report", report, required)
    _int_literal("report.schema_version", report.get("schema_version"), 1)
    _lit("report.contract", report.get("contract"), REPORT_CONTRACT)
    request = validate_capacity_reservation_feedback_simulation_request(
        report.get("simulation_request")
    )
    _digest("report.input_digest", report.get("input_digest"))
    _digest("report.replay_digest", report.get("replay_digest"))
    for f in (
        "simulation_only",
        "activation_authority",
        "runtime_reservation",
        "runtime_feedback_mutation",
        "automatic_half_open",
        "automatic_retry",
        "queue_mutation",
        "config_mutation",
        "cooldown_mutation",
        "wake_mutation",
        "selection_mutation",
        "dispatch_authority",
        "provider_call",
        "promotion_authority",
        "live_routing",
        "default_routing",
        "worker_promotion",
        "provider_promotion",
    ):
        _lit("report." + f, report.get(f), f == "simulation_only")
    for f in MUTATION_FIELDS:
        _lit("report." + f, report.get(f), [])
    _lit(
        "report.manual_override_binding_resolved",
        report.get("manual_override_binding_resolved"),
        False,
    )
    if report != _build(request):
        raise CapacityReservationFeedbackSimulationError(
            "report must exactly match deterministic simulation request replay"
        )
    return deepcopy(report)


def _build(r: dict[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    preview = "would_reserve"
    reservation = deepcopy(r["reservation"])
    feedback = deepcopy(r["feedback"])
    # The validated upstream selector report has no manual-override artifact.
    # Do not manufacture one locally: this branch remains fail-closed.
    if True:
        reasons.append(MANUAL_OVERRIDE_BINDING_GAP)
    elif r["global_admission"]["status"] != "allowed":
        reasons.append("global_admission_" + r["global_admission"]["status"])
    elif r["selector_binding"]["status"] != "eligible":
        reasons.append("selector_" + r["selector_binding"]["status"])
    elif r["currentness"]["status"] != "current" or not _in_window(
        r["replay"]["evaluated_at"], r["currentness"]
    ):
        reasons.append("currentness_not_current")
    elif r["resource"]["mapping_status"] != "exact":
        reasons.append("resource_mapping_not_exact")
    elif not _in_window(r["replay"]["evaluated_at"], r["resource"]):
        reasons.append("resource_not_current")
    elif r["reservation"]["expires_at"] != _earliest(
        r["reservation"], r["currentness"], r["resource"]
    ):
        reasons.append("reservation_expiry_not_earliest_boundary")
    elif _at(r["replay"]["evaluated_at"]) >= _at(r["reservation"]["expires_at"]):
        reasons.append("reservation_expired_revalidate_only")
    if reasons:
        preview = "fail_closed"
        reservation = {"status": "not_reserved", "reason": reasons[0]}
    # feedback is always append-only observation; failure/unknown are retained verbatim.
    half_open = {"status": "not_eligible", "candidate_resource_keys": []}
    recovery = (
        r["feedback"]["outcome"] == "recovery" and r["feedback"]["fresh_exact_bound"]
    )
    if not reasons and recovery:
        half_open = {
            "status": "would_consider_one_candidate",
            "candidate_resource_keys": [r["resource"]["canonical_key"]],
        }
    retry_status = (
        "would_not_retry"
        if r["retry_budget"]["automatic_retries"] == 0
        else "preview_only"
    )
    event_results = [
        {"event_id": e["event_id"], "status": "retained_observation"}
        for e in r["predecessor_events"]
    ]
    body: dict[str, Any] = {
        "schema_version": 1,
        "contract": REPORT_CONTRACT,
        "evaluated_at": r["replay"]["evaluated_at"],
        "scope": deepcopy(r["scope"]),
        "preview": preview,
        "reason_codes": sorted(set(reasons or ["exact_bound_report_only_preview"])),
        "reservation_preview": reservation,
        "feedback_preview": feedback,
        "half_open_preview": half_open,
        "retry_preview": {
            "status": retry_status,
            "remaining_preview": r["retry_budget"]["remaining"],
            "separate_from_provider_quota": True,
            "separate_from_task_attempt_limit": True,
        },
        "event_results": event_results,
        "simulation_request": deepcopy(r),
        "input_digest": stable_digest(r),
        "simulation_only": True,
        "activation_authority": False,
        "runtime_reservation": False,
        "runtime_feedback_mutation": False,
        "automatic_half_open": False,
        "automatic_retry": False,
        "queue_mutation": False,
        "config_mutation": False,
        "cooldown_mutation": False,
        "wake_mutation": False,
        "selection_mutation": False,
        "dispatch_authority": False,
        "provider_call": False,
        "promotion_authority": False,
        "live_routing": False,
        "default_routing": False,
        "worker_promotion": False,
        "provider_promotion": False,
        "manual_override_binding_resolved": False,
        **{f: [] for f in MUTATION_FIELDS},
    }
    body["replay_digest"] = stable_digest(body)
    return body


def _scope(v: object) -> dict[str, Any]:
    d = _obj("request.scope", v)
    _keys(
        "request.scope",
        d,
        {
            "project_id",
            "repository_id",
            "task_class",
            "task_id",
            "attempt_id",
            "target_id",
            "opted_in",
        },
    )
    for k in d:
        if k != "opted_in":
            _id("request.scope." + k, d[k])
    _lit("request.scope.opted_in", d["opted_in"], True)
    return deepcopy(d)


def _revisions(v: object) -> dict[str, Any]:
    d = _obj("request.revisions", v)
    _keys(
        "request.revisions",
        d,
        {
            "mapping_revision",
            "currentness_revision",
            "policy_revision",
            "selector_revision",
            "resume_revision",
            "simulation_policy_revision",
        },
    )
    for k, x in d.items():
        _id("request.revisions." + k, x)
    _lit(
        "request.revisions.simulation_policy_revision",
        d["simulation_policy_revision"],
        POLICY_REVISION,
    )
    return deepcopy(d)


def _currentness(v: object, rev: dict[str, Any]) -> dict[str, Any]:
    d = _timed(
        "request.currentness", v, {"revision", "status", "observed_at", "expires_at"}
    )
    _lit("request.currentness.revision", d["revision"], rev["currentness_revision"])
    if d["status"] not in {
        "current",
        "stale",
        "unknown",
        "missing",
        "ambiguous",
        "conflicting",
    }:
        raise CapacityReservationFeedbackSimulationError(
            "request.currentness.status invalid"
        )
    return d


def _selector(
    v: object, scope: dict[str, Any], revisions: dict[str, Any]
) -> dict[str, Any]:
    d = _obj("request.selector_binding", v)
    _keys(
        "request.selector_binding",
        d,
        {
            "status",
            "baseline_digest",
            "resume_binding",
            "selector_revision",
            "resume_revision",
            "eligible_target_ids",
        },
    )
    _digest("request.selector_binding.baseline_digest", d["baseline_digest"])
    _id("request.selector_binding.resume_binding", d["resume_binding"])
    _lit(
        "request.selector_binding.selector_revision",
        d["selector_revision"],
        revisions["selector_revision"],
    )
    _lit(
        "request.selector_binding.resume_revision",
        d["resume_revision"],
        revisions["resume_revision"],
    )
    if (
        d["status"] not in {"eligible", "ineligible", "unknown", "stale", "conflicting"}
        or not isinstance(d["eligible_target_ids"], list)
        or scope["target_id"] not in d["eligible_target_ids"]
    ):
        raise CapacityReservationFeedbackSimulationError("selector binding invalid")
    return deepcopy(d)


def _global(v: object) -> dict[str, Any]:
    d = _obj("request.global_admission", v)
    _keys("request.global_admission", d, {"status", "decision_key", "wake_key"})
    if d["status"] not in {"allowed", "gated", "unknown", "missing", "conflicting"}:
        raise CapacityReservationFeedbackSimulationError("global admission invalid")
    _id("request.global_admission.decision_key", d["decision_key"])
    _id("request.global_admission.wake_key", d["wake_key"])
    return deepcopy(d)


def _resource(
    v: object,
    revisions: dict[str, Any],
    mapping: dict[str, Any],
    policy: dict[str, Any],
    scope: dict[str, Any],
) -> dict[str, Any]:
    d = _timed(
        "request.resource",
        v,
        {
            "canonical_key",
            "mapping_status",
            "mapping_revision",
            "policy_revision",
            "observed_at",
            "expires_at",
        },
    )
    _id("request.resource.canonical_key", d["canonical_key"])
    _lit(
        "request.resource.mapping_revision",
        d["mapping_revision"],
        revisions["mapping_revision"],
    )
    _lit(
        "request.resource.policy_revision",
        d["policy_revision"],
        revisions["policy_revision"],
    )
    bindings = [
        item
        for item in mapping["bindings"]
        if item["target_id"] == scope["target_id"]
        and item["status"] == "current"
        and item["identity_authority"] == "source_attested"
    ]
    if len(bindings) != 1:
        raise CapacityReservationFeedbackSimulationError(
            "source-attested mapping binding missing or ambiguous"
        )
    binding = bindings[0]
    expected_key = resource_gate_key(
        binding["provider_id"],
        binding["quota_identity_id"],
        binding["observation_scope"]["scope_id"],
        "primary",
    )
    _lit("request.resource.canonical_key", d["canonical_key"], expected_key)
    if (
        mapping["status"] != "current"
        or policy["status"] != "current"
        or not policy["enabled"]
        or mapping["mapping_revision"] not in policy["allowed_mapping_revisions"]
    ):
        raise CapacityReservationFeedbackSimulationError(
            "mapping or policy not currently admitted"
        )
    if d["mapping_status"] not in {
        "exact",
        "unknown",
        "stale",
        "missing",
        "ambiguous",
        "conflicting",
    }:
        raise CapacityReservationFeedbackSimulationError("resource mapping invalid")
    return d


def _replay(v: object) -> dict[str, Any]:
    d = _obj("request.replay", v)
    _keys("request.replay", d, {"evaluated_at"})
    _at(d["evaluated_at"])
    return deepcopy(d)


def _events(v: object, clock: str) -> list[dict[str, Any]]:
    if not isinstance(v, list):
        raise CapacityReservationFeedbackSimulationError(
            "predecessor_events must be a list"
        )
    out = []
    previous = None
    last = None
    seen_ids: set[str] = set()
    seen_digests: set[str] = set()
    for i, raw in enumerate(v):
        d = _obj(f"predecessor_events[{i}]", raw)
        _keys(
            f"predecessor_events[{i}]",
            d,
            {"event_id", "predecessor_event_id", "observed_at", "evidence_digest"},
        )
        _id("event_id", d["event_id"])
        _digest("evidence_digest", d["evidence_digest"])
        if d["event_id"] in seen_ids or d["evidence_digest"] in seen_digests:
            raise CapacityReservationFeedbackSimulationError(
                "duplicate event id or evidence digest"
            )
        now = _at(d["observed_at"])
        if (
            d["predecessor_event_id"] != previous
            or (last and now <= last)
            or now > _at(clock)
        ):
            raise CapacityReservationFeedbackSimulationError(
                "predecessor lineage is broken or unordered"
            )
        out.append(deepcopy(d))
        seen_ids.add(d["event_id"])
        seen_digests.add(d["evidence_digest"])
        previous = d["event_id"]
        last = now
    return out


def _reservation(
    v: object,
    s: dict[str, Any],
    resource: dict[str, Any],
    rev: dict[str, Any],
    cur: dict[str, Any],
) -> dict[str, Any]:
    d = _obj("request.reservation", v)
    _keys(
        "request.reservation",
        d,
        {
            "task_id",
            "attempt_id",
            "target_id",
            "resource_key",
            "evidence_digest",
            "policy_revision",
            "expires_at",
            "authoritative_wake_at",
        },
    )
    for k in (
        "task_id",
        "attempt_id",
        "target_id",
        "resource_key",
        "evidence_digest",
        "policy_revision",
    ):
        (_digest if k == "evidence_digest" else _id)("reservation." + k, d[k])
    if (
        {
            "task_id": d["task_id"],
            "attempt_id": d["attempt_id"],
            "target_id": d["target_id"],
        }
        != {k: s[k] for k in ("task_id", "attempt_id", "target_id")}
        or d["resource_key"] != resource["canonical_key"]
        or d["policy_revision"] != rev["policy_revision"]
    ):
        raise CapacityReservationFeedbackSimulationError(
            "reservation exact binding mismatch"
        )
    _at(d["expires_at"])
    _at(d["authoritative_wake_at"])
    return deepcopy(d)


def _feedback(
    v: object,
    s: dict[str, Any],
    resource: dict[str, Any],
    rev: dict[str, Any],
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    d = _obj("request.feedback", v)
    _keys(
        "request.feedback",
        d,
        {
            "event_id",
            "task_id",
            "attempt_id",
            "target_id",
            "resource_key",
            "outcome",
            "fresh_exact_bound",
            "predecessor_event_id",
        },
    )
    for k in ("event_id", "task_id", "attempt_id", "target_id", "resource_key"):
        _id("feedback." + k, d[k])
    if (
        d["outcome"] not in {"success", "failure", "unknown", "recovery"}
        or type(d["fresh_exact_bound"]) is not bool
        or d["task_id"] != s["task_id"]
        or d["attempt_id"] != s["attempt_id"]
        or d["target_id"] != s["target_id"]
        or d["resource_key"] != resource["canonical_key"]
        or d["predecessor_event_id"] != (events[-1]["event_id"] if events else None)
        or d["event_id"] in {event["event_id"] for event in events}
    ):
        raise CapacityReservationFeedbackSimulationError(
            "feedback exact binding or lineage mismatch"
        )
    return deepcopy(d)


def _retry(v: object, s: dict[str, Any]) -> dict[str, Any]:
    d = _obj("request.retry_budget", v)
    _keys(
        "request.retry_budget",
        d,
        {
            "task_id",
            "attempt_id",
            "remaining",
            "automatic_retries",
            "provider_quota_bound",
            "task_attempt_limit_bound",
        },
    )
    _id("retry.task_id", d["task_id"])
    _id("retry.attempt_id", d["attempt_id"])
    if (
        d["task_id"] != s["task_id"]
        or d["attempt_id"] != s["attempt_id"]
        or type(d["remaining"]) is not int
        or d["remaining"] < 0
        or d["automatic_retries"] != 0
        or d["provider_quota_bound"]
        or d["task_attempt_limit_bound"]
    ):
        raise CapacityReservationFeedbackSimulationError(
            "retry budget must be independent and automatic retries zero"
        )
    return deepcopy(d)


def _timed(n: str, v: object, keys: set[str]) -> dict[str, Any]:
    d = _obj(n, v)
    _keys(n, d, keys)
    for k in ("observed_at", "expires_at"):
        _at(d[k])
    if _at(d["observed_at"]) >= _at(d["expires_at"]):
        raise CapacityReservationFeedbackSimulationError(n + " validity window invalid")
    return deepcopy(d)


def _earliest(
    res: dict[str, Any], cur: dict[str, Any], resource: dict[str, Any]
) -> str:
    candidates = (
        res["authoritative_wake_at"],
        cur["expires_at"],
        resource["expires_at"],
    )
    return min(candidates, key=_at)


def _in_window(now: str, d: dict[str, Any]) -> bool:
    return _at(d["observed_at"]) <= _at(now) < _at(d["expires_at"])


def _obj(n: str, v: object) -> dict[str, Any]:
    if not isinstance(v, dict):
        raise CapacityReservationFeedbackSimulationError(n + " must be an object")
    return v


def _keys(n: str, d: dict[str, Any], expected: set[str]) -> None:
    if set(d) != expected:
        raise CapacityReservationFeedbackSimulationError(
            n + " has unknown, missing, or malformed fields"
        )


def _lit(n: str, v: object, e: object) -> None:
    if v != e or (isinstance(e, bool) and type(v) is not bool):
        raise CapacityReservationFeedbackSimulationError(n + " mismatch")


def _int_literal(n: str, v: object, e: int) -> None:
    if type(v) is not int or v != e:
        raise CapacityReservationFeedbackSimulationError(n + " mismatch")


def _id(n: str, v: object) -> str:
    if not isinstance(v, str) or not v or any(c not in SAFE for c in v):
        raise CapacityReservationFeedbackSimulationError(
            n + " must be a safe identifier"
        )
    return v


def _digest(n: str, v: object) -> str:
    value = _id(n, v)
    if (
        not value.startswith("sha256:")
        or len(value) != 71
        or any(char not in "0123456789abcdef" for char in value[7:])
    ):
        raise CapacityReservationFeedbackSimulationError(n + " must be sha256")
    return value


def _at(v: object) -> datetime:
    if not isinstance(v, str):
        raise CapacityReservationFeedbackSimulationError("timestamp invalid")
    try:
        d = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CapacityReservationFeedbackSimulationError("timestamp invalid") from exc
    if d.tzinfo is None:
        raise CapacityReservationFeedbackSimulationError("timestamp timezone required")
    return d
