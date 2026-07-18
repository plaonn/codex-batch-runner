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

자동화, scheduler, Codex skill에서는 shell alias, interactive shell `PATH`,
운영체제 기본 `python3`에 의존하지 않는 absolute executable을 사용합니다.
Python 3.11 이상으로 설치된 `cbr` console script의 absolute path를 권장합니다.

```bash
/absolute/path/to/cbr --config /path/to/cbr-config.json doctor
/absolute/path/to/cbr --config /path/to/cbr-config.json run-loop --json
```

Checkout module invocation이 필요하면 `python3` 대신 검증된 Python 3.11 이상
interpreter의 absolute path를 사용하고 `PYTHONPATH`와 config를 명시합니다.

운영자가 직접 dashboard/review 작업을 할 때는 optional `cbr` console script가
편리합니다.

```bash
cd /path/to/codex-batch-runner
/absolute/path/to/python3.11 -m pip install -e .
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
3. `$XDG_CONFIG_HOME/codex-batch-runner/config.json`
4. `XDG_CONFIG_HOME`이 없으면 `$HOME/.config/codex-batch-runner/config.json`

먼저 선택된 path가 authoritative합니다. 명시적 `--config`나 non-empty `CBR_CONFIG`가
missing, unreadable, invalid JSON 또는 non-object이면 XDG로 fallback하지 않습니다.
XDG path가 없거나 잘못된 경우도 같은 방식으로 실패합니다. Discovery는 config,
runtime directory 또는 current-working-directory queue를 생성하지 않습니다.
Interactive shell에서는 XDG config 또는 `CBR_CONFIG`를 사용할 수 있고,
launchd/systemd 같은 자동화에서는 `--config`에 absolute path를 넘기는 방식을 권장합니다.

## Guarded managed LaunchAgent lifecycle

`cbr launchd plan`은 intended plist를 렌더링하고 명시적으로 전달한 기존 plist만 분류하는
read-only surface입니다. `cbr launchd install`과 `cbr launchd uninstall`은 같은 exact
ownership marker/digest contract를 사용하는 lifecycle operation입니다. 모든 operation은
기본적으로 dry-run이며, mutation은 `--apply`와 label이 정확히 일치하는 별도
`--confirm-label`을 함께 전달해야 합니다.

```bash
cbr --config /etc/example/cbr/config.json launchd plan \
  --label com.example.codex-batch-runner \
  --executable /opt/example/bin/cbr \
  --working-directory /opt/example/codex-batch-runner \
  --stdout-path /var/tmp/cbr/launchd.out.log \
  --stderr-path /var/tmp/cbr/launchd.err.log \
  --environment-path /opt/example/bin:/usr/bin:/bin \
  --start-interval-seconds 600 \
  --existing-plist /var/tmp/cbr/existing.plist \
  --json
```

`plan`에서 `--existing-plist`를 생략하면 filesystem에서 plist를 추측하거나 검색하지 않고
`not_installed/create` 계획을 반환합니다. 명시된 plist가 managed marker와 digest에
일치하면 `managed_ok/none`, valid managed content가 requested input과 다르면
`drifted/update_needed`입니다. Unmarked foreign plist와 malformed/tampered managed plist는
`blocked`이고 exit status 2를 반환합니다.

Install/update dry-run 예시:

```bash
cbr --config /Users/example/.config/codex-batch-runner/config.json launchd install \
  --label com.example.codex-batch-runner \
  --executable /opt/example/bin/cbr \
  --working-directory /Users/example/codex-batch-runner \
  --stdout-path /Users/example/Library/Logs/cbr.out.log \
  --stderr-path /Users/example/Library/Logs/cbr.err.log \
  --environment-path /opt/example/bin:/usr/bin:/bin \
  --start-interval-seconds 600 \
  --destination /Users/example/Library/LaunchAgents/com.example.codex-batch-runner.plist \
  --user-domain gui/501 \
  --json
