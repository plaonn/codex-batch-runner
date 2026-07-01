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
            self.assertIn("Local read-only overview", body)
            self.assertIn("Index warnings", body)
            self.assertIn("index database is missing", body)
            self.assertIn("/api/dashboard", body)

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
                "tasks": {"total": 0, "active": 0, "runnable": 0, "needs_resume": 0, "by_status": {}},
                "review": {"backlog": {"total": 0}, "accepted_unapplied": 0},
                "failures": {"failed_or_blocked": 0},
                "running": {"stale_progress": 0},
                "cooldowns": {},
                "recent_events": {"recent_count": 0},
                "data_source": "canonical_fallback",
                "fallback_used": True,
            }
        )

        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", body)
        self.assertNotIn("<script>", body)


if __name__ == "__main__":
    unittest.main()
