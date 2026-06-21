from __future__ import annotations

import subprocess
import sys

from .config import Config

POST_MUTATION_TRIGGER_TIMEOUT_SECONDS = 5


def run_post_mutation_trigger(config: Config) -> None:
    run_trigger_command(config, "post-mutation trigger")


def run_post_run_trigger(config: Config) -> None:
    run_trigger_command(config, "post-run trigger")


def run_trigger_command(config: Config, label: str) -> None:
    command = config.post_mutation_trigger_command
    if not command:
        return
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=POST_MUTATION_TRIGGER_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        print(f"warning: {label} timed out", file=sys.stderr)
        return
    except OSError as exc:
        print(f"warning: {label} failed: {exc}", file=sys.stderr)
        return
    if result.returncode != 0:
        print(f"warning: {label} exited with status {result.returncode}", file=sys.stderr)
