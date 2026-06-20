# codex-batch-runner 스펙

이 문서는 `codex-batch-runner`의 구현 기준 문서임. README는 사용자용 설명에 집중하고, 구현 중 설계 판단이 바뀌면 이 문서를 먼저 갱신함.

## 목표

`codex-batch-runner`는 로컬 파일 기반 큐에 등록된 작업을 Codex CLI로 순차 처리하는 배치 runner임.

핵심 목표는 Codex CLI를 직접 주기적으로 호출하되, 처리할 작업이 없거나 지금 처리하면 안 되는 상태에서는 Codex를 호출하지 않아 불필요한 Codex 토큰 소모를 줄이는 것임.

runner는 cron, launchd, systemd처럼 외부 스케줄러가 자주 실행해도 안전해야 함. macOS에서는 launchd를 기본 운영 방식으로 문서화하고, cron은 portable fallback으로만 다룸.

## 비목표

- DB 기반 queue는 초기 구현 범위가 아님. SQLite가 명확히 유리해지기 전까지 JSON/JSONL 파일 기반으로 시작함.
- Codex usage remaining을 안정적으로 조회할 수 있다고 가정하지 않음.
- ChromeDriver, browser UI scraping, GUI automation은 core runner에 넣지 않음. 추후 optional adapter 여지는 남김.
- rate-limit reset 시간이 항상 공식적으로 제공된다고 가정하지 않음.
- runner가 코드 diff를 분석해 작업 완료/부분 완료를 자체 판단하지 않음.

## 구현 선택

초기 구현은 Python 표준 라이브러리 기반 CLI로 작성함.

이유:

- 현재 repo에 기존 Node/Python 패턴이 없음.
- 핵심 기능이 파일 I/O, atomic write, lock, subprocess, JSONL parsing, timestamp 처리임.
- runtime dependency 없이 구현 가능함.
- Python dependency/version 관리 약점은 `pyproject.toml`에 Python 버전을 명시하고 외부 runtime dependency를 두지 않는 방식으로 줄임.

권장 기준:

- Python `>=3.11`
- runtime dependency 없음
- CLI entrypoint 제공
- 테스트는 표준 `unittest` 또는 최소 dev dependency로 시작

## 문서 정책

이 프로젝트의 Markdown 문서는 기본적으로 한국어로 작성함.

- README: 한국어 사용자 문서
- 이 스펙: 한국어 구현 기준 문서
- 예시 roadmap/task 템플릿: 한국어
- CLI option, config key, JSON schema field, log key, test name은 영어 유지

## 저장소 공개 운영 정책

이 저장소는 공개 저장소로 운영함.

- 로컬 runtime state, 실제 queue, 실제 로그, 개인 작업 메모는 commit하지 않음.
- 실제 Codex prompt, JSONL 로그, session id, thread id, usage-limit 메시지는 commit하지 않음.
- 테스트 fixture는 sanitized synthetic data만 사용함.
- `AGENTS.md`는 로컬 개인 지침으로 gitignore 처리함.

## 파일 구조

초기 목표 구조:

```text
codex-batch-runner/
  README.md
  .gitignore
  pyproject.toml
  docs/
    spec.md
  src/
    codex_batch_runner/
      __init__.py
      cli.py
      config.py
      lock.py
      queue.py
      runner.py
      codex.py
      prompts.py
      limits.py
  tests/
    test_queue.py
    test_lock.py
    test_runner.py
  examples/
    config.example.json
    task.example.json
    ROADMAP.local.example.md
```

실제 runtime state는 기본적으로 아래에 둠.

```text
.codex-batch-runner/
  tasks/
  logs/
  rate-limits/
  runner.lock
  state.json
```

`.codex-batch-runner/`는 gitignore 대상임.

## Task schema

각 task는 사람이 읽을 수 있는 개별 JSON 파일로 저장함.

```json
{
  "id": "task-20260620-001",
  "status": "runnable",
  "review_status": null,
  "reviewed_at": null,
  "review_reason": null,
  "prompt": "작업 지시문",
  "next_prompt": null,
  "cwd": "/path/to/repo",
  "session_id": null,
  "thread_id": null,
  "depends_on": [],
  "attempts": 0,
  "max_attempts": 5,
  "cooldown_until": null,
  "last_error": null,
  "created_at": "2026-06-20T12:00:00+09:00",
  "updated_at": "2026-06-20T12:00:00+09:00",
  "started_at": null,
  "completed_at": null,
  "log_paths": []
}
```

