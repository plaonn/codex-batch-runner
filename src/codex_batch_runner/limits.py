from __future__ import annotations

RATE_LIMIT_MARKERS = (
    "rate limit",
    "rate-limit",
    "usage limit",
    "usage-limit",
    "too many requests",
    "429",
    "quota",
    "try again",
)


def looks_like_rate_limit(text: str) -> bool:
    return bool(matched_rate_limit_markers(text))


def matched_rate_limit_markers(text: str) -> list[str]:
    lowered = text.lower()
    return [marker for marker in RATE_LIMIT_MARKERS if marker in lowered]
