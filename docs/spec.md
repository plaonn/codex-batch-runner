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

운영 모델상 `completed + unreviewed`, `completed + rejected`, `completed + needs_followup`은 아직 처리해야 할 task로 봄. 기본 `cbr list`는 `completed + accepted`와 `archived`만 숨기고, 검토가 끝나지 않은 completed task는 기본 출력에 표시함.

## Automatic review bundle and reviewer Codex

규칙만으로 `completed` task를 자동 accept하는 방식은 충분하지 않음. 파일 변경, 테스트 명령, commit/push 상태 같은 기계적 신호는 누락과 모순을 찾는 데 유용하지만, 원래 prompt 의도 충족 여부, 문서/코드 변경의 적절성, 공개 저장소 안전 정책 준수 여부, 후속 작업 필요성은 task마다 문맥 판단이 필요함. 따라서 review는 아래 단계로 분리함.

- Mechanical gates: task 상태, dependency 상태, final JSON schema, verification 유무, git dirty/unpushed 상태, diff 크기, 금지된 runtime/private 파일 포함 여부 같은 결정적 검사를 수행함.
- Reviewer Codex: 독립적으로 생성한 review bundle만 읽고 작업 결과를 평가함. 현재 대화 context나 작업 실행 thread 기억에 의존하지 않음.
- Human fallback: confidence가 낮거나 private/public 안전성, 의도 충족, 큰 diff, 실패한 검증, credential 가능성처럼 사람이 봐야 하는 항목이 있으면 accept하지 않고 확인 대상으로 남김.

Review bundle은 특정 task의 결과를 재검토하기 위한 self-contained artifact임. 생성 시점의 현재 대화 context, Codex transcript 전체, operator 개인 메모에 의존하지 않고, task JSON과 대상 git repository의 현재 local state에서 다시 만들 수 있어야 함. bundle은 기본적으로 report-only 입력이며, 첫 구현은 파일 저장 또는 stdout 출력만 수행하고 review status를 변경하지 않음.

필수 입력:

- task prompt: task에 저장된 prompt와, `needs_resume` 완료인 경우 관련 `next_prompt` 요약
- task metadata: id, status, review_status, cwd, project_root, project_id, category, labels, created_by, attempts, timestamps
- dependencies: `depends_on` id와 각 dependency의 status/review_status 요약
- `last_result`: status, summary, next_prompt, changed_files, verification, optional `commits`, optional `push_status`
- `last_run`: command_kind, returncode, started/finished time, duration_seconds, resume_id_used 존재 여부, log path 존재 여부
- changed files: `last_result.changed_files`와 git diff/name-status에서 확인한 변경 파일 목록
- verification: Codex가 보고한 검증 명령과 결과 요약. 필요하면 reviewer가 재실행할 명령을 제안할 수 있으나 bundle 생성 단계에서 임의 실행하지 않음
- git status: branch, upstream/comparison ref, ahead/behind, dirty 여부, unpushed commit 요약, warnings
- commit data: 관련 commit hash, 짧은 subject/stat, 필요한 경우 sanitized diff. commit/push metadata는 `cbr-result-push-metadata`에서 저장한 optional result fields와 task `git_status`를 함께 사용함
- relevant docs/spec excerpts: README, `docs/spec.md`, examples, public policy 문서가 변경된 경우 해당 주변 문단의 짧은 excerpt
- public/private safety policy: 공개 repo에 commit하면 안 되는 runtime state, 실제 logs/prompts/session ids/thread ids, credentials, 개인 경로, Telegram token/chat id, private queue contents 금지 규칙

Bundle에 기본 포함하지 않는 정보:

- raw private logs 또는 전체 JSONL transcript
- 전체 대화 transcript
- credentials, tokens, chat ids, 개인 계정 식별자
- session id/thread id 원문. 필요한 경우 존재 여부만 표시하거나 sanitized placeholder 사용
- `.codex-batch-runner/` runtime state contents, 실제 queue contents, operator-local `*.local.md` 세부 내용

Reviewer Codex decision schema:

```json
{
  "task_id": "string",
  "decision": "accept | needs_human | reject | follow_up",
  "confidence": "low | medium | high",
  "reason": "string",
  "required_human_checks": ["string"],
  "suggested_follow_up_prompt": "string"
}
```

Decision 의미:

- `accept`: bundle만으로 prompt 충족, 검증, 공개 안전 정책을 high confidence로 확인함
- `needs_human`: 자동 판단에는 정보가 부족하거나 사람이 봐야 할 위험이 있음
- `reject`: 결과가 명확히 실패했거나 task 목표와 충돌함
- `follow_up`: 기본 방향은 맞지만 추가 Codex 작업이 필요함. 이 경우 `suggested_follow_up_prompt`를 구체적으로 작성함

초기 구현은 반드시 dry-run/report-only로 시작함. `cbr review-next --dry-run`은 다음 검토 대상과 mechanical gate 근거를 출력하되 `review_status`를 바꾸거나 follow-up task를 만들지 않음. Auto-accept는 별도 후속 phase이며, mechanical gates를 모두 통과하고 reviewer decision이 `accept`, confidence가 `high`, `required_human_checks`가 비어 있는 경우에만 선택적으로 허용할 수 있음. Auto-apply phase에서도 기본값은 비활성화하고, rejected/follow_up 결정은 자동으로 새 task를 enqueue하지 않음.

Rough roadmap:

- `cbr review-bundle TASK_ID`: bundle을 stdout 또는 지정 파일로 생성함. raw transcript 없이 self-contained report를 만들고, private/public 안전 policy를 항상 포함함.
- `cbr review-next --dry-run`: `completed + unreviewed/rejected/needs_followup` task 중 하나를 선택해 bundle을 만들고 mechanical gates report를 출력함. Reviewer Codex 호출과 auto-apply는 아직 수행하지 않음.
- Reviewer Codex call: bundle만 prompt로 전달해 decision schema JSON을 받음. 실패하거나 schema가 맞지 않으면 `needs_human`으로 보고함.
- Later optional auto-apply: config로 명시적으로 켠 경우에만 high-confidence `accept`를 `cbr accept`와 동일한 상태 변경으로 반영함. `follow_up`은 report에 follow-up prompt를 남기되 task 생성은 operator가 별도로 수행함.

## Queue mutation and replan control plane

