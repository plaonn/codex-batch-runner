from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from codex_batch_runner.antigravity_statusline import (
    ANTIGRAVITY_STATUSLINE_CACHE_CONTRACT,
    collect_statusline_quota,
    read_statusline_cache,
    write_statusline_cache,
)


class AntigravityStatuslineTests(unittest.TestCase):
    def test_allowlist_projection_preserves_only_quota_fields(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "antigravity-statusline-quota-v1.json"
        source = json.loads(fixture.read_text(encoding="utf-8"))
        source.update({"account": "private", "path": "/private", "sessionId": "secret"})
        source["quota"]["gemini-weekly"].update({"identity": "secret", "window_seconds": 3600})

        result = collect_statusline_quota(
            source,
            collected_at=datetime(2030, 1, 2, 4, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(result["status"], "observed")
        cache = result["cache"]
        self.assertEqual(cache["contract"], ANTIGRAVITY_STATUSLINE_CACHE_CONTRACT)
        self.assertEqual(cache["source_version"], "statusline-v1")
        self.assertEqual(cache["plan_tier"], "pro")
        self.assertEqual(cache["field_presence"], {"version": True, "plan_tier": True, "quota": True})
        self.assertRegex(cache["format_fingerprint"], r"^[0-9a-f]{64}$")
        self.assertEqual(cache["collected_at"], "2030-01-02T04:00:00+00:00")
        self.assertEqual(cache["timestamp_provenance"], "statusline_callback_received_at")
        self.assertEqual(cache["freshness_authority"], "advisory_only")
        self.assertEqual(cache["buckets"], [{"bucket_id": "gemini-weekly", "remaining_fraction": 0.375, "reset_time": "2030-01-02T05:00:00Z", "reset_in_seconds": 3600}])
        self.assertNotIn("identity", json.dumps(cache))
        self.assertNotIn("window_seconds", json.dumps(cache))

    def test_invalid_shape_is_non_blocking_and_exposes_only_drift_evidence(self) -> None:
        result = collect_statusline_quota({"version": "statusline-v2", "quota": {"gemini-weekly": {"remaining_fraction": 1.1}}})

        self.assertEqual(result["status"], "invalid")
        self.assertEqual(result["reason"], "quota_bucket_invalid")
        self.assertIsNone(result["cache"])
        self.assertEqual(result["source_version"], "statusline-v2")
        self.assertEqual(result["field_presence"], {"version": True, "plan_tier": False, "quota": True})
        self.assertRegex(result["format_fingerprint"], r"^[0-9a-f]{64}$")

        reset_leak = collect_statusline_quota({"quota": {"gemini-weekly": {"remaining_fraction": 0.5, "reset_time": "private-account@example.test"}}})
        self.assertEqual(reset_leak["status"], "invalid")

    def test_atomic_cache_round_trip_is_bounded_and_rejects_tampering(self) -> None:
        result = collect_statusline_quota({"quota": {"gemini-weekly": {"remaining_fraction": 0.5, "reset_in_seconds": 30}}})
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "statusline.json"
            self.assertTrue(write_statusline_cache(path, result))
            self.assertEqual(read_statusline_cache(path), result["cache"])
            self.assertIsNone(read_statusline_cache(path, max_bytes=1))
            path.write_text('{"raw":"private"}', encoding="utf-8")
            self.assertIsNone(read_statusline_cache(path))

    def test_cache_write_refuses_failure_or_raw_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "statusline.json"
            self.assertFalse(write_statusline_cache(path, collect_statusline_quota({"quota": {}})))
            self.assertFalse(write_statusline_cache(path, {"status": "observed", "cache": {"raw": "private"}}))
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
