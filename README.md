# codex-batch-runner

`codex-batch-runner`는 Codex CLI 작업을 로컬 파일 큐에서 하나씩 실행하는 배치 runner입니다. 스케줄러가 자주 실행되더라도 처리할 작업이 있을 때만 `codex exec --json` 또는 `codex exec resume ... --json`을 호출하여 불필요한 Codex 토큰 소모를 줄입니다.

## 현재 상태

현재는 로컬 beta 운영을 목표로 core flow를 구현하고 있습니다. 파일 기반 queue, lock, cooldown, Codex JSONL parsing, 자동 검토, bounded auto-fix, shell task backend, worktree 격리 실행, worktree apply/rebase/cleanup/branch-prune 흐름을 포함합니다.

실제 Codex CLI JSONL schema는 버전별 차이가 있을 수 있으므로 runner는 원본 JSONL 로그를 보존하고, 최종 응답과 session/thread id는 best-effort로 파싱합니다.

## 문서 지도

- [Core spec index](docs/spec.md): 현재 구현 truth와 topic 문서 index.
- [Task schema and dependency contract](docs/task-schema.md): task JSON fields, task/review status, dependency readiness.
- [Execution contract](docs/execution.md): execution profiles, capacity/priority, shell backend, runner policy, watchdog, lock, atomic write, Codex command/prompt wrapper, rate-limit, queue mutation.
- [Review contract](docs/review.md): review-bundle, review-next, reviewer Codex gates, bounded auto-fix loop.
- [Worktree contract](docs/worktrees.md): worktree prepare/apply/rebase/conflict-fix/cleanup/branch-prune/recovery.
- [Events and index contract](docs/events-and-index.md): event log, local SQLite read index, prune/retention.
- [CLI reference](docs/cli-reference.md): command reference and human/JSON output semantics, including list renderer and routing-report details.
- [Operator installation guide](docs/operator-installation.md): config discovery, macOS launchd setup, doctor, cooldown wake, cross-project usage.
- [Beta operations guide](docs/beta-operations.md): practical beta operating model and review workflow.

Private project planning notes should use an ignored `.private/` directory, for example `.private/ROADMAP.md` and `.private/TASKS.md`; templates are in [examples/private](examples/private/).

## 설치

Python 3.11 이상이 필요합니다. runtime dependency는 없습니다.

개발 checkout에서 바로 실행할 수 있습니다.

```bash
PYTHONPATH=src python3 -m codex_batch_runner --help
```

운영자가 직접 상태를 조회하거나 transcript를 검토하고 `accept`/`reject`를 기록하는 환경에서는 `cbr` console script 설치가 편리합니다.

```bash
python3 -m pip install -e .
cbr --help
```

테스트 실행:

```bash
PYTHONPATH=src python3 -m unittest discover -v
```

## 5-minute quickstart

작업 등록:

```bash
PYTHONPATH=src python3 -m codex_batch_runner enqueue \
  --cwd /path/to/repo \
  --title "README 개선" \
  --prompt "README를 개선하고 테스트를 실행해"
```

다음 실행 가능한 작업 하나 처리:

```bash
PYTHONPATH=src python3 -m codex_batch_runner run-next
```

상태 확인:

```bash
PYTHONPATH=src python3 -m codex_batch_runner list
PYTHONPATH=src python3 -m codex_batch_runner summary task-id
PYTHONPATH=src python3 -m codex_batch_runner review-bundle task-id
```

검토 결과 기록:

```bash
PYTHONPATH=src python3 -m codex_batch_runner accept task-id --reason "verified locally"
PYTHONPATH=src python3 -m codex_batch_runner reject task-id --reason "tests are missing"
PYTHONPATH=src python3 -m codex_batch_runner resolve task-id --resolution manual --reason "handled outside cbr"
```

작업이 완료됐지만 후속 실행이 필요하면 Codex final JSON의 `status`는 `needs_resume`이 될 수 있습니다. Rate-limit은 runner가 stderr/JSONL evidence에서 별도로 감지하고 task/global cooldown을 설정합니다.

## Core workflow

