# 운영자 설치 가이드

이 문서는 운영자가 하나의 로컬 queue를 여러 프로젝트 저장소에서 공유해
`codex-batch-runner`(`cbr`)를 beta로 운영하는 설치 절차를 설명합니다. macOS에서는
`launchd`를 기본 scheduler로 권장합니다.

예시는 generic path만 사용합니다. 실제 runtime state, task prompt, Codex JSONL
로그, session id, credential, private queue 내용은 공개 문서와 commit에 포함하지
않습니다.

## 요구 사항

- Python 3.11 이상
- Scheduler를 실행할 사용자 계정에 설치된 Codex CLI
- 이 저장소의 로컬 checkout
- [examples/config.example.json](../examples/config.example.json)을 바탕으로 만든 config 파일

Runtime dependency는 없습니다. Checkout에서 `PYTHONPATH=src`로 바로 실행할 수
있고, 운영자가 짧은 `cbr` console script를 원하면 editable mode로 설치할 수
있습니다.

## 권장 실행 방식

자동화, scheduler, Codex skill에서는 shell alias나 interactive shell 환경에
덜 의존하는 module invocation을 권장합니다.

```bash
cd /path/to/codex-batch-runner
PYTHONPATH=src python3 -m codex_batch_runner --config /path/to/cbr-config.json doctor
PYTHONPATH=src python3 -m codex_batch_runner --config /path/to/cbr-config.json run-next
```

이 방식은 checkout 위치, Python interpreter, module path, config를 명시하므로
`launchd`에서 가장 예측하기 쉽습니다.

운영자가 직접 dashboard/review 작업을 할 때는 optional `cbr` console script가
편리합니다.

```bash
cd /path/to/codex-batch-runner
python3 -m pip install -e .
cbr --help
```

Console script는 `list`, `summary`, `transcript`, `accept`, `reject` 같은
interactive 명령에 적합합니다. 자동화의 필수 조건은 아닙니다. Scheduler에서
`cbr`를 직접 쓴다면 설치된 script의 absolute path를 사용하고 `--config`를
명시하는 편이 안전합니다.

## Runtime path 설정

Example config를 운영자 전용 위치로 복사한 뒤 로컬 환경에 맞게 수정합니다.

```bash
cp /path/to/codex-batch-runner/examples/config.example.json /path/to/cbr-config.json
```

Config는 `root`, `queue_dir`, `log_dir`, `lock_file`, `state_file`, Codex
command template, stale lock 기준, rate-limit cooldown, 기본 max attempts를
제어합니다. `root`가 있으면 상대 runtime path는 `root` 기준으로 해석됩니다.
`root`가 없으면 built-in fallback 호환성을 위해 process current working
directory를 기준으로 해석됩니다. User config를 기본 운영 config로 쓴다면
`root`를 설정해 어느 working directory에서 `cbr`를 실행해도 같은 queue와 log를
보게 하는 구성을 권장합니다.

`codex`가 scheduler의 제한적인 `PATH` 밖에 있으면 config의 `codex_command`와
`codex_resume_command`에 executable absolute path를 지정합니다. 사용자 로컬
package manager로 설치한 CLI에서 흔한 상황입니다.

## Config discovery 순서

`cbr`는 아래 순서로 config를 찾습니다.

1. `--config /path/to/config.json`
2. `CBR_CONFIG=/path/to/config.json`
3. `$XDG_CONFIG_HOME/codex-batch-runner/config.json`, 또는 `XDG_CONFIG_HOME`이
   없을 때 `~/.config/codex-batch-runner/config.json`
4. Current working directory 기준 built-in defaults

User config discovery는 운영자 개인의 interactive shell과 Codex skill에서
`--config` 없이 같은 기본 queue를 쓰는 용도에 적합합니다. 여러 working
directory에서 실행할 수 있으면 user config에 `root`를 설정합니다. 특정 실험
queue나 다른 운영 config를 명시적으로 쓰고 싶을 때는 `--config`를 사용합니다.
Built-in fallback은 process working directory의 `.codex-batch-runner/` 아래에
runtime state를 만들므로 빠른 local 실험에는 충분하지만 shared beta queue에는
적합하지 않습니다.

## macOS launchd 설정

macOS beta 운영에서는 long-running foreground process 대신 `launchd` 사용을
권장합니다. Scheduler tick마다 `run-next`를 실행하고, runner는 최대 task 하나를
처리하거나 처리할 작업이 없거나 cooldown/lock 상태를 감지하면 종료합니다.

예시 plist는
[examples/com.example.codex-batch-runner.plist](../examples/com.example.codex-batch-runner.plist)에
있습니다.

User LaunchAgents directory로 복사한 뒤 placeholder path를 모두 수정합니다.

```bash
cp /path/to/codex-batch-runner/examples/com.example.codex-batch-runner.plist \
  ~/Library/LaunchAgents/com.example.codex-batch-runner.plist
```

Load 전에 아래 값을 확인합니다.

- `ProgramArguments`: module invocation 또는 installed `cbr` absolute path
- `--config`: 운영자 config 파일 경로
- `PYTHONPATH`: checkout에서 module invocation을 쓸 때 필요한 `src` 경로
- `WorkingDirectory`: checkout 또는 안정적인 runtime root
- `StandardOutPath`, `StandardErrorPath`: local runtime log 파일
- `StartInterval`: 원하는 polling 주기