필수 필드:

- `id`
- `status`
- `prompt`
- `cwd`
- `depends_on`
- `attempts`
- `created_at`
- `updated_at`

선택 필드:

- `next_prompt`
- `session_id`
- `thread_id`
- `max_attempts`
- `cooldown_until`
- `last_error`
- `started_at`
- `completed_at`
- `log_paths`
- `review_status`
- `reviewed_at`
- `review_reason`

## Task status

초기 status:

- `runnable`: 실행 가능
- `running`: runner가 현재 처리 중
- `needs_resume`: Codex가 후속 실행 필요를 보고함
- `completed`: 완료
- `blocked_user`: 사용자 입력 필요
- `failed`: 실패
- `archived`: 운영 목록에서 숨긴 보관 task

`cooldown`은 status로 고정하지 않음. `cooldown_until`이 미래이면 해당 task는 실행 후보에서 제외함.

`archived`는 완료/실패/blocked task를 삭제하지 않고 운영 목록에서 숨기기 위한 상태임. archive 전 상태는 `previous_status`, archive 시각은 `archived_at`에 저장함.

## Review status

`status=completed`는 Codex 실행이 완료되었다는 의미이며, 운영자가 결과를 검토했다는 의미는 아님.

검토 상태는 `review_status`로 별도 기록함.

- `null`: 아직 실행 완료 전이거나 검토 대상이 아님
- `unreviewed`: 실행 완료 후 검토 대기
- `accepted`: 검토 후 완료 인정
- `rejected`: 검토 후 완료 불인정
- `needs_followup`: 후속 작업 필요

runner는 Codex 최종 응답이 `completed`이면 `review_status=unreviewed`를 설정함. 운영자나 관련 프로젝트의 Codex thread는 `cbr transcript`, `cbr show`, 필요한 테스트 결과를 확인한 뒤 `cbr accept` 또는 `cbr reject`로 진짜 완료 여부를 기록함.

## Dependency policy

task 등록 시 `depends_on`으로 의존 task id를 명시할 수 있음.

runner는 아래 조건을 모두 만족하는 task 하나만 실행함.

- `status`가 `runnable` 또는 `needs_resume`
- `cooldown_until`이 없거나 현재 시각 이전
- 모든 `depends_on` task의 status가 `completed`
- global cooldown 상태가 아님

실행 가능한 task가 없으면 Codex를 호출하지 않고 즉시 종료함.

의존 task가 `failed` 또는 `blocked_user`인 경우 dependent task를 자동 실패시키지 않음. `list` 또는 `show`에서 dependency blocked 상태를 표시하고 runner는 해당 task를 건너뜀.

## Runner execution policy

`run-next`는 1회 실행당 runnable task 하나만 처리함.

흐름:

1. config 로드
2. global cooldown 확인
3. lock 획득 시도
4. active lock이 있으면 즉시 종료
5. stale lock이면 복구 후 lock 재시도
6. 실행 가능한 task 하나 선택
7. 없으면 즉시 종료
8. task를 `running`으로 atomic update
9. Codex prompt wrapper 생성
10. 실제 작업이 있을 때만 Codex CLI 호출
11. Codex JSONL stdout을 attempt별 로그 파일에 저장
12. `turn.completed`, `turn.failed`, `error` event 파싱
13. 최종 JSON 응답 파싱
14. task 상태 갱신
15. lock 해제

## Lock policy

동시 실행 방지는 lock file로 처리함.

기본 lock path:

```text
.codex-batch-runner/runner.lock
```

lock 획득은 atomic create를 사용함.

- `O_CREAT | O_EXCL`
- 성공하면 lock 보유
- 이미 있으면 lock metadata와 age 확인
- stale 기준을 초과하면 stale lock으로 보고 복구 시도
- stale 제거 전 pid 생존 확인은 best-effort로만 수행

lock 파일 예:

```json
{
  "pid": 12345,
  "hostname": "host",
  "created_at": "2026-06-20T12:00:00+09:00",
  "task_id": "task-20260620-001"
}
```

기본 stale 기준은 긴 Codex 작업을 고려해 6시간으로 시작함.

lock 복구 후 `running` 상태의 task가 stale 기준보다 오래됐으면 다음 실행에서 다시 `runnable` 또는 `needs_resume`으로 되돌림. 실제 Codex가 아직 실행 중인 task를 중복 실행하지 않도록 stale 기준은 보수적으로 길게 둠.