Queue mutation은 사람이 task JSON을 편집하기 쉽게 만드는 기능이 주목적이 아님. 핵심 목적은 Codex 또는 operator workflow가 설계 변경, review 결과, dependency 재정렬, 운영 중단 같은 control-plane 결정을 안전하고 감사 가능하게 queue에 반영하는 것임. 특히 이미 여러 batch task가 enqueued된 뒤 spec이나 roadmap이 바뀌면, 기존 prompt를 지우거나 task를 임의로 삭제하는 대신 변경 이유와 적용 결과가 남는 replan 흐름이 필요함.

초기 설계는 “명시적 계획을 검증하고 적용하는 작은 mutation engine”으로 둠. Codex가 곧바로 queue를 무제한 수정하지 않고, 사람이 읽을 수 있는 plan patch를 만들고 dry-run 검증을 거친 뒤 제한된 operation만 적용할 수 있어야 함.

의도한 operation:

- `pause`: 아직 실행하지 않을 task를 일시 중지함. 원래 status와 사유를 기록함.
- `unpause`: pause된 task를 원래 실행 가능 상태로 되돌림.
- `replan` 또는 `update`: task의 실행 지시를 최신 설계에 맞게 보강함. 원본 prompt를 덮어쓰지 않고 `next_prompt`, `plan_notes`, `history` 같은 append-only field 또는 명시적 revision metadata에 변경을 남김.
- `supersede`: 기존 task를 더 이상 실행하지 않도록 표시하고 대체 task id나 resolution reason을 연결함.
- `split`: 큰 task를 여러 후속 task로 나누고, 원 task에는 split history와 child task id를 기록함.
- `merge`: 중복되거나 강하게 결합된 task들을 하나의 대표 task로 합치고, source task에는 merge 대상과 사유를 기록함.
- `retarget_metadata`: `project_root`, `project_id`, `category`, `labels`, `created_by` 같은 routing metadata를 수정함.
- `dependency_changes`: `depends_on`을 추가, 제거, 교체함.
- `append_note` 또는 `append_history`: task 실행 지시를 바꾸지 않고 운영 메모나 review/replan 근거만 추가함.
- `create_followup`: review 또는 replan 결과로 새 task를 제한적으로 등록함. 기본값은 dry-run이며, 자동 생성 수와 dependency 연결을 엄격히 제한함.

안전 규칙:

- Replan/control-plane mutation은 `running` task를 실행 상태 전환 대상으로 mutate하지 않음. runner lock 또는 task `status=running`이 보이면 pause/replan/dependency rewrite 같은 계획 적용은 reject함.
- `completed` task는 원칙적으로 재작성하지 않음. 허용 범위는 `review_status`, `reviewed_at`, `review_reason`, `resolution`, audit/history 같은 review/resolution metadata로 제한함.
- `accept`는 `completed` task에만 허용함. `reject`와 `needs_followup`은 운영자가 비정상 실행 결과나 후속 처리 필요성을 표시할 수 있도록 더 넓은 상태에서 허용하되, runner가 수행하는 실행 상태 전환을 대체하지 않음.
- 원본 `prompt`, 기존 `next_prompt`, 실행 history, `last_result`, `last_run`, log path는 보존함. 설계 변경은 덮어쓰기보다 revision 또는 append-only history로 표현함.
- 모든 mutation은 `reason`이 필수임. 자동 생성 계획에는 `actor`와 plan 생성 근거도 포함함.
- dependency graph에 cycle이 생기면 reject함. 존재하지 않는 task id, 자기 자신 dependency, completed가 아닌 superseded dependency 같은 애매한 상태는 dry-run에서 warning 또는 error로 보고함.
- `create_followup`, `split`, `merge`는 unbounded task creation을 만들 수 있으므로 plan당 생성 task 수와 전체 queue 증가량을 제한함.
- public/private 안전 정책을 그대로 적용함. mutation plan, task history, event log에는 로컬 runtime state, 실제 raw log/prompt/session id/thread id, credentials, 개인 경로, Telegram token/chat id, private queue contents를 넣지 않음. 필요한 prompt 변경은 sanitized summary 또는 operator가 의도적으로 제공한 public-safe prompt text만 저장함.
- mutation apply는 atomic write와 lock policy를 따라야 함. 여러 task를 바꾸는 plan은 가능한 한 all-or-nothing으로 검증하고, 부분 적용이 불가피하면 event log에 적용 성공/실패 task를 명확히 남김.

초기 interface 후보:

```bash
cbr queue pause TASK_ID --reason "blocked by spec change"
cbr queue unpause TASK_ID --reason "new spec accepted"
cbr queue note TASK_ID --reason "review found missing dependency" --note "wait for task-b"
cbr queue supersede TASK_ID --by TASK_ID --reason "covered by newer plan"
cbr queue deps TASK_ID --add DEP_ID --remove OLD_DEP_ID --reason "implementation order changed"
cbr queue replan TASK_ID --prompt-file replan.md --reason "spec updated"
cbr queue plan --from review-bundle.json --out queue-plan.json
cbr apply-plan queue-plan.json --dry-run
cbr apply-plan queue-plan.json
```

작은 수동 operation은 `cbr queue ...` subcommand로 표현하고, Codex/operator workflow가 여러 task를 함께 바꾸는 경우에는 구조화된 `cbr apply-plan queue-plan.json`을 기본 경로로 둠. 첫 구현은 `apply-plan --dry-run`만 제공해 validation report를 출력하고 task 파일을 쓰지 않음.

현재 구현된 `cbr apply-plan QUEUE_PLAN.json --dry-run`은 read-only validator임. `--dry-run` 없이 실행하면 apply mode가 아직 구현되지 않았다는 명확한 오류로 종료함. 지원 operation 이름은 `pause`, `unpause`, `replan`, `supersede`, `split`, `merge`, `retarget_metadata`, `dependency_changes`, `append_note`, `create_followup`임. Dry-run은 plan JSON을 읽고 `schema_version`, `actor`, `operations`, plan 또는 operation 단위 `reason`, 대상 task 존재 여부, running task 대상 금지, dependency_changes와 생성 draft가 만드는 dependency cycle을 검증함. 결과는 human report 또는 `--json` structured report로 출력함. 이 단계는 queue 파일을 변경하지 않고 Codex를 호출하지 않으며 mutation trigger도 실행하지 않음. Report에는 raw prompt, log path, session/thread id, credential/token 같은 민감한 plan 값을 redaction해야 함.