수정 후 agent를 load합니다.

```bash
launchctl load ~/Library/LaunchAgents/com.example.codex-batch-runner.plist
```

Repository checkout 안에 로컬 수정 plist를 둘 때는 `*.local.plist` 이름을 사용해
private 파일로 유지합니다.

## 설치 후 doctor 실행

Scheduler가 사용할 것과 같은 config와 실행 방식으로 `doctor`를 한 번 실행합니다.

```bash
cd /path/to/codex-batch-runner
PYTHONPATH=src python3 -m codex_batch_runner --config /path/to/cbr-config.json doctor
```

`doctor`는 Codex를 호출하지 않습니다. Resolved runtime path, directory와 parent
접근성, Codex command availability, global cooldown, lock state, task count,
runnable count, review count, resolved failed/blocked count, resolved completed-review count를 점검합니다.
Unattended execution에 의존하기 전에 `error` check를 해결합니다. Warning은 exit
code를 실패로 만들지 않지만 확인해야 합니다.

Launch agent를 load했다면 첫 scheduler pass 이후 또는 manual `run-next` 이후에
`doctor`를 다시 실행해 runtime path와 lock/state 파일이 예상 위치에 생기는지
확인합니다.

## 수동 cooldown one-shot wake

`cooldown set VALUE`는 기본적으로 `global_cooldown_until`만 저장하며, 기존
launchd polling이 fallback으로 계속 동작합니다. Manual reset 시각에 더 가깝게
재시작하려면 config에서 macOS one-shot wake adapter를 명시적으로 켤 수 있습니다.

```json
{
  "manual_cooldown_wake_scheduler": "macos_launchd",
  "manual_cooldown_wake_command": ["launchctl", "start", "com.example.codex-batch-runner"]
}
```

`manual_cooldown_wake_command`는 Codex CLI가 아니라 정상 runner entrypoint를 깨우는
명령이어야 합니다. Launch agent를 쓰는 환경에서는 위 예시처럼 service label을
`launchctl start`로 시작하는 구성이 가장 단순합니다. Adapter는 `launchctl submit`으로
launchd 관리 one-shot job을 등록하고, job은 `effective_cooldown_until`까지 기다린 뒤
wake command를 실행합니다. `launchctl kickstart -k`는 사용하지 않습니다.

One-shot 등록에 실패해도 `cooldown set` 자체는 실패하지 않습니다. 명령 출력과 event
log에 scheduled, skipped, failed 상태가 남으며, polling scheduler가 다음 주기에서
계속 fallback으로 동작합니다.

## 다른 프로젝트에서 enqueue/check 하기

다른 프로젝트 저장소는 runner 설치 위치의 `cbr`를 호출하고 대상 프로젝트를
`--cwd`로 넘겨 central queue에 task를 등록합니다. 운영자가 task를 하나씩 열지
않고 triage할 수 있도록 project metadata를 함께 지정합니다.

```bash
PYTHONPATH=/path/to/codex-batch-runner/src \
python3 -m codex_batch_runner \
  --config /path/to/cbr-config.json \
  enqueue \
  --cwd /path/to/project-repo \
  --project project-repo \
  --category docs \
  --label beta \
  --created-by operator \
  --prompt "Make a small documentation update and run the relevant checks."
```

Review나 dashboard에서는 detail을 열기 전에 project root로 먼저 필터링합니다.

```bash
PYTHONPATH=/path/to/codex-batch-runner/src \
python3 -m codex_batch_runner --config /path/to/cbr-config.json \
  list --project-root /path/to/project-repo

PYTHONPATH=/path/to/codex-batch-runner/src \
python3 -m codex_batch_runner --config /path/to/cbr-config.json \
  list --project-root /path/to/project-repo --needs-review
```

`--project-root`는 cross-project queue에서 보통 가장 안전한 필터입니다. Enqueue
시점에 target `--cwd`의 git root를 task에 저장하기 때문입니다. Enqueue workflow가
stable project id를 꾸준히 지정한다면 `--project`도 사용할 수 있습니다.

Automation에서는 사람용 table을 parse하지 말고 `--json`을 붙여 JSON output을
사용합니다.

```bash
PYTHONPATH=/path/to/codex-batch-runner/src \
python3 -m codex_batch_runner --config /path/to/cbr-config.json \
  list --project-root /path/to/project-repo --json
```

## Disable 또는 uninstall

macOS scheduler를 일시적으로 끄려면 LaunchAgent를 unload합니다.

```bash
launchctl unload ~/Library/LaunchAgents/com.example.codex-batch-runner.plist
```

Scheduler를 제거하려면 unload 후 `~/Library/LaunchAgents/`의 plist를 삭제합니다.

`cbr` console script만 필요해서 editable package를 설치했다면 pip로 제거합니다.

```bash
python3 -m pip uninstall codex-batch-runner
```

Console script를 uninstall하거나 `launchd`를 unload해도 runtime queue와 log 파일은
삭제되지 않습니다. Audit/review가 필요하면 보존하고, 더 이상 보존할 task history가
없음을 확인한 뒤 운영자 local runtime directory만 제거합니다.
