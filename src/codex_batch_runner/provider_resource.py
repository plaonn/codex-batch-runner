from __future__ import annotations

from typing import Any

from .request_fingerprint import _safe_metadata_value

PROVIDER_RESOURCE_EVIDENCE_VERSION = "provider-resource-evidence-v1"
DEFAULT_PROVIDER_ID = "codex"
DEFAULT_QUOTA_BOUNDARY = "unknown"
DEFAULT_SHARING_ASSUMPTION = "not_independent"


def derive_provider_resource_evidence(task: dict[str, Any]) -> dict[str, Any]:
    """Return read-only provider/resource evidence for routing analysis.

    This deliberately models current uncertainty without deriving provider
    quota identity from local scheduler pools, worker roles, or legacy profiles.
    """
    explicit = task.get("provider_resource") if isinstance(task.get("provider_resource"), dict) else {}
    provider_id = _safe_metadata_value(explicit.get("provider_id") or task.get("provider_id") or DEFAULT_PROVIDER_ID)
    if provider_id == "unknown":
        provider_id = DEFAULT_PROVIDER_ID
    quota_boundary = _safe_metadata_value(explicit.get("quota_boundary") or DEFAULT_QUOTA_BOUNDARY)
    sharing_assumption = _safe_metadata_value(explicit.get("sharing_assumption") or DEFAULT_SHARING_ASSUMPTION)
    return {
        "schema_version": 1,
        "derivation_version": PROVIDER_RESOURCE_EVIDENCE_VERSION,
        "read_only": True,
        "provider_id": provider_id,
        "quota_boundary": quota_boundary,
        "sharing_assumption": sharing_assumption,
        "identity_source": "explicit_provider_resource" if explicit else "current_codex_uncertainty",
        "derived_from_capacity_pool": False,
        "derived_from_worker_role": False,
        "derived_from_legacy_profile": False,
        "advisory_only": True,
    }


def provider_resource_key(evidence: dict[str, Any]) -> str:
    return (
        f"provider={_safe_metadata_value(evidence.get('provider_id'))} "
        f"quota_boundary={_safe_metadata_value(evidence.get('quota_boundary'))} "
        f"sharing={_safe_metadata_value(evidence.get('sharing_assumption'))}"
    )