Plan patch schema의 상위 형태:

```json
{
  "schema_version": 1,
  "plan_id": "queue-plan-20260621-001",
  "actor": {
    "type": "codex | operator | reviewer",
    "id": "string"
  },
  "reason": "string",
  "created_at": "2026-06-21T12:00:00+09:00",
  "expected_queue_revision": "string optional",
  "limits": {
    "max_created_tasks": 3
  },
  "operations": [
    {
      "op": "pause | unpause | replan | supersede | split | merge | retarget_metadata | dependency_changes | append_note | create_followup",
      "task_id": "string optional",
      "task_ids": ["string optional"],
      "creates": ["task draft optional"],
      "fields": {
        "status": "string optional",
        "next_prompt": "string optional",
        "depends_on": ["string optional"],
        "project_id": "string optional",
        "category": "string optional",
        "labels": ["string optional"]
      },
      "reason": "string",
      "expected": {
        "status": "string optional",
        "review_status": "string optional",
        "updated_at": "string optional"
      },
      "validation": {
        "allow_completed_metadata_only": true,
        "requires_no_running_task": true,
        "reject_dependency_cycles": true
      }
    }
  ]
}
```

각 operation은 바꿀 task id, 변경하려는 field, operation별 reason, 기대하는 현재 상태(`expected`)를 포함해야 함. `expected`는 stale plan 방지용 optimistic validation으로 사용함. 적용 전 validation은 schema, task existence, allowed status transition, dependency graph, public/private safety, task creation limit, atomic write 가능 여부를 검사하고, dry-run report에 `would_change`, `warnings`, `errors`를 구분해 출력함.

Audit 요구사항:

- 모든 mutation은 task의 `history` 배열 또는 별도 append-only queue event log에 기록함.
- 기록에는 mutation id, operation, actor, reason, affected task id, changed fields, before/after summary, validation result, occurred_at을 포함함.
- 나중에 review bundle이나 operator가 “왜 queue가 바뀌었는지”를 재구성할 수 있어야 함.
- event payload는 notification event model과 같은 안전 기준을 따르며, raw prompt/log/session/thread id나 credential을 포함하지 않음.

Rough roadmap:

- Spec first: 이 section을 기준으로 operation, validation, audit model을 확정함.
- Read-only validation/dry-run: `cbr apply-plan --dry-run`이 queue를 읽고 plan patch의 schema와 dependency graph, safety rule 위반을 보고함.
- Limited mutations: `pause`, `unpause`, `append_note`, `dependency_changes`, metadata retarget처럼 blast radius가 작은 operation부터 적용함.
- Replan/supersede/split/merge: review bundle과 operator 확인 흐름이 충분히 안정된 뒤 task prompt revision과 task creation을 제한적으로 허용함.
- Codex-generated plan patches: reviewer gates와 human fallback이 존재한 뒤에만 Codex가 plan patch를 생성하게 하고, 기본은 dry-run 또는 human-approved apply로 유지함.

## Project routing metadata

여러 프로젝트가 하나의 중앙 queue를 공유하면 review 대상 판정을 위해 task를 하나씩 열람하는 방식은 토큰과 시간이 낭비됩니다. task 등록 시 review routing metadata를 함께 저장하고, list 단계에서 먼저 좁혀 볼 수 있게 합니다.

구현 필드:

- `schema_version`: task schema 호환성 판단용 정수
- `project_root`: task가 속한 git root. `git rev-parse --show-toplevel` 성공 시 그 값을 사용하고, 실패하면 `cwd`로 fallback합니다.
- `project_id`: 기본값은 `project_root` basename입니다. 필요하면 enqueue option으로 override할 수 있습니다.
- `category`: `implementation`, `review`, `smoke`, `maintenance`, `docs` 같은 운영 분류
- `labels`: 사람이 지정하거나 skill이 추론한 짧은 태그 목록
- `created_by`: `enqueue-codex-batch`, `operator`, `test` 같은 등록 주체

향후 후보 필드:

- `source_thread_id`: 확인 가능한 경우 등록을 요청한 Codex thread id

기존 task와 호환되어야 합니다. metadata가 없는 task는 `cwd`를 `project_root` fallback으로 사용하고, `project_id`는 fallback root의 basename으로 계산하며, `category`와 `labels`는 비워 둡니다.

관련 CLI:

```bash
cbr enqueue --cwd /path/to/repo --project codex-batch-runner --category implementation --label rate-limit --created-by enqueue-codex-batch --prompt-file task.md
cbr list --project codex-batch-runner
cbr list --project-root /path/to/repo
cbr list --cwd /path/to/repo
cbr list --category implementation
cbr list --label rate-limit
cbr list --unreviewed
cbr list --needs-review
```

전역 enqueue skill은 task 등록 시 `--created-by enqueue-codex-batch`를 함께 넘깁니다. `project_root`는 runner가 `cwd`에서 자동 계산합니다. review 요청에서는 현재 repo root로 먼저 필터링하고, 필요할 때만 개별 task의 `show` 또는 `transcript`를 읽습니다.

## Dependency policy

task 등록 시 `depends_on`으로 의존 task id를 명시할 수 있음.

runner는 아래 조건을 모두 만족하는 task 하나만 실행함.

- `status`가 `runnable` 또는 `needs_resume`
- `cooldown_until`이 없거나 현재 시각 이전
- 모든 `depends_on` task가 dependency readiness policy를 만족함
- global cooldown 상태가 아님

실행 가능한 task가 없으면 Codex를 호출하지 않고 즉시 종료함.

기본 dependency readiness policy는 기존 동작과 호환되도록 dependency task의 `status=completed`만 요구함. Config `dependency_requires_accepted_review` 기본값은 `false`임.

`dependency_requires_accepted_review=true`이면 dependency task는 `status=completed`와 `review_status=accepted`를 모두 만족해야 ready임. 이때 dependency가 `completed`이지만 `review_status`가 `accepted`가 아니면 runner는 dependent task를 건너뛰고 reporting은 blocker reason을 `not_accepted`로 표시함. dependency가 없거나 `completed`가 아니면 blocker reason은 `not_completed`임.

의존 task가 `failed` 또는 `blocked_user`인 경우 dependent task를 자동 실패시키지 않음. `list` 또는 `show`에서 dependency blocked 상태를 표시하고 runner는 해당 task를 건너뜀.

