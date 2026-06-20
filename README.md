# codex-batch-runner

`codex-batch-runner`는 Codex CLI 작업을 로컬 파일 큐에서 하나씩 실행하는 배치 runner입니다.

목표는 스케줄러가 자주 실행되더라도 실제 처리할 작업이 있을 때만 `codex exec --json` 또는 `codex exec resume ... --json`을 호출하여 불필요한 Codex 토큰 소모를 줄이는 것입니다.

## 현재 상태

초기 구현 단계입니다. 실제 Codex CLI JSONL schema는 버전별 차이가 있을 수 있으므로 runner는 원본 JSONL 로그를 보존하고, 최종 응답과 session/thread id는 best-effort로 파싱합니다.

구현 기준은 [docs/spec.md](docs/spec.md)에 있습니다.

## 설치

Python 3.11 이상이 필요합니다. runtime dependency는 없습니다.

기본 운영에서는 다른 Codex thread가 전역 skill을 통해 작업을 queue에 등록하고 launchd/systemd 같은 스케줄러가 `run-next`를 호출하는 방식을 권장합니다. 자동화 경로에서는 `PATH`에 의존하지 않고 config와 절대 경로를 사용하는 것이 안전합니다.

개발 checkout에서 바로 실행할 수 있습니다.

```bash
PYTHONPATH=src python3 -m codex_batch_runner --help
```

운영자가 직접 상태를 조회하거나 transcript를 검토하고 `accept`/`reject`를 기록하는 환경에서는 `cbr` console script 설치가 편리합니다. 자동화의 필수 조건은 아닙니다.

```bash
python3 -m pip install -e .
cbr --help
```

테스트 실행:

```bash
PYTHONPATH=src python3 -m unittest discover -v
```

## 기본 사용법

작업 등록:

```bash
PYTHONPATH=src python3 -m codex_batch_runner enqueue --cwd /path/to/repo --prompt "README를 개선하고 테스트를 실행해"
```

프로젝트 metadata를 함께 지정할 수 있습니다:

```bash
PYTHONPATH=src python3 -m codex_batch_runner enqueue \
  --cwd /path/to/repo \
  --project codex-batch-runner \
  --category implementation \
  --label queue \
  --created-by operator \
  --prompt "README를 개선하고 테스트를 실행해"
```

prompt 파일로 등록:

```bash
PYTHONPATH=src python3 -m codex_batch_runner enqueue --cwd /path/to/repo --prompt-file task.md
```

의존성 있는 작업 등록:

```bash
PYTHONPATH=src python3 -m codex_batch_runner enqueue --cwd /path/to/repo --id task-a --prompt-file a.md
PYTHONPATH=src python3 -m codex_batch_runner enqueue --cwd /path/to/repo --id task-b --depends-on task-a --prompt-file b.md
```

목록 확인:

```bash
PYTHONPATH=src python3 -m codex_batch_runner list
PYTHONPATH=src python3 -m codex_batch_runner list --project codex-batch-runner
PYTHONPATH=src python3 -m codex_batch_runner list --project-root /path/to/repo
PYTHONPATH=src python3 -m codex_batch_runner list --category implementation
PYTHONPATH=src python3 -m codex_batch_runner list --label queue
PYTHONPATH=src python3 -m codex_batch_runner list --unreviewed
PYTHONPATH=src python3 -m codex_batch_runner list --needs-review
```

기본 `list` 출력은 tab-separated table입니다. 열은 `ID`, `STATUS`, `PROJECT`, `ATTEMPTS`, `DEPS`, `FLAGS`이며, `DEPS`는 쉼표로 연결한 dependency id 또는 `-`, `FLAGS`는 `cooldown`, `blocked_by=...`, `last_error=...`, `resolution=...`, `review=...` 같은 운영 표시 또는 `-`를 보여줍니다. 스크립트에서는 사람이 읽는 table 대신 `--json` 출력을 사용해야 합니다.

다음 실행 가능한 작업 하나 처리:

```bash
PYTHONPATH=src python3 -m codex_batch_runner run-next
```

작업 상세:

```bash
PYTHONPATH=src python3 -m codex_batch_runner show task-a
PYTHONPATH=src python3 -m codex_batch_runner summary task-a
```

로그 확인:

```bash
PYTHONPATH=src python3 -m codex_batch_runner logs task-a
PYTHONPATH=src python3 -m codex_batch_runner logs task-a --cat
```

실행 대화 요약 확인:

```bash
PYTHONPATH=src python3 -m codex_batch_runner transcript task-a
PYTHONPATH=src python3 -m codex_batch_runner transcript task-a --raw
```