## Atomic write policy

task와 state 갱신은 atomic write로 처리함.

1. 같은 디렉터리에 임시 파일 작성
2. flush/fsync
3. `os.replace(tmp, target)`

Codex JSONL 로그는 attempt별 새 파일로 저장함. 중단되더라도 partial JSONL을 사람이 확인할 수 있어야 함.

## Codex command policy

신규 실행 기본 형태:

```bash
codex exec --sandbox workspace-write --json "<wrapped prompt>"
```

resume 실행 기본 형태:

```bash
codex exec --sandbox workspace-write resume "<session_id>" --json "<wrapped prompt>"
```

실제 CLI 문법 차이에 대비해 config에서 command template를 제공함.

```json
{
  "codex_command": ["codex", "exec", "--sandbox", "workspace-write", "--json"],
  "codex_resume_command": ["codex", "exec", "--sandbox", "workspace-write", "resume", "{session_id}", "--json"]
}
```

`workspace-write`를 기본으로 둠. non-interactive batch 작업은 일반적으로 파일 수정을 해야 하며, read-only sandbox에서는 수정 task가 실패함.

launchd 같은 scheduler는 사용자 shell `PATH`를 그대로 상속하지 않을 수 있음. 운영 config에서는 `codex` 실행 파일을 절대 경로로 지정할 수 있어야 함.

Codex CLI 0.136 JSONL은 `thread.started.thread_id`를 내보내며, 이 값은 `codex exec resume <thread_id>`에 사용할 수 있음. runner는 명시적인 `session_id`가 없으면 `thread_id`를 resume id fallback으로 저장함.

`needs_resume`인데 resume id를 찾지 못하면 신규 `codex exec`로 이어가되, prompt wrapper에 이전 summary와 `next_prompt`를 포함함. 이 경우 task/log metadata에 `resume_unavailable: true`를 남김.

## Prompt wrapper contract

runner는 task prompt를 그대로 넘기지 않고 wrapper를 붙여 전달함.

wrapper 요구사항:

- 한 번에 task 하나만 처리
- 임의로 새 task를 만들지 않음
- task id를 유지
- 최종 응답은 JSON object만 반환
- 완료하지 못하면 `needs_resume`과 `next_prompt` 반환
- 사용자 입력이 필요하면 `blocked_user` 반환

최종 응답 schema:

```json
{
  "task_id": "string",
  "status": "completed | needs_resume | blocked_user | failed",
  "summary": "string",
  "next_prompt": "string",
  "changed_files": ["string"],
  "verification": ["string"]
}
```

## Partial completion policy

runner는 partial completion을 자체 추론하지 않음.

`needs_resume` 판단 주체는 Codex의 최종 JSON 응답임.

- `completed`: task 완료
- `needs_resume`: task 유지, `next_prompt` 저장
- `blocked_user`: 자동 재시도 중단
- `failed`: 실패 처리

Codex process가 rate-limit으로 실패하면 정상 final JSON까지 도달하지 못할 수 있음. 이 경우 runner가 로그와 stderr에서 rate-limit을 감지하고 cooldown만 설정함.

## Rate-limit policy

Codex usage remaining을 안정적으로 조회할 수 있다고 가정하지 않음.

rate-limit/usage-limit 감지 대상:

- JSONL `error` event
- JSONL `turn.failed` event
- stderr
- process output의 error text

감지 문자열 예:

- `rate limit`
- `rate-limit`
- `usage limit`
- `usage-limit`
- `too many requests`
- `429`
- `quota`
- `try again`

rate-limit으로 판단되면:

- task는 실패 처리하지 않음
- resume id가 있으면 task 상태를 `needs_resume`으로 되돌림
- resume id가 없으면 task 상태를 기존처럼 `runnable`으로 되돌림
- `cooldown_until`을 설정함
- global cooldown을 설정함
- 다음 cooldown 전까지 Codex를 호출하지 않음
- cooldown 만료 후 resume id가 있으면 이전 Codex thread를 resume함
- sanitized rate-limit evidence event를 별도 JSON으로 저장함

정상 final JSON 응답이 파싱되면 final JSON의 status를 우선함. Codex stderr에는 plugin warning 같은 비치명적 경고가 섞일 수 있으므로, final JSON 없이 실패한 실행에서만 rate-limit cooldown을 적용함.