마이그레이션은 기본값 `false`로 기존 queue behavior를 유지하면서 completed task의 review state를 정리한 뒤, operator가 accepted review를 dependency gate로 쓸 준비가 되었을 때 `dependency_requires_accepted_review=true`를 설정하는 순서로 진행함. 전환 직후 completed-but-unaccepted dependency를 가진 child task는 runnable 목록에서 제외될 수 있으며, `list`, `summary`, `review-bundle`, `review-next`, `doctor` report에서 blocker reason을 확인함.

## Operational triage plan

실운용에서 중앙 queue가 커지면 full transcript를 읽기 전에 저비용 triage가 가능해야 함.

구현:

- `cbr list`는 실행 대기 task와 검토 대기 task를 기본 표시합니다.
- `cbr list --all`은 accepted/archived까지 포함한 전체 목록을 표시합니다.
- `cbr summary TASK_ID`는 `last_result.summary`, changed files, verification, last_error를 transcript보다 짧게 보여줍니다.
- failed/blocked task에는 `resolution`을 기록해 `wont_fix`, `superseded`, `manual`, `smoke`, `duplicate` 같은 운영 결정을 남길 수 있습니다.

계획:

- `cbr list --verbose`는 summary의 핵심 정보를 목록 화면에 압축해서 보여줄 수 있습니다.
- 오래된 `accepted`/`archived` task와 로그는 추후 `cbr prune`으로 정리할 수 있게 합니다.
- 초기 `cbr prune`은 dry-run report를 기본값으로 두고, 명시적인 `--apply`가 있을 때만 삭제합니다.

## Event log and derived SQLite index roadmap

dashboard, Telegram notification, automatic review, queue mutation 기능이 커지기 전에 runtime record의 계층을 명확히 둡니다. 지금 단계에서 모든 queue 상태를 full SQL canonical storage로 옮기는 것은 권장하지 않습니다. 초기 source of truth는 계속 task JSON 파일입니다. task JSON 파일은 사람이 읽고 복구할 수 있는 canonical queue state이며, Codex attempt output은 attempt별 JSONL 로그로 보존합니다.

다음 durable audit layer는 append-only event log입니다. event log는 task JSON의 최신 상태를 대체하지 않고, 언제 어떤 상태 변화와 운영 결정이 발생했는지 재구성하기 위한 감사 stream입니다. 현재 minimal implementation은 `event_dir` config 값이 있으면 그 경로를 사용하고, 없으면 runtime directory 아래 date-partitioned JSONL 파일로 저장합니다.

```text
.codex-batch-runner/events/YYYY-MM-DD.jsonl
```

대표 event type:

- `task_created`: task가 queue에 등록됨
- `task_started`: runner가 task 실행을 시작함
- `task_completed`: Codex final JSON이 `completed`를 반환함
- `task_failed`: task가 실패 상태로 전환됨
- `task_needs_resume`: Codex final JSON이 `needs_resume`을 반환함
- `task_blocked_user`: Codex final JSON이 `blocked_user`를 반환함
- `task_reviewed`: 운영자 또는 review workflow가 검토 상태를 기록함
- `task_resolved`: failed/blocked task에 운영상 resolution이 기록됨
- `task_archived`: task가 archived 상태로 전환됨
- `task_mutated`: queue mutation plan 또는 제한된 queue command가 task metadata나 실행 계획을 변경함
- `dependency_changed`: task dependency graph가 변경됨
- `rate_limit_detected`: rate-limit 또는 usage-limit cooldown이 설정됨
- `git_commit_detected`: task 결과 또는 local inspection에서 관련 commit metadata가 관측됨
- `git_push_detected`: task 결과 또는 local inspection에서 push 상태 변화가 관측됨
- `notification_sent`: notifier가 event에 대한 외부 알림 전송을 완료함

현재 구현은 snake_case 이름을 사용합니다. `task.accepted`, `task.rejected` 같은 세부 review decision은 `task_reviewed` event의 `review_status` payload field로 표현합니다. `lock.stale_recovered` 같은 더 세부적인 상태 변화는 future event type 또는 `task_mutated` subtype/status field로 표현할 수 있습니다.

각 event payload는 consumer에 필요한 최소 안전 필드만 포함합니다.

- `event_id`: 중복 처리 방지용 고유 id
- `occurred_at`: event 발생 시각
- `task_id`: task 관련 event일 때의 task id
- `project_id`: project routing metadata
- `status`: task 상태 전이와 관련 있을 때의 status
- `review_status`: accept/reject/follow-up review 상태와 관련 있을 때의 review state
- `resolution`: resolved task의 운영상 처리 결정
- `attempts`: event 시점의 task attempt count
- `summary_excerpt`: 사람이 알림에서 읽을 수 있는 짧은 요약

Event envelope includes `schema_version`, `event_id`, `event_type`, `occurred_at`, optional `task_id`, optional `project_id`, optional `project_root`, `actor`, `source`, `summary`, and sanitized `payload`.

Payload 원칙:

- event는 작고 구조화된 record로 유지합니다.
- transcript, raw Codex JSONL log, prompt 원문, session id, thread id, credential, Telegram token/chat id, 환경 변수 값, secret으로 볼 수 있는 문자열을 넣지 않습니다.
- private prompt는 기본적으로 저장하지 않습니다. 꼭 필요한 경우 operator가 명시적으로 제공한 sanitized summary 또는 짧은 excerpt만 저장합니다.
- Git metadata는 commit hash, subject excerpt, ahead/behind, pushed 여부처럼 필요한 최소 정보만 저장하고 diff 전문은 event에 넣지 않습니다.
- 알림이나 dashboard에서 더 자세한 확인이 필요하면 operator가 로컬에서 `cbr summary`, `cbr review-bundle`, `cbr transcript`를 직접 실행합니다.

Minimal implementation emits events from `enqueue`, `run-next` task transitions, `accept`, `reject`, `resolve`, `archive`, and rate-limit detection. Event write failures are non-fatal warnings; canonical task JSON remains the source of truth.

Event log가 필요한 이유:

- Queue mutation/replan: task가 왜 pause, dependency change, supersede, follow-up 상태가 되었는지 append-only history로 남길 수 있습니다.
- Review bundle: reviewer가 현재 task JSON만으로 알기 어려운 상태 변화 순서와 운영 결정을 self-contained하게 재구성할 수 있습니다.
- Telegram notifications: notifier가 task 파일 polling만으로 놓치기 쉬운 edge-triggered 변화를 cursor 기반으로 중복 없이 처리할 수 있습니다.
- Dashboard: status counts, recent activity, unresolved failures, review backlog, rate-limit history를 매번 전체 JSONL transcript에서 재계산하지 않아도 됩니다.
- Post-hoc debugging: runner crash, stale lock recovery, rate-limit, git metadata 관측, notification failure 같은 운영 사건을 나중에 시간순으로 확인할 수 있습니다.

Notifier는 각자 cursor와 전송 상태를 public repository 밖에 저장합니다. 예를 들어 notifier는 `.codex-batch-runner/notify-state.json`이나 사용자 local config/state 파일에 마지막 처리 event file, byte offset, 마지막 event id, 전송 실패 retry metadata를 저장할 수 있습니다. Notifier state는 adapter별로 독립적이어야 하며, 한 notifier의 장애가 다른 notifier의 cursor를 변경하지 않아야 합니다.

JSONL Codex attempt logs와 event logs는 장기 운영에서 계속 커질 수 있습니다. Retention policy는 review와 audit 요구사항이 충족된 뒤 오래된 runtime logs/events를 정리할 수 있어야 합니다. 장기 기본 정책은 60일보다 오래된 runtime log와 event file을 cleanup 후보에 포함하는 방향입니다. Current `cbr prune` reports old event JSONL files under configured `event_dir` as distinct event candidates and deletes them only when `--apply` is explicit. If notifier cursor state paths are configured, event pruning checks them before deleting old event files. Cursor state상 아직 처리되지 않았거나 fully processed 여부가 불확실한 event file은 삭제하지 않고 skipped warning으로 보고합니다.

SQLite는 초기 source of truth가 아니라 derived index/cache입니다. SQLite index는 task JSON 파일과 event log에서 재생성 가능해야 하며, dashboard, notification, search, automated review workflow가 빠르게 조회하기 위한 optional layer로 둡니다. SQLite 파일이 없거나 손상되어도 `cbr enqueue`, `cbr list`, `cbr run-next`, `cbr accept/reject`, `cbr prune` 같은 core command는 canonical task JSON 파일과 event log만으로 계속 동작해야 합니다. 복구 방법은 손상된 SQLite 파일을 삭제하고 task JSON 및 event log에서 index를 다시 build하는 것입니다.

Rough roadmap:

- Event schema spec: event envelope, snake_case event type, required fields, payload safety rule, versioning, retention interaction을 확정합니다.
- Event writer helper: append-only JSONL writer, event id 생성, date partition, fsync/atomicity 기준, sanitizer를 구현합니다.
- Existing command emission: enqueue, run-next state transitions, accept/reject/resolve, rate-limit detection, git metadata inspection, future queue mutation command에서 event를 기록합니다.
- Prune/retention support: task/log cleanup report에 event file 후보를 포함하고 notifier cursor safety check를 적용합니다.
- Optional SQLite index builder: task JSON과 event log를 읽어 rebuild 가능한 local SQLite cache를 생성합니다.
- Dashboard/notification consumers: dashboard, Telegram notifier, search, automated review workflow는 SQLite가 있으면 index를 사용하고, 없으면 canonical JSON/event log fallback을 사용합니다.

Telegram integration은 future optional adapter입니다. Core runner는 Telegram에 직접 의존하지 않고 append-only event log만 기록합니다. Telegram token, chat id, enable flag, rate limit, formatting option은 local-only config나 runtime state에만 저장하며 public docs와 examples에는 실제 값을 포함하지 않습니다.

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
16. 다른 eligible task가 있고 global cooldown이 없으면 configured scheduler wake-up hook을 warning-only로 실행

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
  "codex_resume_command": ["codex", "exec", "--sandbox", "workspace-write", "resume", "{session_id}", "--json"],
  "post_mutation_trigger_command": []
}
```

`workspace-write`를 기본으로 둠. non-interactive batch 작업은 일반적으로 파일 수정을 해야 하며, read-only sandbox에서는 수정 task가 실패함.

기본 공개 예시 [examples/config.example.json](../examples/config.example.json)은 이 safe default를 유지함.
완전 비대화형 운영이 필요하고 운영자가 full local access 위험을 수용한 경우에만
[examples/config.automation.example.json](../examples/config.automation.example.json)을 참고할 수 있음.
Automation 예시는 `--dangerously-bypass-approvals-and-sandbox`를 사용해 approval prompt와 sandbox를 모두 비활성화함.
이 설정은 해당 사용자 권한으로 접근 가능한 로컬 파일과 명령에 제한 없는 접근을 허용하므로, trusted queue와 명시적으로 관리되는 scheduler에서만 사용해야 함.

Automation mode는 approval prompt 대기와 sandbox 권한 부족으로 인한 반복 실패를 줄여 pending task와 lock 정체를 완화할 수 있음.
대신 실행 후 review 책임은 더 크며, `summary`, 필요한 경우 `transcript`, 대상 repository의 검증 명령, `doctor`를 이용해 결과와 runner 상태를 확인한 뒤 `accept`를 기록해야 함.

launchd 같은 scheduler는 사용자 shell `PATH`를 그대로 상속하지 않을 수 있음. 운영 config에서는 `codex` 실행 파일을 절대 경로로 지정할 수 있어야 함.

`post_mutation_trigger_command`는 queue mutation 이후, 그리고 `run-next`가 task 하나를 처리한 뒤 eligible follow-up work가 있을 때 외부 scheduler/runner를 즉시 깨우기 위한 optional hook임. 값은 shell string이 아니라 argv string list이며 기본값은 빈 list로 disabled임. 구현은 shell expansion을 하지 않고 짧은 timeout으로 실행함. 실패, non-zero exit, timeout은 stderr warning으로만 표시하고 원래 mutation 또는 처리된 task 결과를 되돌리지 않음.

hook은 durable task JSON write와 event emission이 끝난 뒤 실행함. `enqueue`, `accept`, `reject`, `resolve`, `archive` 같은 queue-mutating command에서 호출함. `run-next`는 task 하나를 terminal 또는 resumable state로 갱신하고 lock을 해제한 뒤, global cooldown이 없고 `select_next_task` 기준 eligible `runnable` 또는 `needs_resume` task가 있을 때만 hook을 호출함. Empty queue, active global cooldown, dependency-blocked-only queue, task cooldown뿐인 queue, 방금 처리한 task가 아직 cooldown 중인 경우에는 호출하지 않음. `list`, `show`, `summary`, `review-bundle`, `logs`, `transcript`, `doctor`, `events`, `rate-limits`, `prune` 같은 read-only 또는 cleanup command에서는 호출하지 않음. 목적은 polling interval로 인한 latency를 줄이는 것이며, polling은 fallback으로 계속 유지함. duplicate wake-up은 안전해야 함. `run-next`가 lock, cooldown, empty queue, dependency, single-task execution 규칙을 계속 강제하기 때문임.

예시:

```json
{
  "post_mutation_trigger_command": ["launchctl", "kickstart", "gui/UID/com.example.codex-batch-runner"]
}
```

launchd wake-up 용도로는 active runner를 kill하지 않는 `launchctl kickstart gui/UID/LABEL` 형식을 사용함.
`-k` force-kill option은 이 hook에 사용하지 않음. 해당 option은 task가 `status=running`으로 기록된 뒤 final result 처리 전에 실행 중인 runner를 종료해 running task 또는 lock state를 남길 수 있음.

```json
{
  "post_mutation_trigger_command": ["systemctl", "--user", "start", "codex-batch-runner.service"]
}
```

Codex CLI 0.136 JSONL은 `thread.started.thread_id`를 내보내며, 이 값은 `codex exec resume <thread_id>`에 사용할 수 있음. runner는 명시적인 `session_id`가 없으면 `thread_id`를 resume id fallback으로 저장함.

`needs_resume`인데 resume id를 찾지 못하면 신규 `codex exec`로 이어가되, prompt wrapper에 이전 summary와 `next_prompt`를 포함합니다. 이 경우 task metadata에 `resume_unavailable: true`, `resume_unavailable_at`, `resume_unavailable_attempts`를 남깁니다.

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
  "verification": ["string"],
  "commits": ["string, optional"],
  "push_status": "string or object, optional"
}
```

