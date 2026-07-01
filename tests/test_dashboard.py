from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from codex_batch_runner.config import Config
from codex_batch_runner.dashboard import dashboard_handler_class, render_dashboard_html
from codex_batch_runner.queue import create_task, list_tasks


def write_config(tmp: str) -> Path:
    root = Path(tmp)
    config_path = root / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "queue_dir": str(root / "tasks"),
                "log_dir": str(root / "logs"),
                "event_dir": str(root / "events"),
                "lock_file": str(root / "runner.lock"),
                "state_file": str(root / "state.json"),
            }
        ),
        encoding="utf-8",
    )
    return config_path


class DashboardServer:
    def __init__(self, config: Config) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard_handler_class(config))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    def __enter__(self) -> "DashboardServer":
        self.thread.start()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()


def get_text(url: str) -> tuple[int, str, str]:
    with urlopen(url, timeout=5) as response:
        return response.status, response.headers.get("Content-Type", ""), response.read().decode("utf-8")


class DashboardHttpTests(unittest.TestCase):
    def test_api_returns_dashboard_data_and_missing_index_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(str(write_config(tmp)))
            create_task(config, "work", tmp, task_id="ready", title="Ready task")

            with DashboardServer(config) as server:
                status, content_type, body = get_text(server.base_url + "/api/dashboard")

            payload = json.loads(body)
            self.assertEqual(200, status)
            self.assertIn("application/json", content_type)
            self.assertEqual("canonical_fallback", payload["data_source"])
            self.assertTrue(payload["fallback_used"])
            self.assertIn("missing", " ".join(payload["warnings"]))
            self.assertEqual(1, payload["tasks"]["total"])

    def test_root_renders_read_only_page_with_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(str(write_config(tmp)))
            create_task(config, "work", tmp, task_id="ready", title="Ready task")

            with DashboardServer(config) as server:
                status, content_type, body = get_text(server.base_url + "/")

            self.assertEqual(200, status)
            self.assertIn("text/html", content_type)
            self.assertIn("Local read-only operator overview", body)
            self.assertIn("Index warnings", body)
            self.assertIn("Queue overview", body)
            self.assertIn("Operator attention", body)
            self.assertIn("Recent sanitized events", body)
            self.assertIn("index database is missing", body)
            self.assertIn("/api/dashboard", body)
            self.assertIn("cbr list --needs-review", body)
            self.assertIn("cbr review-next --dry-run", body)
            self.assertIn("cbr worktree apply TASK_ID --dry-run", body)
            self.assertIn("cbr events --limit 10", body)
            self.assertIn("cbr index rebuild --dry-run", body)
            self.assertNotIn("<button", body.lower())
            self.assertNotIn("<form", body.lower())

    def test_non_get_and_unknown_routes_do_not_mutate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(str(write_config(tmp)))
            create_task(config, "work", tmp, task_id="ready", title="Ready task")

            with DashboardServer(config) as server:
                with self.assertRaises(HTTPError) as post_error:
                    urlopen(Request(server.base_url + "/api/dashboard", method="POST"), timeout=5)
                with self.assertRaises(HTTPError) as missing_error:
                    urlopen(server.base_url + "/missing", timeout=5)

            self.assertEqual(405, post_error.exception.code)
            self.assertEqual(404, missing_error.exception.code)
            self.assertEqual(1, len(list_tasks(config)))

    def test_render_dashboard_html_escapes_values(self) -> None:
        body = render_dashboard_html(
            {
                "warnings": ["<script>alert(1)</script>"],
                "tasks": {
                    "total": 0,
                    "active": 0,
                    "runnable": 0,
                    "needs_resume": 0,
                    "by_status": {},
                    "capacity": {"running_total": 0, "max_total_running": 1, "max_running_per_project": 1},
                },
                "review": {"backlog": {"total": 0, "by_review_status": {}}, "accepted_unapplied": 0},
                "failures": {"failed": 0, "blocked_user": 0, "usage_exhausted": 0, "failed_or_blocked": 0},
                "running": {"total": 0, "stale_progress": 0},
                "cooldowns": {},
                "recent_events": {"recent_count": 0, "by_type": {}, "recent": []},
                "data_source": "canonical_fallback",
                "fallback_used": True,
            }
        )

        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", body)
        self.assertNotIn("<script>", body)

    def test_render_dashboard_html_contains_v1_operator_overview_and_sanitized_events(self) -> None:
        body = render_dashboard_html(
            {
                "warnings": ["index schema mismatch: found 0, expected 1; canonical fallback used"],
                "tasks": {
                    "total": 9,
                    "active": 6,
                    "runnable": 2,
                    "needs_resume": 1,
                    "by_status": {"failed": 1, "needs_resume": 1, "runnable": 2, "running": 2},
                    "capacity": {"running_total": 2, "max_total_running": 3, "max_running_per_project": 1},
                },
                "review": {
                    "backlog": {"total": 3, "by_review_status": {"needs_followup": 1, "unreviewed": 2}},
                    "accepted_unapplied": 4,
                },
                "failures": {"failed": 1, "blocked_user": 1, "usage_exhausted": 1, "failed_or_blocked": 3},
                "running": {"total": 2, "stale_progress": 1},
                "cooldowns": {
                    "global": {
                        "active": True,
                        "cooldown_until": "2026-01-01T00:00:00+00:00",
                        "last_rate_limit_at": "2025-12-31T23:00:00+00:00",
                    },
                    "reviewer_codex": {"active": False, "cooldown_until": None, "last_rate_limit_at": None},
                },
                "recent_events": {
                    "recent_count": 1,
                    "by_type": {"task_created": 1},
                    "recent": [
                        {
                            "event_type": "task_created",
                            "occurred_at": "2026-01-01T00:00:00+00:00",
                            "task_id": "task-public",
                            "project_id": "project-public",
                            "payload": {"prompt": "SECRET_PROMPT"},
                            "summary": "SECRET_SUMMARY",
                        }
                    ],
                },
                "data_source": "sqlite_index",
                "fallback_used": False,
            }
        )

        for expected in [
            "Queue overview",
            "Total tasks",
            "Active tasks",
            "Runnable",
            "Needs resume",
            "Running total",
            "Operator attention",
            "Review needed",
            "Accepted unapplied",
            "Failed or blocked",
            "Running stale progress",
            "Global cooldown",
            "Reviewer rate-limit",
            "Recent sanitized events",
            "task_created",
            "task-public",
            "project-public",
        ]:
            self.assertIn(expected, body)
        for command in [
            "cbr list --needs-review",
            "cbr review-next --dry-run",
            "cbr worktree apply TASK_ID --dry-run",
            "cbr events --limit 10",
            "cbr index rebuild --dry-run",
        ]:
            self.assertIn(command, body)
        self.assertIn("index schema mismatch", body)
        self.assertNotIn("SECRET_PROMPT", body)
        self.assertNotIn("SECRET_SUMMARY", body)
        self.assertNotIn("payload", body)
        self.assertNotIn("<button", body.lower())
        self.assertNotIn("<form", body.lower())


if __name__ == "__main__":
    unittest.main()
