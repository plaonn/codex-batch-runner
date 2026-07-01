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
    review_backlog = dict(review.get("backlog") or {})
    by_status_html = "".join(
        f"<li><span>{escape_text(status)}</span><strong>{escape_text(count)}</strong></li>"
        for status, count in sorted(by_status.items())
    ) or "<li><span>none</span><strong>0</strong></li>"
    review_status_html = compact_kv_list(review_backlog.get("by_review_status") or {}, empty_label="none")
    event_type_html = compact_kv_list(recent_events.get("by_type") or {}, empty_label="none")
    recent_event_rows = "".join(render_recent_event_row(event) for event in list(recent_events.get("recent") or []))
    if not recent_event_rows:
        recent_event_rows = '<tr><td colspan="4">No recent events</td></tr>'
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>cbr dashboard</title>
  <style>
    :root {{ color-scheme: light dark; font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; --border: #9aa0a6; --muted: #5f6368; --surface: #f8f9fa; --strong: #202124; --warn: #8a5a00; }}
    @media (prefers-color-scheme: dark) {{ :root {{ --border: #5f6368; --muted: #bdc1c6; --surface: #202124; --strong: #f1f3f4; --warn: #fdd663; }} }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; padding: 18px; line-height: 1.35; color: var(--strong); }}
    main {{ max-width: 1180px; margin: 0 auto; }}
    header {{ display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; border-bottom: 1px solid var(--border); padding-bottom: 12px; margin-bottom: 14px; }}
    h1 {{ margin: 0 0 4px; font-size: 24px; }}
    h2 {{ margin: 0 0 10px; font-size: 16px; }}
    .meta, .label, .hint-label {{ color: var(--muted); }}
    .meta {{ font-size: 13px; }}
    .source {{ text-align: right; min-width: 180px; }}
    .section {{ border-top: 1px solid var(--border); padding-top: 14px; margin-top: 14px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; }}
    .tile {{ border: 1px solid var(--border); border-radius: 6px; padding: 10px; min-height: 86px; background: var(--surface); }}
    .tile .label {{ font-size: 12px; }}
    .value {{ font-size: 26px; font-weight: 700; margin-top: 4px; overflow-wrap: anywhere; }}
    .subvalue {{ color: var(--muted); font-size: 12px; margin-top: 4px; }}
    .columns {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 14px; align-items: start; }}
    ul {{ padding-left: 20px; margin: 8px 0 0; }}
    .warning-list {{ color: var(--warn); }}
    .status-list, .hint-list {{ list-style: none; padding-left: 0; }}
    .status-list li {{ display: flex; justify-content: space-between; gap: 14px; border-bottom: 1px solid var(--border); padding: 5px 0; }}
    .hint-list li {{ margin: 6px 0; }}
    code {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.95em; overflow-wrap: anywhere; }}
    table {{ width: 100%; border-collapse: collapse; table-layout: fixed; }}
    th, td {{ border-bottom: 1px solid var(--border); padding: 7px 6px; text-align: left; vertical-align: top; overflow-wrap: anywhere; }}
    th {{ color: var(--muted); font-size: 12px; font-weight: 600; }}
    @media (max-width: 620px) {{ body {{ padding: 12px; }} header {{ display: block; }} .source {{ text-align: left; margin-top: 8px; }} .grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }} th, td {{ font-size: 13px; }} }}
  </style>
</head>
<body>
<main>
  <header>
    <div>
      <h1>cbr dashboard</h1>
      <div class="meta">Local read-only operator overview. API: <code>/api/dashboard</code></div>
    </div>
    <div class="meta source">Source: <strong>{escape_text(overview.get("data_source"))}</strong><br>Fallback: <strong>{escape_text(overview.get("fallback_used"))}</strong></div>
  </header>
  <section class="section" aria-labelledby="warnings">
    <h2 id="warnings">Index warnings</h2>
    <ul class="warning-list">{warning_html}</ul>
  </section>
  <section class="section" aria-labelledby="queue-overview">
    <h2 id="queue-overview">Queue overview</h2>
    <div class="grid">
    {metric_tile("Total tasks", tasks.get("total", 0))}
    {metric_tile("Active tasks", tasks.get("active", 0))}
    {metric_tile("Runnable", tasks.get("runnable", 0))}
    {metric_tile("Needs resume", tasks.get("needs_resume", 0))}
    {metric_tile("Running total", nested_get(tasks, "capacity", "running_total"), f"capacity {nested_get(tasks, 'capacity', 'max_total_running')} total / {nested_get(tasks, 'capacity', 'max_running_per_project')} per project")}
    </div>
  </section>
  <section class="section" aria-labelledby="operator-attention">
    <h2 id="operator-attention">Operator attention</h2>
    <div class="grid">
    {metric_tile("Review needed", review_backlog.get("total", 0))}
    {metric_tile("Accepted unapplied", review.get("accepted_unapplied", 0))}
    {metric_tile("Failed or blocked", failures.get("failed_or_blocked", 0), f"failed {failures.get('failed', 0)} / blocked {failures.get('blocked_user', 0)} / usage {failures.get('usage_exhausted', 0)}")}
    {metric_tile("Running stale progress", running.get("stale_progress", 0), f"running {running.get('total', 0)}")}
    {metric_tile("Global cooldown", active_label(nested_get(cooldowns, "global", "active")), cooldown_detail(cooldowns.get("global")))}
    {metric_tile("Reviewer rate-limit", active_label(nested_get(cooldowns, "reviewer_codex", "active")), cooldown_detail(cooldowns.get("reviewer_codex")))}
    </div>
  </section>
  <section class="section columns" aria-label="breakdowns">
    <div>
      <h2 id="status">Tasks by status</h2>
      <ul class="status-list">{by_status_html}</ul>
    </div>
    <div>
      <h2 id="review-status">Review backlog</h2>
      <ul class="status-list">{review_status_html}</ul>
    </div>
    <div>
      <h2 id="event-types">Recent event types</h2>
      <ul class="status-list">{event_type_html}</ul>
    </div>
  </section>
  <section class="section" aria-labelledby="command-hints">
    <h2 id="command-hints">CLI command hints</h2>
    <ul class="hint-list">
      <li><span class="hint-label">Review backlog:</span> <code>cbr list --needs-review</code> or <code>cbr review-next --dry-run</code></li>
      <li><span class="hint-label">Accepted unapplied:</span> <code>cbr worktree apply TASK_ID --dry-run</code></li>
      <li><span class="hint-label">Events:</span> <code>cbr events --limit 10</code></li>
      <li><span class="hint-label">Index warning:</span> <code>cbr index rebuild --dry-run</code></li>
    </ul>
  </section>
  <section class="section" aria-labelledby="recent-events">
    <h2 id="recent-events">Recent sanitized events</h2>
    <table>
      <thead><tr><th>Occurred</th><th>Event</th><th>Task</th><th>Project</th></tr></thead>
      <tbody>{recent_event_rows}</tbody>
    </table>
  </section>
</main>
</body>
</html>
"""


def metric_tile(label: str, value: Any, subvalue: Any | None = None) -> str:
    subvalue_html = f'<div class="subvalue">{escape_text(subvalue)}</div>' if subvalue is not None else ""
    return (
        f'<div class="tile"><div class="label">{escape_text(label)}</div>'
        f'<div class="value">{escape_text(value)}</div>{subvalue_html}</div>'
    )


def compact_kv_list(values: dict[str, Any], *, empty_label: str) -> str:
    if not values:
        return f"<li><span>{escape_text(empty_label)}</span><strong>0</strong></li>"
    return "".join(
        f"<li><span>{escape_text(label)}</span><strong>{escape_text(count)}</strong></li>"
        for label, count in sorted(values.items())
    )


def render_recent_event_row(event: Any) -> str:
    event_data = event if isinstance(event, dict) else {}
    return (
        "<tr>"
        f"<td>{escape_text(event_data.get('occurred_at') or '-')}</td>"
        f"<td>{escape_text(event_data.get('event_type') or 'unknown')}</td>"
        f"<td>{escape_text(event_data.get('task_id') or '-')}</td>"
        f"<td>{escape_text(event_data.get('project_id') or '-')}</td>"
        "</tr>"
    )


def active_label(value: Any) -> str:
    return "active" if bool(value) else "clear"


def cooldown_detail(entry: Any) -> str:
    data = entry if isinstance(entry, dict) else {}
    until = data.get("cooldown_until")
    last_rate_limit = data.get("last_rate_limit_at")
    parts = []
    if until:
        parts.append(f"until {until}")
    if last_rate_limit:
        parts.append(f"last rate-limit {last_rate_limit}")
    return "; ".join(parts) if parts else "no cooldown evidence"


def nested_get(data: dict[str, Any], *keys: str) -> Any:
    current: Any = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def escape_text(value: Any) -> str:
    return html.escape(str(value), quote=True)
