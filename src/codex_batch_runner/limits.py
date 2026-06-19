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
    lowered = text.lower()
    return any(marker in lowered for marker in RATE_LIMIT_MARKERS)
