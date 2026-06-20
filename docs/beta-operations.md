# Beta 운영 가이드

이 문서는 여러 프로젝트 저장소를 오가며 `codex-batch-runner`(`cbr`)를
중앙 로컬 queue로 사용하는 beta 운영 흐름을 설명합니다.

예시는 generic path만 사용합니다. 실제 runtime state, prompt, log,
session id, credential, private queue 내용은 공개 문서와 commit에 포함하지
않습니다.

## 운영 모델

`cbr`는 codex-batch-runner 설치 위치에서 실행하거나 명시적인 config와 함께
실행합니다. 작업 대상 프로젝트는 `enqueue --cwd`로 지정합니다. launchd,
systemd, cron 같은 scheduler는 `run-next`를 반복 호출하고, 각 호출은 runnable
task를 최대 하나만 처리합니다.

```bash
cbr enqueue \
  --cwd /path/to/repo \
  --project repo-name \
  --category docs \
  --label beta \
  --created-by operator \
  --prompt "Make a small documentation update and run the relevant checks."

cbr run-next
```

자동화에서는 interactive shell 환경에 의존하지 말고 명시적인 config 경로나
안정적으로 설치된 `cbr` binary를 사용하는 편이 안전합니다.

```bash
cbr --config /path/to/cbr-config.json run-next
```

## Inbox triage

기본 `cbr list` 출력을 actionable inbox로 사용합니다. 기본 목록은 accepted와
archived task를 숨기지만, runnable task, resolution이 없는 failed 또는
blocked task, 아직 review가 필요한 completed task는 계속 표시합니다.

```bash
cbr list
```

기본 출력은 `ID`, `STATUS`, `PROJECT`, `ATTEMPTS`, `DEPS`, `FLAGS` 열을 가진
tab-separated table입니다. `FLAGS` 열에서 cooldown, dependency blocker,
last error, resolution, review state를 먼저 확인합니다.

여러 프로젝트가 하나의 queue를 공유하면 detail을 열기 전에 필터링합니다.

```bash
cbr list --project-root /path/to/repo
cbr list --project-root /path/to/repo --needs-review
cbr list --project repo-name
```

다른 프로젝트에서 작업을 확인할 때는 `--project-root`가 보통 가장 안전한
필터입니다. cbr가 enqueue 시점에 감지한 git root를 task에 저장하기 때문입니다.
`--project`는 enqueue 단계에서 안정적인 project id를 꾸준히 지정하는 운영에
적합합니다.

Inbox가 예상과 다르거나 scheduler 상태가 의심스러우면 `doctor`를 먼저 실행합니다.

```bash
cbr doctor
```

`doctor`는 Codex를 호출하지 않습니다. runtime path, command availability,
global cooldown, lock state, task count, runnable count, review count, resolved
failed/blocked count를 점검합니다.

## Review workflow

Review는 비용이 낮은 순서로 진행합니다.

1. `cbr list` 또는 project-root filter로 대상 task를 좁힙니다.
2. `cbr transcript TASK_ID`보다 먼저 `cbr summary TASK_ID`를 확인합니다.
3. 대상 repository에서 일반적인 Git 명령과 test 명령으로 상태를 검증합니다.
4. 결과에 따라 `accept`, `reject`, `resolve`를 기록합니다.

```bash
cbr summary task-id
cbr transcript task-id
git -C /path/to/repo status --short

cbr accept task-id --reason "verified locally"
cbr reject task-id --reason "missing requested check"
cbr reject task-id --follow-up --reason "needs a follow-up change"
cbr resolve task-id --resolution manual --reason "handled outside cbr"
```

`summary`는 final status, review state, project routing, dependency blocker,
`last_result.summary`, changed files, verification, `last_error`, `next_prompt`,
log path를 짧게 확인하는 용도입니다. Codex가 무엇을 했는지 또는 왜 멈췄는지
summary만으로 판단하기 어려울 때만 `transcript`를 엽니다.

`accept`는 관련 프로젝트의 상태와 검증 명령을 확인한 뒤에만 사용합니다.
완료 결과를 인정할 수 없으면 `reject`를 사용합니다. 결과 일부는 유효하지만
추가 작업이 필요하면 `reject --follow-up`을 사용합니다. `failed` 또는
`blocked_user` task가 더 이상 기본 inbox에 남아 있을 필요가 없으면 `resolve`로
운영상 결정을 기록합니다.

## `--json` 사용 기준

사람이 직접 운영할 때는 table과 Markdown 출력을 기본으로 사용합니다.

```bash
cbr list
cbr summary task-id
cbr doctor
```

다른 script, dashboard, agent가 출력을 parse해야 할 때만 `--json`을 사용합니다.

```bash
cbr list --project-root /path/to/repo --json
cbr summary task-id --json
cbr doctor --json
cbr run-next --json
```

자동화에서는 사람용 table 열을 parse하지 않습니다. Table은 operator scanning에
맞춘 출력이고, `--json`은 task 또는 report 구조를 machine-readable하게 유지합니다.

## 다른 프로젝트 smoke checklist

새 프로젝트에 큰 beta queue를 맡기기 전에 아래 checklist로 작은 흐름을 검증합니다.

1. cbr 설치 위치에서 대상 프로젝트로 작은 task를 enqueue합니다.

   ```bash
   cbr enqueue \
     --cwd /path/to/repo \
     --project repo-name \
     --category smoke \
     --label beta-smoke \
     --created-by operator \
     --prompt "Make a tiny harmless change, run the smallest relevant check, and report the result."
   ```

2. Task가 actionable inbox에 보이는지 확인합니다.

   ```bash
   cbr list --project-root /path/to/repo
   ```

3. Scheduler 실행을 기다리거나 runner pass를 한 번 직접 실행합니다.

   ```bash
   cbr run-next
   ```

4. Full transcript를 열기 전에 summary를 확인합니다.

   ```bash
   cbr list --project-root /path/to/repo --needs-review
   cbr summary task-id
   ```

5. 대상 repository 상태를 검증합니다.

   ```bash
   git -C /path/to/repo status --short
   git -C /path/to/repo diff --stat
   ```

6. 결과에 따라 accept, reject, resolve 중 하나를 기록합니다.

   ```bash
   cbr accept task-id --reason "smoke verified"
   cbr reject task-id --reason "smoke did not satisfy the request"
   cbr resolve task-id --resolution smoke --reason "smoke result recorded"
   ```

7. 기본 inbox에서 accepted 또는 resolved smoke 작업이 사라졌는지 확인합니다.

   ```bash
   cbr list --project-root /path/to/repo
   ```
