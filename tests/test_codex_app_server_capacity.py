from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

from codex_batch_runner.codex_app_server_capacity import acquire_capacity, project_rate_limits_response


NOW = datetime(2030, 1, 2, 4, 0, tzinfo=timezone.utc)
FAKE = Path(__file__).parent / "fixtures" / "fake_codex_app_server.py"


def command(mode: str) -> list[str]:
    return [sys.executable, str(FAKE), mode]


class CodexAppServerCapacityTests(unittest.TestCase):
    def test_one_shot_rpc_projects_primary_and_secondary_remaining_ratios(self) -> None:
        result = acquire_capacity(command("success"), evaluated_at=NOW)
        self.assertEqual((result["status"], result["reason"], result["method"]), ("observed", None, "account/rateLimits/read"))
        self.assertEqual(result["contract"], "codex-app-server-capacity-v1")
        self.assertEqual(result["resources"], [{
            "limit_id": "codex",
            "plan_type": "prolite",
            "windows": [
            {"window_id": "primary", "status": "observed", "window_duration_seconds": 18000.0, "used_ratio": 0.25, "remaining_ratio": 0.75, "resets_at": "2030-01-02T05:00:00+00:00"},
            {"window_id": "secondary", "status": "observed", "window_duration_seconds": 604800.0, "used_ratio": 0.8, "remaining_ratio": 0.19999999999999996, "resets_at": "2030-01-09T04:00:00+00:00"},
        ]}])
        self.assertTrue(result["read_only"])
        self.assertEqual(result["advisory_fallback"]["status"], "not_used")

    def test_method_not_found_malformed_timeout_and_output_bound_are_sanitized(self) -> None:
        for mode, limit, expected in (("method-not-found", 65536, ("unknown", "method_not_found")), ("malformed", 65536, ("unknown", "malformed_response")), ("timeout", 65536, ("unavailable", "timeout")), ("oversized", 100, ("unavailable", "output_limit"))):
            with self.subTest(mode=mode):
                result = acquire_capacity(command(mode), timeout_seconds=0.2, max_output_bytes=limit, evaluated_at=NOW)
                self.assertEqual((result["status"], result["reason"]), expected)
                self.assertEqual(result["resources"], [])
                self.assertNotIn("raw", result)

    def test_malformed_window_becomes_unknown_without_copying_other_response_fields(self) -> None:
        result = project_rate_limits_response({"rateLimits": {"primary": {"windowDurationMins": 300, "usedPercent": 101, "resetsAt": "bad"}, "account": "must-not-leak"}}, evaluated_at=NOW)
        assert result is not None
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["resources"][0]["windows"][0], {"window_id": "primary", "status": "unknown", "window_duration_seconds": 18000.0, "used_ratio": None, "remaining_ratio": None, "resets_at": None})
        self.assertNotIn("account", result)

    def test_multi_limit_projection_deduplicates_legacy_view_and_drops_private_fields(self) -> None:
        result = project_rate_limits_response(
            {
                "rateLimits": {
                    "limitId": "codex",
                    "planType": "prolite",
                    "primary": {"windowDurationMins": 300, "usedPercent": 25},
                    "credits": {"balance": "private"},
                },
                "rateLimitsByLimitId": {
                    "codex": {
                        "limitId": "codex",
                        "planType": "prolite",
                        "primary": {"windowDurationMins": 300, "usedPercent": 25},
                    },
                    "codex_bengalfox": {
                        "limitId": "codex_bengalfox",
                        "planType": "prolite",
                        "secondary": {"windowDurationMins": 10080, "usedPercent": 80},
                    },
                },
                "account": {"email": "private@example.test"},
            },
            evaluated_at=NOW,
        )
        assert result is not None
        self.assertEqual(
            [resource["limit_id"] for resource in result["resources"]],
            ["codex", "codex_bengalfox"],
        )
        serialized = json.dumps(result)
        self.assertNotIn("credits", serialized)
        self.assertNotIn("private", serialized)
        self.assertNotIn("email", serialized)

    def test_failure_marks_fallback_advisory_and_stale_aware(self) -> None:
        fallback = project_rate_limits_response({"rateLimits": {"primary": {"windowDurationMins": 300, "usedPercent": 25, "resetsAt": "2030-01-02T05:00:00Z"}}}, evaluated_at=datetime(2030, 1, 2, 3, 59, tzinfo=timezone.utc))
        assert fallback is not None
        fallback_windows = fallback["resources"][0]["windows"]
        fresh = acquire_capacity(
            command("malformed"),
            evaluated_at=NOW,
            fallback={"collected_at": fallback["collected_at"], "windows": fallback_windows},
            fallback_max_age_seconds=120,
        )
        stale = acquire_capacity(
            command("malformed"),
            evaluated_at=NOW,
            fallback={"collected_at": fallback["collected_at"], "windows": fallback_windows},
            fallback_max_age_seconds=30,
        )
        self.assertEqual((fresh["status"], fresh["advisory_fallback"]["status"]), ("unknown", "fresh"))
        self.assertEqual((stale["status"], stale["advisory_fallback"]["status"]), ("unknown", "stale"))
        self.assertEqual(fresh["advisory_fallback"]["windows"][0]["remaining_ratio"], 0.75)
        unsafe = acquire_capacity(command("malformed"), evaluated_at=NOW, fallback={"observed_at": "2030-01-02T03:59:00Z", "windows": [{"window_id": "primary", "status": "observed", "used_ratio": "secret"}]}, fallback_max_age_seconds=120)
        self.assertEqual(unsafe["advisory_fallback"]["windows"], [])

    def test_invalid_invocation_does_not_launch_a_command(self) -> None:
        for argv in ([], "codex"):
            with self.subTest(argv=argv):
                result = acquire_capacity(argv, evaluated_at=NOW)
                self.assertEqual((result["status"], result["reason"]), ("unknown", "invalid_request"))


if __name__ == "__main__":
    unittest.main()
