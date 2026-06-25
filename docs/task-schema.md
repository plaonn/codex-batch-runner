# Task Schema and Dependency Contract

이 문서는 task JSON schema, task/review 상태, dependency readiness, project routing metadata를 정의합니다. 핵심 스펙 index는 [spec.md](spec.md)입니다.

## Task schema

각 task는 사람이 읽을 수 있는 개별 JSON 파일로 저장함.

```json
{
  "id": "task-20260620-001",
  "title": "작업 제목",
  "description": "선택 설명",
  "status": "runnable",
  "review_status": null,
  "reviewed_at": null,
  "review_reason": null,
  "prompt": "작업 지시문",
  "execution_profile": "normal",
  "model": null,
  "codex_profile": null,
  "codex_config_overrides": {},
  "token_budget_hint": null,
  "next_prompt": null,
  "cwd": "/path/to/repo",
  "execution_backend": "codex",
  "shell_command": null,
  "shell_timeout_seconds": null,
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

- `title`
- `description`
- `next_prompt`
- `session_id`
- `thread_id`
- `execution_backend`: `codex` 또는 `shell`. 생략된 기존 task는 `codex`로 해석합니다.
- `shell_command`: shell backend가 실행할 non-empty argv string list. Raw shell string은 저장하지 않고, shell 기능이 필요하면 `["bash", "-lc", "..."]`처럼 명시합니다.
- `shell_timeout_seconds`: shell backend task-specific timeout. 없으면 config `shell_task_timeout_seconds` 기본값을 사용합니다.
- bounded review/fix chain metadata:
  - `root_task_id`
  - `parent_task_id`
  - `review_cycle`
  - `review_attempts`
  - `fix_attempts`
  - `chain_status`
  - `review_findings`
  - `last_review_decision`
  - `auto_fix_allowed`
  - `auto_fix_budget`
  - `last_auto_fix_task_id`
  - `finding_fingerprints`
- `max_attempts`
- `cooldown_until`
- `last_error`
- `started_at`
- `completed_at`
- `log_paths`
- `review_status`
- `reviewed_at`
- `review_reason`
- `execution_profile`: config `execution_profiles`에 정의된 cbr profile 이름
- `model`: task-level Codex `--model` override
- `codex_profile`: task-level Codex `--profile` override
- `codex_config_overrides`: task-level allowlisted Codex `-c key=value` override
- `token_budget_hint`: 운영자가 보는 비강제 token/cost hint


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

운영 모델상 `completed + unreviewed`, cleanup되지 않은 `completed + rejected`, `completed + needs_followup`, accepted-but-unapplied worktree task는 아직 처리해야 할 task로 봄. 기본 `cbr list`는 `archived`, applied worktree task, non-worktree `completed + accepted`, discard cleanup이 완료된 rejected worktree task를 숨기고, 검토가 끝나지 않았거나 integration target에 아직 반영되지 않은 completed task는 기본 출력에 표시함. `completed + needs_followup`은 후속 task가 연결되지 않았으면 create/link follow-up 또는 explicit `resolve`가 다음 action으로 표시되고, 연결된 follow-up task가 있으면 active/review-needed/accepted/blocked 상태를 기준으로 다음 action이 표시됨. Discard cleanup된 rejected worktree task도 내부 `review_status=rejected`는 유지하므로 routing-report와 모델 평가에서는 rejected outcome으로 집계됨.


## Project routing metadata

여러 프로젝트가 하나의 중앙 queue를 공유하면 review 대상 판정을 위해 task를 하나씩 열람하는 방식은 토큰과 시간이 낭비됩니다. task 등록 시 review routing metadata를 함께 저장하고, list 단계에서 먼저 좁혀 볼 수 있게 합니다.

구현 필드:

- `schema_version`: task schema 호환성 판단용 정수
- `project_root`: task가 속한 git root. `git rev-parse --show-toplevel` 성공 시 그 값을 사용하고, 실패하면 `cwd`로 fallback합니다.
- `project_id`: 기본값은 `project_root` basename입니다. 필요하면 enqueue option으로 override할 수 있습니다.
- `category`: `implementation`, `review`, `smoke`, `maintenance`, `docs` 같은 운영 분류
- `labels`: 사람이 지정하거나 skill이 추론한 짧은 태그 목록
- `created_by`: `enqueue-codex-batch`, `operator`, `test` 같은 등록 주체
- `title`: 사람이 목록에서 구분하기 쉬운 짧은 제목. 없으면 prompt 첫 줄, 그것도 없으면 id로 fallback합니다.
- `description`: 사람이 읽는 선택 설명. 실행 prompt를 대체하지 않습니다.

기존 task와 호환되어야 합니다. metadata가 없는 task는 `cwd`를 `project_root` fallback으로 사용하고, `project_id`는 fallback root의 basename으로 계산하며, `category`와 `labels`는 비워 둡니다. `title`이 없는 task는 list 표시에서 prompt 첫 줄 또는 id를 fallback으로 사용합니다.

관련 CLI:

```bash
cbr enqueue --cwd /path/to/repo --project codex-batch-runner --category implementation --label rate-limit --created-by enqueue-codex-batch --title "Rate-limit handling" --description "Cooldown and retry behavior" --prompt-file task.md
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