1. `enqueue`가 task JSON을 local queue에 저장합니다.
2. Scheduler가 `run-next`를 반복 호출합니다.
3. Runner가 lock, pause, cooldown, dependency, capacity gate를 확인합니다.
4. 실행 가능한 task 하나를 Codex 또는 shell backend로 실행합니다.
5. 결과와 sanitized event를 저장합니다.
6. 운영자 또는 opt-in review automation이 `summary`, `review-bundle`, `review-next`를 기준으로 `accept`/`reject`/`needs_followup`을 기록합니다.
7. Worktree-backed accepted result는 `worktree apply` 또는 post-accept apply path로 integration target에 반영된 뒤 dependency-ready가 됩니다.

기본 운영에서는 다른 Codex thread나 사람이 작업을 queue에 등록하고, launchd/systemd 같은 외부 scheduler가 `run-next`를 호출합니다. 자동화 경로에서는 `PATH`에 의존하지 않고 config와 절대 경로를 사용하는 것이 안전합니다.

## Common commands

```bash
# Register from a prompt file
PYTHONPATH=src python3 -m codex_batch_runner enqueue --cwd /path/to/repo --prompt-file task.md

# Register with metadata and routing hints
PYTHONPATH=src python3 -m codex_batch_runner enqueue \
  --cwd /path/to/repo \
  --project codex-batch-runner \
  --category implementation \
  --label queue \
  --profile small \
  --routing-size small \
  --routing-risk low \
  --verification-scope unit \
  --prompt-file task.md

# Inspect queue and logs
PYTHONPATH=src python3 -m codex_batch_runner list --project codex-batch-runner
PYTHONPATH=src python3 -m codex_batch_runner follow task-id --lines 40 --poll-interval 1
PYTHONPATH=src python3 -m codex_batch_runner transcript task-id
PYTHONPATH=src python3 -m codex_batch_runner events --task-id task-id --limit 10

# Review automation reports
PYTHONPATH=src python3 -m codex_batch_runner review-next --dry-run
PYTHONPATH=src python3 -m codex_batch_runner review-next --dry-run --project codex-batch-runner --json

# Worktree integration
PYTHONPATH=src python3 -m codex_batch_runner worktree apply task-id --dry-run
PYTHONPATH=src python3 -m codex_batch_runner worktree cleanup task-id --dry-run
PYTHONPATH=src python3 -m codex_batch_runner worktree branch-prune task-id --dry-run
```

Detailed command semantics are in [docs/cli-reference.md](docs/cli-reference.md). Worktree apply/rebase/cleanup/branch-prune safety rules are in [docs/worktrees.md](docs/worktrees.md).

## Configuration

Config discovery order:

1. `--config path/to/config.json`
2. `CBR_CONFIG` environment variable

If neither is provided, `cbr` exits with an error instead of creating a runtime directory under the current working directory. Example configs are available in [examples/config.example.json](examples/config.example.json) and [examples/config.automation.example.json](examples/config.automation.example.json).

Optional `root` makes relative runtime paths independent of the process current working directory. `worktree_mode=task` enables task-specific git worktrees. `execution_profiles`, task-level `--profile`, `--model`, `--codex-profile`, and allowlisted `--config-override` values provide cost and routing hints without changing the correctness contract.

For launchd/systemd installation, config discovery, and `doctor`, use [docs/operator-installation.md](docs/operator-installation.md). For execution policy and full config contracts, use [docs/execution.md](docs/execution.md).

## Safety model

`run-next` handles one unit of work per invocation and avoids Codex calls when the queue is empty, another runner holds the lock, global cooldown is active, dependencies are not ready, task cooldown is active, capacity is full, or runner pause is active.

Task/state writes use atomic replace. Core state-changing commands append sanitized audit events. Event payloads are intentionally small and redact prompt text, raw transcripts, session/thread ids, secrets, credentials, and token-like fields.

`review-next` is report-only by default. Mechanical accept, reviewer Codex, bounded auto-fix enqueue, worktree apply, cleanup, branch pruning, pruning retained files, and Codex CLI maintenance all require explicit command/config opt-in. `worktree branch-prune --apply` only deletes eligible cleaned applied local `cbr/*` branches with `git branch -d`; it does not delete remote branches, task JSON, runtime logs, event logs, worktree directories, or force-delete branches.

## 운영 메모

macOS에서는 launchd 운영을 권장합니다. Linux user service에서는 systemd timer/service를 사용할 수 있고, cron은 portable fallback으로만 취급합니다. Installation examples are maintained in [docs/operator-installation.md](docs/operator-installation.md).

Public docs must not include private/operator state, `.private/` contents, runtime logs, actual Codex prompts, session ids, thread ids, personal paths, credentials, or `.local` files.
