from __future__ import annotations

import plistlib
import unittest

from codex_batch_runner.launchd_lifecycle import (
    MARKER_KEY,
    MARKER_VERSION,
    LaunchdPlanInput,
    plan_launchd_lifecycle,
    render_launchd_plist,
)


def plan_input(**overrides: object) -> LaunchdPlanInput:
    values: dict[str, object] = {
        "label": "com.example.codex-batch-runner",
        "executable_path": "/opt/cbr/bin/cbr",
        "config_path": "/var/example/cbr/config.json",
        "config_provenance": "xdg",
        "working_directory": "/opt/cbr",
        "stdout_path": "/var/example/cbr/launchd.out.log",
        "stderr_path": "/var/example/cbr/launchd.err.log",
        "environment_path": "/opt/cbr/bin:/usr/bin:/bin",
        "start_interval_seconds": 600,
    }
    values.update(overrides)
    return LaunchdPlanInput(**values)  # type: ignore[arg-type]


class LaunchdLifecycleTests(unittest.TestCase):
    def test_render_is_deterministic_and_contains_only_expected_scheduler_values(self) -> None:
        rendered = render_launchd_plist(plan_input())
        self.assertEqual(rendered, render_launchd_plist(plan_input()))
        plist = plistlib.loads(rendered)
        self.assertEqual(
            ["/opt/cbr/bin/cbr", "--config", "/var/example/cbr/config.json", "run-loop", "--json"],
            plist["ProgramArguments"],
        )
        self.assertEqual(MARKER_VERSION, plist[MARKER_KEY]["version"])
        self.assertRegex(plist[MARKER_KEY]["digest"], r"^[0-9a-f]{64}$")
        self.assertNotIn("launchctl", rendered.decode("utf-8"))

    def test_missing_plist_plans_creation_without_mutation(self) -> None:
        plan = plan_launchd_lifecycle(plan_input(), None)
        self.assertEqual(("not_installed", "create"), (plan.status, plan.action))
        self.assertEqual("xdg", plan.config_provenance)

    def test_matching_owned_plist_is_no_op(self) -> None:
        rendered = render_launchd_plist(plan_input())
        plan = plan_launchd_lifecycle(plan_input(), rendered)
        self.assertEqual(("managed_ok", "none"), (plan.status, plan.action))

    def test_valid_owned_plist_with_different_inputs_is_drifted(self) -> None:
        existing = render_launchd_plist(plan_input(start_interval_seconds=300))
        plan = plan_launchd_lifecycle(plan_input(), existing)
        self.assertEqual(("drifted", "update_needed"), (plan.status, plan.action))

    def test_missing_marker_is_foreign_conflict(self) -> None:
        plist = plistlib.loads(render_launchd_plist(plan_input()))
        del plist[MARKER_KEY]
        plan = plan_launchd_lifecycle(plan_input(), plistlib.dumps(plist))
        self.assertEqual(("foreign_conflict", "blocked"), (plan.status, plan.action))

    def test_invalid_marker_or_malformed_plist_is_unhealthy(self) -> None:
        plist = plistlib.loads(render_launchd_plist(plan_input()))
        plist[MARKER_KEY]["digest"] = "not-a-digest"
        invalid_marker = plan_launchd_lifecycle(plan_input(), plistlib.dumps(plist))
        malformed = plan_launchd_lifecycle(plan_input(), b"not a plist")
        self.assertEqual(("unhealthy", "blocked"), (invalid_marker.status, invalid_marker.action))
        self.assertEqual(("unhealthy", "blocked"), (malformed.status, malformed.action))

    def test_tampered_owned_content_fails_closed(self) -> None:
        plist = plistlib.loads(render_launchd_plist(plan_input()))
        plist["StartInterval"] = 30
        plan = plan_launchd_lifecycle(plan_input(), plistlib.dumps(plist))
        self.assertEqual(("unhealthy", "blocked"), (plan.status, plan.action))
        self.assertIn("does not match", plan.reason)

    def test_owned_plist_with_extra_behavior_or_environment_key_is_unhealthy(self) -> None:
        extra_behavior = plistlib.loads(render_launchd_plist(plan_input()))
        extra_behavior["KeepAlive"] = True
        behavior_plan = plan_launchd_lifecycle(plan_input(), plistlib.dumps(extra_behavior))

        extra_environment = plistlib.loads(render_launchd_plist(plan_input()))
        extra_environment["EnvironmentVariables"]["PYTHONPATH"] = "/opt/cbr/src"
        environment_plan = plan_launchd_lifecycle(plan_input(), plistlib.dumps(extra_environment))

        self.assertEqual(("unhealthy", "blocked"), (behavior_plan.status, behavior_plan.action))
        self.assertIn("top-level keys", behavior_plan.reason)
        self.assertEqual(("unhealthy", "blocked"), (environment_plan.status, environment_plan.action))
        self.assertIn("EnvironmentVariables keys", environment_plan.reason)

    def test_invalid_input_is_rejected_before_rendering(self) -> None:
        with self.assertRaisesRegex(ValueError, "absolute path"):
            render_launchd_plist(plan_input(config_path="relative/config.json"))
        with self.assertRaisesRegex(ValueError, "config_provenance"):
            render_launchd_plist(plan_input(config_provenance="unknown"))

    def test_environment_path_requires_only_nonempty_absolute_segments(self) -> None:
        self.assertEqual(
            "/opt/cbr/bin:/usr/bin:/bin",
            plistlib.loads(render_launchd_plist(plan_input()))["EnvironmentVariables"]["PATH"],
        )
        for value in (
            ":/usr/bin",
            "/usr/bin:",
            "/usr/bin::/bin",
            "/usr/bin:.",
            "/usr/bin:relative/bin",
        ):
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError,
                "environment_path segments must be non-empty absolute paths",
            ):
                render_launchd_plist(plan_input(environment_path=value))


if __name__ == "__main__":
    unittest.main()