`transcript`는 기본적으로 cbr JSONL 로그와, `session_id` 또는 `thread_id`로 찾을 수 있는 Codex 원본 세션 로그에서 사용자 메시지, assistant 메시지, tool 호출, patch, final/error event를 사람이 읽기 좋은 형태로 재구성합니다. `--raw`를 붙이면 원본 JSONL 로그를 출력합니다.

배치 결과 검토:

```bash
PYTHONPATH=src python3 -m codex_batch_runner accept task-a --reason "verified locally"
PYTHONPATH=src python3 -m codex_batch_runner reject task-a --reason "tests are missing"
PYTHONPATH=src python3 -m codex_batch_runner reject task-a --follow-up --reason "needs follow-up task"
PYTHONPATH=src python3 -m codex_batch_runner resolve task-a --resolution manual --reason "handled outside cbr"
```

Codex가 `completed`를 반환하면 실행은 완료되지만, 검토 상태는 `unreviewed`로 남습니다. 운영상 진짜 완료로 판단한 뒤 `accept`로 `review_status=accepted`를 기록하는 흐름을 권장합니다.

`failed` 또는 `blocked_user` task를 운영상 더 추적하지 않아도 되면 `resolve`로 `resolution`을 기록할 수 있습니다. resolution이 기록된 failed/blocked task는 기본 `list`에서 숨겨지고, `list --all`이나 `summary`에서 확인할 수 있습니다.

각 Codex 실행 뒤에는 task에 `last_run` metadata가 기록됩니다. 여기에는 `command_kind`, `returncode`, 시작/종료 시각, `duration_seconds`, 사용한 resume id, log path가 포함됩니다. `run_count`, `resume_count`, `rate_limit_count`, `failure_count` counters도 함께 유지됩니다.

rate-limit evidence 확인:

```bash
PYTHONPATH=src python3 -m codex_batch_runner rate-limits
PYTHONPATH=src python3 -m codex_batch_runner rate-limits --json
```

runner state 확인:

```bash
PYTHONPATH=src python3 -m codex_batch_runner state
```

beta health check:

```bash
PYTHONPATH=src python3 -m codex_batch_runner doctor
PYTHONPATH=src python3 -m codex_batch_runner doctor --json
```

`doctor`는 Codex를 실행하지 않고 config/runtime path, Codex command availability, global cooldown, active lock, task status counts, review/resolution/cooldown/runnable counts를 점검합니다. 다른 프로젝트에서 상세 transcript를 열기 전에 queue 상태를 낮은 비용으로 확인하는 용도입니다. error check가 있으면 non-zero로 종료하고, warning은 종료 코드를 실패로 만들지 않습니다.

## 설정

config 탐색 순서는 다음과 같습니다.

1. `--config path/to/config.json`
2. `CBR_CONFIG` 환경 변수
3. `~/.config/codex-batch-runner/config.json`
4. 현재 작업 디렉터리 기준 기본값

config가 없을 때의 기본 runtime 디렉터리는 현재 작업 디렉터리의 `.codex-batch-runner/`입니다. 이 디렉터리는 gitignore 대상입니다.

설정 파일 예시는 [examples/config.example.json](examples/config.example.json)에 있습니다.

```bash
PYTHONPATH=src python3 -m codex_batch_runner --config examples/config.example.json run-next
```

## macOS launchd 예시

macOS에서는 cron보다 launchd 운영을 기본으로 권장합니다. `StartInterval`은 평상시 실행 주기이고, rate-limit 이후의 긴 대기는 runner 내부 global cooldown으로 처리합니다.

예시 plist는 [examples/com.example.codex-batch-runner.plist](examples/com.example.codex-batch-runner.plist)에 있습니다.

설치 예:

```bash
cp examples/com.example.codex-batch-runner.plist ~/Library/LaunchAgents/com.example.codex-batch-runner.plist
launchctl load ~/Library/LaunchAgents/com.example.codex-batch-runner.plist
```

사용 전 plist의 `ProgramArguments`, `WorkingDirectory`, config 경로는 로컬 환경에 맞게 수정해야 합니다. 개인 수정본은 `*.local.plist` 이름으로 두면 gitignore됩니다.

launchd는 사용자 shell의 `PATH`를 그대로 상속하지 않습니다. `codex`가 `/Users/you/.local/bin/codex`처럼 기본 launchd `PATH` 밖에 있으면 config의 `codex_command`와 `codex_resume_command`에는 `codex` 대신 절대 경로를 사용하는 것이 안전합니다.

