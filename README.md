# codex-batch-runner

`codex-batch-runner`는 Codex CLI 작업을 로컬 파일 큐에서 하나씩 실행하는 배치 runner입니다.

목표는 스케줄러가 자주 실행되더라도 실제 처리할 작업이 있을 때만 `codex exec --json` 또는 `codex exec resume ... --json`을 호출하여 불필요한 Codex 토큰 소모를 줄이는 것입니다.

## 현재 상태

초기 구현 단계입니다. 실제 Codex CLI JSONL schema는 버전별 차이가 있을 수 있으므로 runner는 원본 JSONL 로그를 보존하고, 최종 응답과 session/thread id는 best-effort로 파싱합니다.

구현 기준은 [docs/spec.md](docs/spec.md)에 있습니다.

## 설치

Python 3.11 이상이 필요합니다. runtime dependency는 없습니다.

기본 운영에서는 사람이 `cbr`를 직접 자주 실행하기보다, 다른 Codex thread가 전역 skill을 통해 작업을 queue에 등록하고 launchd/systemd 같은 스케줄러가 `run-next`를 호출하는 방식을 권장합니다. CLI 직접 실행은 설치, 디버깅, 상태 확인을 위한 도구로 보는 것이 안전합니다.

개발 checkout에서 바로 실행할 수 있습니다.

```bash
PYTHONPATH=src python3 -m codex_batch_runner --help
```

`cbr` console script 설치는 선택 사항입니다. 개발 편의가 필요할 때만 editable install을 사용합니다.

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
```

다음 실행 가능한 작업 하나 처리:

```bash
PYTHONPATH=src python3 -m codex_batch_runner run-next
```

작업 상세:

```bash
PYTHONPATH=src python3 -m codex_batch_runner show task-a
```

로그 확인:

```bash
PYTHONPATH=src python3 -m codex_batch_runner logs task-a
PYTHONPATH=src python3 -m codex_batch_runner logs task-a --cat
```

runner state 확인:

```bash
PYTHONPATH=src python3 -m codex_batch_runner state
```

## 설정

기본 runtime 디렉터리는 현재 작업 디렉터리의 `.codex-batch-runner/`입니다. 이 디렉터리는 gitignore 대상입니다.

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
- task 상태를 다시 실행 가능한 상태로 돌립니다.
- task `cooldown_until`을 설정합니다.
- global cooldown을 설정합니다.
- cooldown 전까지 Codex를 호출하지 않습니다.

초기 기본값은 launchd 10분 주기, rate-limit 이후 30분 cooldown입니다.

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

## 로컬 작업 메모

공개 repo에 올리지 않을 작업 메모는 `ROADMAP.local.md`처럼 `*.local.md` 파일로 관리합니다. 템플릿은 [examples/ROADMAP.local.example.md](examples/ROADMAP.local.example.md)에 있습니다.