```

검토한 동일 command에 `--apply --confirm-label com.example.codex-batch-runner`를 추가해야만
mutation이 허용됩니다. Install operation은 destination이 absent이면 install, exact owned
digest가 같으면 no-op, exact owned plist가 drifted이면 update합니다. Unmarked manual plist,
malformed/tampered plist, symlink, non-regular file, oversized file은 adoption 또는 overwrite하지
않고 차단합니다.

Uninstall도 explicit identity와 destination을 사용합니다.

```bash
cbr --config /Users/example/.config/codex-batch-runner/config.json launchd uninstall \
  --label com.example.codex-batch-runner \
  --destination /Users/example/Library/LaunchAgents/com.example.codex-batch-runner.plist \
  --user-domain gui/501 \
  --json
```

Lifecycle apply는 Darwin non-root user, 정확한 `gui/UID` domain, resolved config provenance,
`HOME/Library/LaunchAgents/LABEL.plist`와 정확히 일치하는 explicit absolute destination을
요구합니다. Current working directory나 LaunchAgents directory를 검색하지 않습니다.
Update는 same-directory unique backup 뒤 `bootout`, atomic replace, `bootstrap` 순서로
진행합니다. 새 plist bootstrap이 실패하면 이전 bytes를 atomic restore하고 다시
bootstrap합니다. 복구가 완결되지 않으면 `recovery_required`를 반환합니다. Uninstall은
`bootout` 성공 뒤 plist를 same-directory recoverable backup으로 atomic move하며, bootout이
실패하면 원본 plist를 유지합니다. 성공 결과에 retained backup path가 있으면 operator가
검토 후 별도로 정리합니다.

이 lifecycle contract의 service 전이는 `bootstrap`/`bootout`만 사용합니다. 아래 cooldown
wake hook의 `kickstart`는 이미 설치된 job을 깨우는 별도 실행 계약이며 install/update/
uninstall을 대신하지 않습니다.

## macOS launchd 설정

macOS beta 운영에서는 long-running foreground process 대신 `launchd` 사용을
권장합니다. Single-worker 설치에서는 scheduler tick마다 `run-loop --json`을
실행합니다. Loop의 각 iteration은 one-shot `run-next`와 같은 runner path를
사용하고, 매번 config, pause, cooldown, lock, capacity, queue selection,
auto-review candidate를 다시 확인합니다. 처리할 작업이 없거나 cooldown/lock/pause
같은 non-actionable 상태를 감지하면 종료합니다. 기본 capacity에서는 동시에 하나만
실행됩니다. 병렬 실행이 필요할 때만 config의 capacity 값을 올리고 동일한 queue를
깨우는 scheduler worker를 여러 개 운영합니다. `run-next`는 수동 one-shot 처리와
기존 automation을 위해 유지됩니다.

Apply 전에 아래 값을 확인합니다.

- `ProgramArguments`: Python 3.11 이상으로 설치한 `cbr` absolute path, 보통 `run-loop --json`
- `--config`: 운영자 config 파일 경로
- `EnvironmentVariables.PATH`: `cbr`의 Python bin directory와 configured Codex executable directory를 포함한 non-interactive child-process path
- `PYTHONPATH`: checkout에서 module invocation을 쓸 때 필요한 `src` 경로
- `WorkingDirectory`: checkout 또는 안정적인 runtime root
- `StandardOutPath`, `StandardErrorPath`: local runtime log 파일
- `StartInterval`: 원하는 polling 주기

기존 manual plist를 guarded lifecycle로 전환하려면 먼저 기존 운영 절차로 unload/remove하고
destination이 absent임을 확인해야 합니다. Lifecycle command는 manual plist를 자동
adoption하지 않습니다. Repository checkout 안의 로컬 plist는 `*.local.plist` 이름으로
private하게 유지합니다.

## 설치 후 doctor 실행

Scheduler가 사용할 것과 같은 executable과 config로 `doctor`를 한 번 실행합니다.

```bash
/absolute/path/to/cbr --config /path/to/cbr-config.json doctor
```

`doctor`는 Codex task를 실행하지 않습니다. 현재 CBR Python executable/version,
resolved runtime path, directory와 parent
접근성, Codex command availability, global cooldown, runner pause, lock state,
task count, runnable count, review count, resolved failed/blocked count,
resolved completed-review count를 점검합니다.
Unattended execution에 의존하기 전에 `error` check를 해결합니다. Warning은 exit
code를 실패로 만들지 않지만 확인해야 합니다.

Launch agent를 load했다면 첫 scheduler pass 이후 또는 manual `run-next`/`run-loop` 이후에
`doctor`를 다시 실행해 runtime path와 lock/state 파일이 예상 위치에 생기는지
확인합니다.

Queue admission과 unattended execution health는 별개입니다. `cbr status --json`의
`admission`은 pause, cooldown, lock, local capacity를 나타내며 scheduler process가
실제로 실행 가능한지는 증명하지 않습니다. Unattended dispatch 전에 다음을 모두
확인합니다.

- `doctor --json`의 `python_runtime.supported`가 `true`이고 scheduler와 같은 executable을 사용함
- supervisor가 load되어 있고 실제 argv가 그 executable과 config를 가리킴
- supervisor의 effective `PATH`에서 Python 3.11+와 configured Codex executable이 실제로 resolve됨
- 빈 queue에서도 bounded `run-next --json` 또는 1회 scheduler tick이 exit code 0으로 끝남
- supervisor의 latest exit status가 0이며 새 stderr 오류가 없음

운영자가 scheduler는 그대로 둔 채 신규 queue admission만 잠시 막고 싶으면
global cooldown 대신 runner pause를 사용합니다.

```bash
/absolute/path/to/cbr --config /path/to/cbr-config.json \
  pause set --reason "operator maintenance window"
