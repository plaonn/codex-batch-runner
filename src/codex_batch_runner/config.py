from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .fs import read_json


@dataclass(frozen=True)
class Config:
    root: Path
    queue_dir: Path
    log_dir: Path
    lock_file: Path
    state_file: Path
    codex_command: list[str]
    codex_resume_command: list[str]
    stale_lock_seconds: int
    rate_limit_cooldown_seconds: int
    default_max_attempts: int

    @classmethod
    def load(cls, config_path: str | None = None, root: Path | None = None) -> "Config":
        base = (root or Path.cwd()).resolve()
        data: dict[str, Any] = {}
        if config_path:
            data = read_json(Path(config_path).expanduser().resolve(), {}) or {}

        def path_value(key: str, default: str) -> Path:
            raw = Path(data.get(key, default)).expanduser()
            return raw if raw.is_absolute() else base / raw

        return cls(
            root=base,
            queue_dir=path_value("queue_dir", ".codex-batch-runner/tasks"),
            log_dir=path_value("log_dir", ".codex-batch-runner/logs"),
            lock_file=path_value("lock_file", ".codex-batch-runner/runner.lock"),
            state_file=path_value("state_file", ".codex-batch-runner/state.json"),
            codex_command=list(data.get("codex_command", ["codex", "exec", "--json"])),
            codex_resume_command=list(
                data.get("codex_resume_command", ["codex", "exec", "resume", "{session_id}", "--json"])
            ),
            stale_lock_seconds=int(data.get("stale_lock_seconds", 21600)),
            rate_limit_cooldown_seconds=int(data.get("rate_limit_cooldown_seconds", 1800)),
            default_max_attempts=int(data.get("default_max_attempts", 5)),
        )