## cron fallback

cron을 써야 하는 환경에서는 아래처럼 실행할 수 있습니다.

```cron
*/10 * * * * cd /path/to/codex-batch-runner && /path/to/venv/bin/cbr run-next >> .codex-batch-runner/runner.log 2>&1
```

## 안전 모델

`run-next`는 한 번 실행될 때 task 하나만 처리합니다.

Codex를 호출하지 않는 조건:

- queue가 비어 있습니다.
- 다른 runner가 lock을 보유 중입니다.
- global cooldown 중입니다.
- 모든 task가 dependency blocked 상태입니다.
- 모든 runnable task가 task cooldown 중입니다.

동시 실행 방지는 `.codex-batch-runner/runner.lock` atomic create로 처리합니다. lock이 오래 남아 있으면 stale lock으로 보고 복구합니다. 기본 stale 기준은 6시간입니다.

task와 state 파일은 같은 디렉터리에 임시 파일을 쓴 뒤 `os.replace`로 교체합니다. Codex JSONL 로그는 attempt별 파일로 저장합니다.

## Rate-limit 처리

runner는 Codex usage remaining을 조회할 수 있다고 가정하지 않습니다.

Codex 출력이나 stderr에서 rate-limit/usage-limit로 보이는 실패를 감지하면:

- task를 실패 처리하지 않습니다.
- resume id가 있으면 task 상태를 `needs_resume`으로 돌려 cooldown 이후 이전 Codex thread를 resume합니다.
- resume id가 없으면 task 상태를 `runnable`로 돌려 기존처럼 새 실행으로 재시도합니다.
- `needs_resume` task에 `next_prompt`가 있지만 resume id가 없으면 runner는 신규 실행으로 이어가며 `resume_unavailable` metadata를 남깁니다.
- task `cooldown_until`을 설정합니다.
- global cooldown을 설정합니다.
- cooldown 전까지 Codex를 호출하지 않습니다.
- `.codex-batch-runner/rate-limits/` 아래에 sanitized evidence JSON을 저장합니다.

초기 기본값은 launchd 10분 주기, rate-limit 이후 30분 cooldown입니다.

rate-limit evidence에는 task id, detected time, attempt, matched markers, cooldown deadline, 짧은 stderr/error excerpt, 원본 log path만 저장합니다. prompt, 전체 JSONL, session/thread id, secrets는 저장하지 않습니다.

## Codex 최종 응답 계약

runner는 task prompt를 wrapper로 감싸 Codex에 넘깁니다. Codex는 마지막에 JSON object만 반환해야 합니다.

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

`needs_resume`은 사용량 부족 전용 상태가 아닙니다. Codex가 작업을 일부 진행했고 후속 실행이 필요하다고 판단할 때 반환하는 상태입니다. rate-limit은 runner가 실패 로그에서 별도로 감지합니다.

## 검토 모델

배치 작업의 `status=completed`는 Codex 실행이 완료되었다는 뜻입니다. 실제 작업 품질이 확인되었다는 뜻은 아닙니다.

검토 상태는 `review_status` 필드로 별도 관리합니다.

- `unreviewed`: Codex 실행은 완료되었지만 아직 검토되지 않았습니다.
- `accepted`: 운영자가 결과를 검토하고 완료로 인정했습니다.
- `rejected`: 결과를 완료로 인정하지 않았습니다.
- `needs_followup`: 후속 작업이 필요합니다.

관련 프로젝트에서 배치 결과를 점검할 때는 먼저 `summary`로 `last_result.summary`, changed files, verification, last_error를 확인합니다. 더 자세한 실행 대화가 필요하면 `transcript`와 `logs`, 필요한 테스트 명령을 함께 확인한 뒤 `accept` 또는 `reject`를 사용합니다.

여러 프로젝트가 하나의 중앙 queue를 공유하는 운영을 위해 project metadata와 review 대상 필터를 제공합니다. 관련 프로젝트에서는 `list --project-root /path/to/repo --needs-review`처럼 먼저 자기 작업만 좁힌 뒤, 필요한 task에 대해서만 `show`나 `transcript`를 읽는 흐름을 권장합니다. 세부 설계는 [docs/spec.md](docs/spec.md)의 project routing metadata와 operational triage plan을 기준으로 관리합니다.

## 로컬 작업 메모

공개 repo에 올리지 않을 작업 메모는 `ROADMAP.local.md`처럼 `*.local.md` 파일로 관리합니다. 템플릿은 [examples/ROADMAP.local.example.md](examples/ROADMAP.local.example.md)에 있습니다.