/absolute/path/to/cbr --config /path/to/cbr-config.json pause show
/absolute/path/to/cbr --config /path/to/cbr-config.json pause clear
```

Pause는 rate-limit cooldown과 별개이며 expiry 없이 유지됩니다. 활성 중인 Codex
child는 종료하지 않고, 이후 `enqueue`는 task를 쓰지 않고 거부되며 `run-next`는
queue lock 아래에서 stale `running` recovery만 수행한 뒤 `paused`로 종료합니다.
`pause set`은 wake hook을 실행하지 않고, `pause clear`는 runnable work가 다시
있을 수 있으므로 configured post-mutation trigger를 실행합니다.

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

## Usage-aware admission 설정

아래 설정은 native Codex implementation task를 claim하기 전에 generic JSON snapshot을
한 번 읽어 remaining threshold를 확인합니다. 명시적으로 enable하지 않으면 runner 동작은
기존과 같습니다.

```json
{
  "usage_admission_enabled": true,
  "usage_admission_command": ["usage-snapshot", "--json"],
  "usage_admission_timeout_seconds": 5,
  "usage_admission_max_age_seconds": 300,
  "usage_admission_short_window_threshold_percent": 10,
  "usage_admission_reset_grace_seconds": 60,
  "manual_cooldown_wake_scheduler": "macos_launchd",
  "manual_cooldown_wake_command": ["launchctl", "start", "com.example.codex-batch-runner"]
}
```

Snapshot stdout 예시는 synthetic data만 사용하면 다음과 같습니다.

```json
{
  "available": true,
  "observed_at": "2030-01-02T03:04:05Z",
  "primary": {
    "remaining_percent": 8,
    "resets_at_iso": "2030-01-02T05:00:00Z"
  },
  "secondary": {
    "remaining_percent": 40,
    "resets_at_iso": "2030-01-08T05:00:00Z"
  }
}
```

Runner는 provider의 `primary`/`secondary` 이름을 정책 의미로 사용하지 않고 `window_minutes`가
짧은 값을 short window, 긴 값을 long window로 해석합니다. Short window가 configured threshold
이하이면 short reset + grace까지 global cooldown을 설정합니다. Long window의 낮은 잔여량은
hard gate가 아니며, 실제 0%일 때만 long reset까지 gate합니다. 둘 다 낮아도 long window가 0%가
아니면 short reset 뒤 재판정합니다. Triggering window reset이 없거나 invalid이면 unrelated
window reset으로 대체하지 않고 sanitized warning/event와 함께 fail open합니다. 선택된 reset이
지났지만 latest low snapshot이 reset 전
관측값인 경우에는 stale value로 무기한 연기하지 않고 실제 queued task 한 건을 정상 attempt로
허용합니다. Provider가 여전히 거부하면 기존 rate-limit recovery가 새 cooldown을 기록합니다.

Snapshot command는 read-only adapter여야 하며 Codex를 직접 probe하거나 install/authenticate를
수행하면 안 됩니다. Timeout, command failure, invalid JSON, unavailable snapshot은 sanitized
warning을 남기고 fail open합니다. One-shot wake를 enabled하지 않았거나 scheduling이 실패해도
정기 `run-loop`/scheduler polling이 cooldown 이후의 fallback wake path입니다.

## 다른 프로젝트에서 enqueue/check 하기

다른 프로젝트 저장소는 runner 설치 위치의 `cbr`를 호출하고 대상 프로젝트를
`--cwd`로 넘겨 central queue에 task를 등록합니다. 운영자가 task를 하나씩 열지
않고 triage할 수 있도록 project metadata를 함께 지정합니다.

```bash
/absolute/path/to/cbr \
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
/absolute/path/to/cbr --config /path/to/cbr-config.json \
  list --project-root /path/to/project-repo