rate-limit evidence event는 runtime directory의 `rate-limits/` 아래에 attempt별 JSON으로 저장함. prompt, 전체 JSONL, session/thread id, secrets를 저장하지 않음. 저장 대상은 task id, detected_at, attempt, matched markers, cooldown_until, 짧은 stderr/error excerpt, 원본 log path 정도로 제한함.

초기 기본 정책:

- launchd는 10분마다 runner 실행
- 평상시에는 runnable task가 있으면 1개 처리
- rate-limit 발생 시 task/global cooldown을 30분으로 설정
- reset 시간이 명확히 파싱되면 그 값을 사용할 수 있으나, 기본은 고정 cooldown

`rate_limit_count`는 초기 필수 필드로 두지 않음. 실제 reset 시점 예측에는 큰 도움이 없고, 운영 모델은 “실패 후 성공할 때까지 주기를 늘림”에 집중함.

## Global state

global state는 사람이 읽을 수 있는 JSON으로 저장함.

기본 path:

```text
.codex-batch-runner/state.json
```

예:

```json
{
  "global_cooldown_until": null,
  "last_rate_limit_at": null,
  "last_run_at": null,
  "last_success_at": null,
  "last_task_id": null
}
```

## CLI

초기 CLI:

```bash
cbr enqueue --cwd /repo --prompt-file prompt.md
cbr enqueue --cwd /repo --prompt "작업 지시문"
cbr list
cbr run-next
cbr show TASK_ID
cbr logs TASK_ID
cbr transcript TASK_ID
cbr archive TASK_ID
cbr accept TASK_ID --reason "verified"
cbr reject TASK_ID --reason "missing tests"
cbr list --all
cbr rate-limits
```

공통 option:

```bash
--config path/to/config.json
```

`cbr list` 기본 출력은 운영자가 신경 써야 할 task 중심으로 유지함. `completed`와 `archived`는 기본 출력에서 숨기고, 전체 조회가 필요하면 `--all`을 사용함. `failed` task는 한 줄짜리 `last_error` 요약을 함께 표시함.

`cbr archive TASK_ID`는 task 파일을 삭제하지 않고 `status=archived`, `previous_status`, `archived_at`을 기록함.

`cbr transcript TASK_ID`는 저장된 cbr JSONL 로그와, `session_id` 또는 `thread_id`로 찾을 수 있는 Codex 원본 세션 로그에서 주요 대화, tool 호출, patch, final/error event를 사람이 읽기 좋은 형태로 재구성함. `--raw`는 원본 JSONL을 출력함.

`cbr accept TASK_ID`는 `review_status=accepted`를 기록함. `cbr reject TASK_ID`는 `review_status=rejected`를 기록하고, `--follow-up`을 붙이면 `review_status=needs_followup`을 기록함.

`cbr rate-limits`는 저장된 sanitized rate-limit evidence event를 조회함. `--json`을 붙이면 evidence JSON 배열을 출력함.

## Future local web dashboard

향후 read-only local web dashboard를 둘 수 있음.

초기 방향:

- `python -m codex_batch_runner web`
- localhost 전용
- 별도 DB 없이 기존 task/state/log/rate-limit evidence JSON을 읽음
- task table, status counts, review status, dependency graph, task detail, transcript, rate-limit events를 표시함
- 초기 버전에는 write action을 넣지 않음

## macOS launchd 운영

macOS 기본 운영 방식은 launchd임.

권장 모델:

- `StartInterval = 600`
- runner 내부에서 lock, dependency, cooldown, empty queue를 판단
- 실행할 작업이 없으면 즉시 종료
- rate-limit 발생 시 launchd interval을 바꾸지 않고 runner 내부 global cooldown으로 Codex 호출을 막음

cron 예시는 portable fallback으로만 문서화함.

## 최소 테스트

초기 테스트 범위:

- enqueue가 task JSON 생성
- dependency가 완료되지 않은 task는 실행 후보에서 제외
- cooldown 중인 task는 실행 후보에서 제외
- lock acquire/release
- stale lock 복구
- fake Codex success JSONL 처리
- fake Codex `needs_resume` JSON 처리
- fake Codex rate-limit error 처리와 cooldown 설정
- malformed final JSON 처리

fake Codex는 실제 Codex CLI를 호출하지 않고 synthetic JSONL을 stdout으로 출력하는 helper를 사용함.