`commits`와 `push_status`는 optional result metadata임. 기존 final JSON처럼 이 필드를 생략해도 파싱과 상태 전이는 동일하게 동작해야 함. 포함된 optional field는 runner가 의미를 강제 변환하지 않고 `last_result`에 그대로 저장함.

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
cbr enqueue --cwd /repo --project project-id --category implementation --label queue --created-by operator --prompt-file prompt.md
cbr list
cbr list --project project-id
cbr list --project-root /repo
cbr list --cwd /repo
cbr list --category implementation
cbr list --label queue
cbr list --verbose
cbr run-next
cbr show TASK_ID
cbr summary TASK_ID
cbr logs TASK_ID
cbr transcript TASK_ID
cbr archive TASK_ID
cbr accept TASK_ID --reason "verified"
cbr reject TASK_ID --reason "missing tests"
cbr resolve TASK_ID --resolution manual --reason "handled outside cbr"
cbr list --all
cbr list --unreviewed
cbr list --needs-review
cbr rate-limits
cbr events
cbr events --task-id TASK_ID --limit 20
cbr events --json
cbr doctor
cbr doctor --json
cbr prune
cbr prune --older-than-days 60 --json
cbr prune --apply
```

공통 option:

```bash
--config path/to/config.json
```

config 탐색 순서:

1. `--config` 명시값
2. `CBR_CONFIG` 환경 변수
3. `~/.config/codex-batch-runner/config.json`
4. 현재 작업 디렉터리 기준 기본값

자동화와 launchd에서는 명시적 config 또는 절대 경로 기반 호출을 권장함. 운영자가 직접 viewer/review 용도로 사용할 때는 `cbr` console script를 설치하고 사용자 config 자동 탐색을 활용할 수 있음.

`cbr list` 기본 출력은 운영자가 신경 써야 할 task 중심으로 유지함. `archived`와 `completed + accepted`는 기본 출력에서 숨기고, `completed + unreviewed/rejected/needs_followup`은 검토 대상이므로 표시함. 전체 조회가 필요하면 `--all`을 사용함. `failed` task는 한 줄짜리 `last_error` 요약을 함께 표시함.

사람이 읽는 `cbr list` 출력은 header가 있는 tab-separated table입니다. 기본 list, `--all`, filter list에 같은 형식을 적용합니다. 최소 열은 `ID`, `STATUS`, `PROJECT`, `ATTEMPTS`, `DEPS`, `FLAGS`입니다. `PROJECT`는 task metadata fallback 규칙으로 계산한 project id입니다. `DEPS`는 `depends_on` id를 쉼표로 연결하고 없으면 `-`를 표시합니다. `FLAGS`는 `cooldown`, `blocked_by=...`, `last_error=...`, `resolution=...`, `review=...`를 공백으로 연결하고 없으면 `-`를 표시합니다. table의 빈 scalar 값은 `-`로 표시하며 값을 자르지 않습니다. 자동화나 스크립트는 table format에 의존하지 말고 `--json`을 사용해야 합니다.

`cbr list --verbose`는 사람용 table에 `LAST_RESULT`, `LAST_RUN`, `LAST_ERROR` 열을 추가합니다. `LAST_RESULT`는 `last_result.status`, `last_result.summary`, optional `commits`/`push_status`, task `git_status`의 한 줄 요약을, `LAST_RUN`은 `last_run.command_kind`, `returncode`, `duration_seconds`를, `LAST_ERROR`는 `last_error`의 한 줄 요약을 표시합니다. 누락된 값은 `-`로 표시하고 transcript 또는 raw JSONL 내용은 출력하지 않습니다. `--json`을 함께 사용하면 verbose 열을 만들지 않고 기존 JSON 배열만 출력합니다.

`cbr list --unreviewed`는 `completed + unreviewed` task만 표시함. `cbr list --needs-review`는 `completed + unreviewed/rejected/needs_followup` task를 표시함.

`cbr archive TASK_ID`는 task 파일을 삭제하지 않고 `status=archived`, `previous_status`, `archived_at`을 기록함.

Successful queue mutations run the optional `post_mutation_trigger_command` after durable writes. This includes `enqueue`, `accept`, `reject`, `resolve`, and `archive`. After `run-next` processes one task and releases the runner lock, it may run the same wake-up hook when eligible follow-up work remains and no global cooldown is active. Read-only commands, empty or cooldown `run-next` exits, and `prune` do not run the trigger.

`cbr summary TASK_ID`는 task metadata, dependency blocked 상태, dependency blocker reason, `last_result.summary`, optional commits/push_status, changed files, verification, task `git_status`, last_error, next_prompt, log path를 transcript보다 짧은 Markdown 형식으로 표시합니다.

`cbr review-bundle TASK_ID`는 현재 대화 context 없이 task 결과를 재검토하기 위한 read-only bundle을 stdout에 생성합니다. 기본 출력은 Markdown-like human report이고, `--json`은 같은 정보를 structured JSON으로 출력합니다. 포함 정보는 task metadata, sanitized prompt excerpt, status/review/resolution, dependencies와 blockers, `last_result`, `last_run`, changed files, verification, `last_error`, relevant log paths, task `git_status`, local git repository state, inferable commit information, safely scoped commit 또는 working tree diff/stat, public/private safety policy입니다. commit hash를 명확히 하나로 추론할 수 있으면 해당 commit의 subject/stat/diff를 포함하고, 추론이 여러 개이거나 모호하면 diff를 생략하고 ambiguity를 보고합니다. commit을 추론할 수 없고 task repository의 working tree가 dirty이면 working tree diff/stat만 포함합니다. repository가 아니거나 git metadata를 읽을 수 없으면 fallback warning을 보고합니다. 원본 JSONL transcript 내용은 기본적으로 포함하지 않고, 명령은 Codex 호출, enqueue, accept/reject, task state 변경을 수행하지 않습니다.

`cbr review-next --dry-run`은 `status=completed`이고 `review_status`가 `unreviewed`, `rejected`, `needs_followup`인 task 중 가장 오래된 항목 하나를 선택해 concise review report를 출력합니다. 선택 기준 timestamp는 `completed_at`, fallback으로 `updated_at`, `created_at`, `id`를 사용합니다. `--project`, `--project-root`, `--category`, `--label`은 `list`와 같은 metadata fallback 규칙으로 후보를 좁힙니다. `--json`은 human report와 같은 정보를 structured JSON으로 출력합니다.

`review-next --dry-run` report는 selected 여부, candidate count, task id, review status, dependency summary, review bundle 핵심 요약, mechanical gates를 포함합니다. Gate는 task status completed, final result status completed, `last_error` 없음, verification list 존재, changed_files list 존재, dependency ready, git working tree clean, unpushed commit 없음 여부를 확인합니다. Dependency summary는 config의 `dependency_requires_accepted_review` 적용 여부와 blocker reason(`not_completed`, `not_accepted`)을 포함합니다. 이 명령은 read-only이며 task JSON, review_status, event log, post-mutation trigger를 변경하지 않고, follow-up task를 enqueue하지 않으며, Codex 또는 reviewer Codex를 호출하지 않습니다. `--dry-run` 없이 실행하면 auto-apply가 아직 구현되지 않았다는 명확한 오류로 종료합니다.

runner는 각 Codex 호출 후 task에 `last_run` metadata를 저장합니다. 필드는 `command_kind`, `returncode`, `started_at`, `finished_at`, `duration_seconds`, `resume_id_used`, `log_path`입니다. task-level counters로 `run_count`, `resume_count`, `rate_limit_count`, `failure_count`도 유지합니다.

정상 final JSON 응답을 받은 뒤 runner는 task `cwd`에서 네트워크를 사용하지 않는 local Git inspection을 시도할 수 있습니다. repository이면 `git_status`에 `branch`, `upstream`, `comparison_ref`, `ahead`, `behind`, `has_unpushed`, `dirty`, `unpushed_commits`, `warnings`, `inspected_at`을 저장합니다. 비교 기준은 configured upstream을 우선하고, 없으면 local `origin/<branch>` 또는 `origin/main` ref를 사용합니다. runner는 push를 수행하지 않으며, 이 metadata는 운영자가 남은 push 작업을 판단하기 위한 보고용입니다.

`cbr follow TASK_ID`는 저장 중인 attempt JSONL을 read-only polling으로 관찰하는 operator view입니다. `--lines N`은 처음 표시할 기존 JSONL line 수를 제한하고, `--poll-interval SECONDS`는 새 log path와 append된 event 확인 주기를 정합니다. 출력은 compact human stream이며 assistant message, command start/finish, command exit code, final JSON, `turn.failed`/`error`/rate-limit marker 요약을 포함합니다. 사용자 prompt, session/thread id, obvious secret, credential, token, personal user path는 transcript/review sanitization pattern으로 redacted됩니다. task가 `running`이 아니고 더 읽을 새 이벤트가 없으면 종료합니다. 이 명령은 task JSON, runner state, event log, post-mutation trigger를 변경하지 않고 Codex를 호출하지 않습니다.

`cbr transcript TASK_ID`는 저장된 cbr JSONL 로그와, `session_id` 또는 `thread_id`로 찾을 수 있는 Codex 원본 세션 로그에서 주요 대화, tool 호출, patch, final/error event를 사람이 읽기 좋은 형태로 재구성함. `--raw`는 원본 JSONL을 출력함.

`cbr accept TASK_ID`는 `completed` task에만 `review_status=accepted`를 기록함. `cbr reject TASK_ID`는 `review_status=rejected`를 기록하고, `--follow-up`을 붙이면 `review_status=needs_followup`을 기록함. Reject는 운영자가 비정상 실행 결과나 후속 처리 필요성을 표시할 수 있도록 non-completed task에서도 허용함.

`cbr resolve TASK_ID --resolution VALUE`는 `failed` 또는 `blocked_user` task에 운영상 처리 결정을 기록합니다. 허용값은 `wont_fix`, `superseded`, `manual`, `smoke`, `duplicate`입니다. resolution이 있는 failed/blocked task는 기본 `cbr list`에서 숨기고, `cbr list --all` 또는 `cbr summary TASK_ID`에서 확인합니다.

`cbr rate-limits`는 저장된 sanitized rate-limit evidence event를 조회함. `--json`을 붙이면 evidence JSON 배열을 출력함.

`cbr events`는 append-only event log에서 최근 event를 조회함. 기본 출력은 human-readable table이고, `--json`은 event object 배열을 출력함. `--task-id`로 특정 task event만 필터링할 수 있고 `--limit`으로 최대 출력 개수를 제한함.

`cbr doctor`는 저비용 health check임. resolved `queue_dir`, `log_dir`, `event_dir`, `lock_file`, `state_file` 경로, runtime directory 접근 가능 여부, configured Codex executable path, resolved Codex executable path, executable availability, bounded `codex --version` output, global cooldown, active lock age/pid/liveness, status별 task 수, needs-review completed task 수, resolved failed/blocked task 수, runnable task 수, cooldown task 수를 표시함. Version 확인은 configured executable에 `--version`만 붙여 짧은 timeout으로 실행하며, `codex exec`를 호출하지 않음. Version command 실패, 빈 output, timeout은 warning으로 보고하고 doctor 실패로 취급하지 않음. configured/current project root가 git repository 안에 있으면 branch, dirty status, upstream 또는 local `origin/main` 대비 ahead/behind count도 표시함. git check는 local repository metadata만 읽고 fetch/pull 같은 network operation을 실행하지 않음. git executable 없음, git repository 아님, upstream 없음, remote ref 조회 불가 같은 상태는 warning으로 보고하고 doctor 실패로 취급하지 않음. `--json`을 붙이면 같은 정보를 JSON으로 출력함. error check가 있으면 non-zero로 종료하고 warning은 종료 코드에 영향을 주지 않음.

Runner lock recovery treats a lock as recoverable immediately when metadata contains a pid for the same hostname and that pid is no longer alive. Unknown host, missing/invalid pid, invalid metadata, and cross-host locks fall back to the age-based stale threshold.

Doctor는 기본적으로 Codex application bundle 안의 별도 executable과 configured CLI를 비교하지 않음. macOS나 특정 app install layout을 가정하지 않기 위함임. 운영자가 app-bundled CLI와 standalone CLI 차이를 확인해야 하는 환경에서는 별도 수동 조사로 처리함. Routine doctor는 대형 binary hash를 계산하지 않음. Hash는 향후 `--verbose` 또는 deep diagnostic check에서 명시적으로 요청할 때만 고려함.

## Codex CLI maintenance policy

현재 runner는 Codex CLI automatic update를 수행하지 않음. Update는 JSONL schema, resume semantics, permission/sandbox behavior, final response handling을 바꿀 수 있음. 잘못된 update 뒤 자동 실행이 계속되면 usage-limit tokens를 낭비할 수 있고, 설치 방식에 따라 rollback path가 불명확할 수 있음.

권장 운영 정책:

- Queue가 idle일 때만 CLI version check 또는 update를 수행함.
- Idle 기준은 active runner lock 없음, active global cooldown 없음, `runnable`/`needs_resume`/`running` task 없음.
- Manual update 전후에 `cbr doctor --json` 결과를 기록해 configured executable, resolved executable, `codex --version` output을 비교 가능하게 함.
- Manual update 뒤에는 `cbr doctor`와 runner deployment에 맞는 focused tests 또는 smoke command를 실행한 뒤 queued work를 재개함.
- Automatic update는 별도 rollback strategy, compatibility smoke, idle gate, operator approval flow가 설계되기 전까지 추가하지 않음.

`cbr prune`은 오래된 cleanup 후보를 보고하거나 삭제합니다. 기본 동작은 dry-run이며 `--apply`를 명시하지 않으면 파일을 삭제하지 않습니다. Task/log 후보는 보수적으로 `status=archived` task와 `status=completed && review_status=accepted` task 중 `--older-than-days`보다 오래된 항목으로 제한합니다. Event 후보는 configured `event_dir` 아래에서 `--older-than-days`보다 오래된 `*.jsonl` 파일로 제한합니다. 기본 age는 30일입니다. Optional `notifier_cursor_state_paths` config 값 또는 반복 가능한 `--notifier-cursor-state` flag로 local-only notifier cursor state JSON 파일을 지정할 수 있습니다. 기본값은 빈 목록입니다.

후보 age 기준 timestamp는 `archived` task에서는 `archived_at`, fallback으로 `updated_at`, `completed_at`, `reviewed_at`, `created_at`을 사용하고, accepted completed task에서는 `reviewed_at`, fallback으로 `completed_at`, `updated_at`, `created_at`을 사용합니다. timestamp가 없거나 파싱할 수 없으면 삭제 후보에서 제외합니다.

report에는 task JSON 파일과 task의 `log_paths`, `last_run.log_path`를 중복 제거해 포함하고, event JSONL 후보는 별도 `event_candidates` section으로 포함합니다. `--json`은 machine-readable report를 출력합니다. `--apply`가 있어도 resolved path가 configured `queue_dir`, `log_dir`, 또는 `event_dir` 밖이면 삭제하지 않습니다. path containment check는 resolved path 기준으로 명시적으로 수행하며, regular file이 아닌 path도 삭제하지 않습니다. Event pruning does not delete notifier cursor/state files or other non-JSONL files.

Notifier cursor state schema is generic and does not require a Telegram dependency. Version 1 accepts either:

```json
{
  "schema_version": 1,
  "current_event_file": ".codex-batch-runner/events/2000-01-02.jsonl",
  "current_byte_offset": 1234
}
```

or:

```json
{
  "schema_version": 1,
  "last_processed_event_file": ".codex-batch-runner/events/2000-01-01.jsonl"
}
```

`current_event_file` and `last_processed_event_file` may be absolute paths or paths relative to configured `event_dir`, but must resolve inside `event_dir`. If `current_byte_offset` is absent or is smaller than the current event file size, that current file is treated as not fully processed. Files after the current cursor file, or after `last_processed_event_file` when only whole-file progress is recorded, are also skipped. If a configured cursor state file is missing, malformed, unreadable, or references files outside `event_dir`, `cbr prune` emits a warning and skips event JSONL deletion for that safety decision while still reporting task/log cleanup candidates.

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
- optional `post_mutation_trigger_command`로 queue mutation 직후 또는 eligible follow-up work가 남은 task 처리 직후 `launchctl kickstart` 호출 가능
- runner 내부에서 lock, dependency, cooldown, empty queue를 판단
- 실행할 작업이 없으면 즉시 종료
- rate-limit 발생 시 launchd interval을 바꾸지 않고 runner 내부 global cooldown으로 Codex 호출을 막음

cron 예시는 portable fallback으로만 문서화함.

Linux systemd user service 운영에서는 같은 hook에 `["systemctl", "--user", "start", "codex-batch-runner.service"]`를 사용할 수 있음. timer/cron polling은 fallback으로 유지함.

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