기본 dependency readiness policy는 non-worktree dependency에 대해 기존 동작과 호환되도록 dependency task의 `status=completed`만 요구함. Config `dependency_requires_accepted_review` 기본값은 `false`임. 따라서 non-worktree dependency가 `completed`이면 `review_status`가 아직 `unreviewed` 또는 운영 화면에서 `awaiting_review`로 표시되는 completed-but-unreviewed 상태여도 ready로 판단함.

Worktree-backed dependency는 더 엄격함. `execution_mode=git_worktree`인 completed task는 `review_status=accepted`이고 `execution_apply_status=applied`가 기록된 뒤에만 ready임. Accepted-but-not-applied worktree result는 아직 integration target에 반영되지 않았으므로 blocker reason `not_applied`로 남고, completed-but-unaccepted worktree result는 blocker reason `not_accepted`로 남음. 이 규칙은 `dependency_requires_accepted_review=false` 호환 모드에서도 적용되며, stricter accepted-review mode는 non-worktree dependency까지 accepted review를 요구하는 추가 gate임.

이 기본값은 batch 운영에서 의도한 throughput/latency 선택임. 독립적인 후속 작업은 review backlog가 있다는 이유만으로 멈추지 않아야 하며, runner는 completed 결과를 기반으로 다음 eligible work를 계속 처리할 수 있어야 함. 대신 실행 결과의 품질 확인과 공개 저장소 안전성 판단은 review workflow가 별도로 수행하고, 운영자는 필요할 때 `accept`, `reject`, `reject --follow-up`으로 review state를 정리함.

`dependency_requires_accepted_review=true`이면 non-worktree dependency task도 `status=completed`와 `review_status=accepted`를 모두 만족해야 ready임. 이때 dependency가 `completed`이지만 `review_status`가 `accepted`가 아니면 runner는 dependent task를 건너뛰고 reporting은 blocker reason을 `not_accepted`로 표시함. dependency가 없거나 `completed`가 아니면 blocker reason은 `not_completed`임.

Accepted-review dependency mode는 dependent work에 더 엄격하고 안전한 정책임. 검토가 완료된 결과만 후속 작업의 전제로 사용하므로 잘못된 completed 결과가 이어지는 작업에 전파될 가능성을 줄임. 그 대가로 review backlog가 있을 때 처리량이 낮아지고, 더 많은 task가 dependency blocked 상태로 남을 수 있음.

의존 task가 `failed` 또는 `blocked_user`인 경우 dependent task를 자동 실패시키지 않음. `list` 또는 `show`에서 dependency blocked 상태를 표시하고 runner는 해당 task를 건너뜀.

마이그레이션은 기본값 `false`로 기존 queue behavior를 유지하면서 completed task의 review state를 정리한 뒤, operator가 accepted review를 dependency gate로 쓸 준비가 되었을 때 `dependency_requires_accepted_review=true`를 설정하는 순서로 진행함. 전환 직후 completed-but-unaccepted dependency를 가진 child task는 runnable 목록에서 제외될 수 있으며, `list`, `summary`, `review-bundle`, `review-next`, `doctor` report에서 blocker reason을 확인함.
