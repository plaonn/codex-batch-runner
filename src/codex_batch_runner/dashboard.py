from __future__ import annotations

import html
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

from .config import Config
from .dashboard_data import build_dashboard_overview

DEFAULT_DASHBOARD_HOST = "127.0.0.1"
DEFAULT_DASHBOARD_PORT = 8765


def serve_dashboard(config: Config, host: str = DEFAULT_DASHBOARD_HOST, port: int = DEFAULT_DASHBOARD_PORT) -> None:
    server = ThreadingHTTPServer((host, port), dashboard_handler_class(config))
    print(f"serving read-only dashboard on http://{server.server_address[0]}:{server.server_address[1]}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def dashboard_handler_class(config: Config) -> type[BaseHTTPRequestHandler]:
    class DashboardHandler(BaseHTTPRequestHandler):
        server_version = "CodexBatchRunnerDashboard/1"

        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            if path == "/api/dashboard":
                self._send_json(build_dashboard_overview(config))
                return
            if path == "/":
                overview = build_dashboard_overview(config)
                self._send_html(render_dashboard_html(overview))
                return
            self._send_error(HTTPStatus.NOT_FOUND, "not found")

        def do_HEAD(self) -> None:
            path = urlsplit(self.path).path
            if path == "/api/dashboard":
                self._send_headers(HTTPStatus.OK, "application/json; charset=utf-8", 0)
                return
            if path == "/":
                self._send_headers(HTTPStatus.OK, "text/html; charset=utf-8", 0)
                return
            self._send_error(HTTPStatus.NOT_FOUND, "not found", body=False)

        def do_POST(self) -> None:
            self._send_read_only_method_error()

        def do_PUT(self) -> None:
            self._send_read_only_method_error()

        def do_PATCH(self) -> None:
            self._send_read_only_method_error()

        def do_DELETE(self) -> None:
            self._send_read_only_method_error()

        def log_message(self, format: str, *args: object) -> None:
            return

        def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
            body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
            self._send_headers(status, "application/json; charset=utf-8", len(body))
            self.wfile.write(body)

        def _send_html(self, body: str, status: HTTPStatus = HTTPStatus.OK) -> None:
            encoded = body.encode("utf-8")
            self._send_headers(status, "text/html; charset=utf-8", len(encoded))
            self.wfile.write(encoded)

        def _send_error(self, status: HTTPStatus, message: str, *, body: bool = True) -> None:
            payload = {"error": message, "read_only": True}
            encoded = json.dumps(payload, sort_keys=True).encode("utf-8") if body else b""
            self._send_headers(status, "application/json; charset=utf-8", len(encoded))
            if body:
                self.wfile.write(encoded)

        def _send_read_only_method_error(self) -> None:
            self._send_error(HTTPStatus.METHOD_NOT_ALLOWED, "dashboard is read-only")

        def _send_headers(self, status: HTTPStatus, content_type: str, content_length: int) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(content_length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Allow", "GET, HEAD")
            self.end_headers()

    return DashboardHandler


def render_dashboard_html(overview: dict[str, Any]) -> str:
    warnings = list(overview.get("warnings") or [])
    warning_html = "".join(f"<li>{escape_text(warning)}</li>" for warning in warnings) or "<li>None</li>"
    tasks = dict(overview.get("tasks") or {})
    review = dict(overview.get("review") or {})
    failures = dict(overview.get("failures") or {})
    running = dict(overview.get("running") or {})
    cooldowns = dict(overview.get("cooldowns") or {})
    recent_events = dict(overview.get("recent_events") or {})
    by_status = dict(tasks.get("by_status") or {})
    by_status_html = "".join(
        f"<li><span>{escape_text(status)}</span><strong>{escape_text(count)}</strong></li>"
        for status, count in sorted(by_status.items())
    ) or "<li><span>none</span><strong>0</strong></li>"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>cbr dashboard</title>
  <style>
    :root {{ color-scheme: light dark; font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    body {{ margin: 0; padding: 24px; line-height: 1.45; }}
    main {{ max-width: 960px; margin: 0 auto; }}
    h1 {{ margin: 0 0 4px; font-size: 28px; }}
    h2 {{ margin: 24px 0 8px; font-size: 18px; }}
    .meta {{ color: #666; margin-bottom: 20px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; }}
    .tile {{ border: 1px solid #bbb; border-radius: 6px; padding: 12px; }}
    .label {{ color: #666; font-size: 13px; }}
    .value {{ font-size: 28px; font-weight: 700; }}
    ul {{ padding-left: 22px; }}
    .status-list {{ list-style: none; padding-left: 0; max-width: 420px; }}
    .status-list li {{ display: flex; justify-content: space-between; border-bottom: 1px solid #ddd; padding: 4px 0; }}
    code {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
  </style>
</head>
<body>
<main>
  <h1>cbr dashboard</h1>
  <div class="meta">Local read-only overview. API: <code>/api/dashboard</code></div>
  <section aria-labelledby="warnings">
    <h2 id="warnings">Index warnings</h2>
    <ul>{warning_html}</ul>
  </section>
  <section class="grid" aria-label="summary">
    {metric_tile("Total tasks", tasks.get("total", 0))}
    {metric_tile("Active tasks", tasks.get("active", 0))}
    {metric_tile("Runnable", tasks.get("runnable", 0))}
    {metric_tile("Needs resume", tasks.get("needs_resume", 0))}
    {metric_tile("Review backlog", nested_get(review, "backlog", "total"))}
    {metric_tile("Accepted unapplied", review.get("accepted_unapplied", 0))}
    {metric_tile("Failed or blocked", failures.get("failed_or_blocked", 0))}
    {metric_tile("Running stale", running.get("stale_progress", 0))}
  </section>
  <section aria-labelledby="status">
    <h2 id="status">Tasks by status</h2>
    <ul class="status-list">{by_status_html}</ul>
  </section>
  <section aria-labelledby="source">
    <h2 id="source">Data source</h2>
    <p>Source: <strong>{escape_text(overview.get("data_source"))}</strong>; fallback used: <strong>{escape_text(overview.get("fallback_used"))}</strong>.</p>
    <p>Recent events: <strong>{escape_text(recent_events.get("recent_count", 0))}</strong>.</p>
    <p>Cooldown active: global <strong>{escape_text(nested_get(cooldowns, "global", "active"))}</strong>; reviewer <strong>{escape_text(nested_get(cooldowns, "reviewer_codex", "active"))}</strong>.</p>
  </section>
</main>
</body>
</html>
"""


def metric_tile(label: str, value: Any) -> str:
    return f'<div class="tile"><div class="label">{escape_text(label)}</div><div class="value">{escape_text(value)}</div></div>'


def nested_get(data: dict[str, Any], *keys: str) -> Any:
    current: Any = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def escape_text(value: Any) -> str:
    return html.escape(str(value), quote=True)