/absolute/path/to/cbr --config /path/to/cbr-config.json \
  list --project-root /path/to/project-repo --needs-review
```

`--project-root`는 cross-project queue에서 보통 가장 안전한 필터입니다. Enqueue
시점에 target `--cwd`의 git root를 task에 저장하기 때문입니다. Enqueue workflow가
stable project id를 꾸준히 지정한다면 `--project`도 사용할 수 있습니다.

Automation에서는 사람용 table을 parse하지 말고 `--json`을 붙여 JSON output을
사용합니다.

```bash
/absolute/path/to/cbr --config /path/to/cbr-config.json \
  list --project-root /path/to/project-repo --json
```

## Disable 또는 uninstall

Guarded lifecycle이 소유한 macOS scheduler는 먼저 uninstall dry-run을 검토합니다.

```bash
cbr --config /Users/example/.config/codex-batch-runner/config.json launchd uninstall \
  --label com.example.codex-batch-runner \
  --destination /Users/example/Library/LaunchAgents/com.example.codex-batch-runner.plist \
  --user-domain gui/501 \
  --json
```

결과의 identity, destination, action을 확인한 뒤 같은 command에
`--apply --confirm-label com.example.codex-batch-runner`를 추가합니다. Apply는 bootout이
성공한 경우에만 plist를 recoverable same-directory backup으로 이동합니다. Temporary
disable을 위한 별도 lifecycle operation은 제공하지 않습니다. Manual/unmarked plist는 이
command로 제거할 수 없으며 기존 운영 절차를 따라야 합니다.

`cbr` console script만 필요해서 editable package를 설치했다면 pip로 제거합니다.

```bash
python3 -m pip uninstall codex-batch-runner
```

Console script 또는 managed LaunchAgent를 uninstall해도 runtime queue와 log 파일은
삭제되지 않습니다. Audit/review가 필요하면 보존하고, 더 이상 보존할 task history가
없음을 확인한 뒤 운영자 local runtime directory만 제거합니다.
