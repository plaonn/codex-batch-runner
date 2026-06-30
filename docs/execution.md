# Execution Contract

이 문서는 model requirement vector, shell backend, capacity/priority, queue admission, Codex command wrapper, watchdog, lock, atomic write, rate-limit, queue mutation control plane을 정의합니다. 핵심 스펙 index는 [spec.md](spec.md)입니다.

## Model Requirements

Task JSON은 provider/model/profile 이름이 아니라 `model_requirement_vector`를 저장합니다. 이 벡터는 작업이 요구하는 모델 특성이고, 현재 설치된 Codex 모델 선택은 config의 `model_selection_rules`와 `default_execution_config`가 실행 직전에 해석합니다.

Config는 선택적으로 아래 field를 가질 수 있습니다.

- `default_model_requirement_vector`: task에 explicit vector가 없을 때 사용할 기본 요구 벡터
- `review_model_requirement_vector`: reviewer Codex 호출에 사용할 요구 벡터
- `default_execution_config`: selection rule이 match되지 않을 때 사용할 local Codex 실행 설정
- `model_selection_rules`: requirement dimension match 조건과 local Codex `model`, `codex_profile`, allowlisted `config_overrides`, `budget_hint` mapping

Task는 `model_requirement_vector`를 저장할 수 있습니다. 없으면 enqueue 단계에서 routing metadata를 기반으로 deterministic vector를 생성합니다. 직접 model/profile/config override를 task에 저장하지 않습니다.

Task는 model requirement 결정을 나중에 outcome과 대조할 수 있도록 선택적 audit metadata를 저장할 수 있습니다.

- `routing_reason`: operator 또는 enqueue caller가 남긴 public-safe routing decision reason.
- `routing_risk_factors`: public-safe risk factor 문자열 목록. Enqueue CLI에서는 repeatable option으로 누적합니다.
- `routing_experiment`: routing cohort label. 현재 권장 label은 `baseline`, `downshift_probe`, `upshift_guard`, `manual`이지만 policy enforcement 대상은 아닙니다.
- `routing_size`: pre-enqueue work size estimate. 허용값은 `tiny`, `small`, `medium`, `large`, `xlarge`입니다.
- `routing_risk`: pre-enqueue implementation risk estimate. 허용값은 `low`, `medium`, `high`입니다.
- `verification_scope`: expected verification scope를 나타내는 public-safe 짧은 문자열 목록. 허용값은 `none`, `docs`, `lint`, `typecheck`, `unit`, `integration`, `e2e`, `smoke`, `manual`, `build`이고, Enqueue CLI에서는 repeatable option으로 누적합니다.

이 field들은 task selection을 변경하지 않지만, missing `model_requirement_vector`를 파생할 때 입력으로 사용합니다. Prompt text, raw runtime state, session/thread id, credential, private local path를 넣지 않는 공개 가능한 짧은 metadata로만 사용합니다.

Command builder는 resolved execution config option을 `codex exec` 뒤에 삽입합니다. 예를 들어 `codex exec --sandbox workspace-write resume {session_id} --json` template는 selection rule 적용 후 `codex exec --model MODEL --profile PROFILE --sandbox workspace-write resume SESSION --json PROMPT` 형태가 됩니다. `resume {session_id}` 순서는 유지해야 합니다.

`config_overrides`는 임의 `-c` 주입을 허용하지 않습니다. 현재 allowlist는 `model_reasoning_effort`, `model_reasoning_summary`, `model_verbosity`입니다. Codex CLI가 dedicated `--reasoning-effort` flag를 노출하지 않는 버전이 있으므로 reasoning 관련 override는 이 allowlist 안에서만 보수적으로 허용합니다. Allowlist 밖의 key는 config load 단계에서 오류로 처리합니다.

High-risk category/label에는 보수적 vector derivation을 적용합니다. category 또는 label이 `runner`, `runner-state`, `lock`, `resume`, `reviewer-codex`, `reviewer-safety`, `queue-mutation`, `worktree-critical`, `worktree-apply`, `worktree-recovery`, `stale-base`, `rebase` 중 하나이면 `reasoning_depth`, `context_need`, `tool_reliability`를 높게 둡니다. 일반 routing label인 `worktree`, `docs`, `document`는 단독으로 high requirement를 trigger하지 않습니다.

Low-risk docs-only routing metadata에는 낮은 reasoning depth와 높은 cost sensitivity vector를 적용합니다. Unit, integration, build, e2e처럼 검증 범위가 넓은 작업은 이 low-cost candidate 대상이 아닙니다.

`cbr list`와 `cbr summary`는 explicit `model_requirement_vector`를 표시할 수 있습니다. `cbr summary`와 `review-bundle`은 routing decision metadata도 sanitized task metadata로 표시해 review outcome과 original routing decision을 대조할 수 있게 합니다. Runner는 각 Codex 실행의 `last_run.resolved_execution_config`에 worker role, selection rule, model/profile 존재 여부, override key 이름, 사용한 requirement vector를 기록합니다. `cbr doctor`는 configured model selection rule 이름과 override key 이름만 표시하고 override 값은 출력하지 않습니다.

`cbr routing-report`는 model requirement와 selection rule을 조정하기 위한 read-only evidence surface입니다. 명령은 queue task를 model requirement, model selection rule, category, label, requirement/category 조합, routing experiment, routing size, routing risk, routing risk factor, verification scope, routing decision tuple, requirement/routing decision tuple, selection/routing decision tuple, low-cost candidate 신호, requirement/experiment 조합, provider resource evidence로 집계하고 accepted count, first-pass accepted count, needs-fix/rejected rate, reviewer decision count, auto-fix task frequency, attempts, run count, duration 기반 cost proxy를 출력합니다. Provider resource evidence는 현재 Codex provider 불확실성을 명시하기 위해 기본적으로 `provider_id=codex`, `quota_boundary=unknown`, `sharing_assumption=not_independent`를 사용합니다. 이 evidence는 local `capacity_pool`, worker/reviewer role, legacy profile name과 분리되며 provider quota bucket을 추론하지 않습니다. Report는 task JSON, event log, review status를 변경하지 않고 Codex 또는 reviewer Codex를 호출하지 않습니다. 운영자는 이 결과를 보고 requirement derivation 또는 selection rule 변경을 별도 policy change로 반영합니다.

`routing-report`는 의사결정 근거가 아니라 운영 진단입니다. task 선택, dependency readiness, review acceptance, cleanup/apply/archiving, cooldown, reject/resolve, run/claim 정책을 바꾸지 않습니다. 보고가 보여주지 않는 항목은 다음과 같습니다.
- 개별 task의 raw prompt, full transcript/JSONL, raw log body
- 다음 실행에서 바꿔 적용할 patch나 실행 계획
- 안전성 판단 근거의 원문 근거 데이터(요약되지 않은 증거)
- local `capacity_pool`에서 추론한 provider quota/resource identity
- 정책 변경 자동 실행

그래서 `routing-report`는 운영자 판단의 입력값으로만 쓰고, policy 변경은 별도 제어면에서 수동으로 수행해야 합니다. 즉 advisory + read-only입니다.


## Shell execution backend

`execution_backend=shell` task는 Codex를 호출하지 않고 local argv list command를 실행합니다. 기본값은 backward-compatible `codex`입니다. Shell backend는 simple verification, maintenance, dependency gate용이며 token-free queue task로 동작합니다.

Enqueue CLI는 `--backend shell`과 함께 `--command-json '["cmd", "arg"]'` 또는 마지막 option인 `--command cmd arg`를 받습니다. cbr는 문자열을 암묵적으로 shell 평가하지 않습니다. Pipe, redirect, `&&` 같은 shell syntax가 필요하면 command argv에 `bash -lc` 또는 동등한 explicit shell invocation을 넣어야 합니다.

Runner는 shell task에도 기존 queue ordering, dependency readiness, cooldown skip, runner lock, stale running recovery, worktree cwd adapter, attempts/run count, log path, status transition event, post-run wake trigger를 적용합니다. Exit code `0`은 `completed`와 `review_status=unreviewed`를 기록하고, nonzero exit, executable failure, timeout은 `failed`를 기록합니다. Downstream task는 기존 dependency rule 때문에 failed shell dependency를 unmet dependency로 봅니다.

Shell attempt log는 stdout/stderr 전체를 task log file에 저장합니다. Task JSON의 `last_run`은 `execution_backend=shell`, `command_kind=shell`, argv command, returncode, started/finished time, duration, timeout flag/seconds, stdout/stderr byte count, log path만 저장합니다. `last_result`는 Codex final JSON과 같은 review/list surface에서 읽을 수 있도록 `task_id`, terminal `status`, compact `summary`, empty `changed_files`, verification summary를 저장합니다. Event payload에는 raw stdout/stderr를 넣지 않고 sanitized summary/count/path metadata만 남깁니다.

`shell_task_timeout_seconds` config 기본값은 `900`입니다. `--shell-timeout` 또는 task `shell_timeout_seconds`가 있으면 해당 task에만 override합니다.

Codex CLI update 같은 guarded maintenance workflow는 runner-level maintenance로 처리합니다. Shell task는 프로젝트별 ordered dependency gate로 사용할 수 있지만, runner pause를 잡고 queue idle gate를 확인하는 solo maintenance mode 자체는 shell backend가 아니라 별도 maintenance command가 담당합니다.


## Profile routing optimization policy

Profile routing 최적화는 비용을 줄이기 위한 운영 루프이지만, task prompt와 verification 요구를 낮추는 방식으로 사용하지 않습니다. Runner는 routing-report 결과를 근거로 자동 policy mutation을 수행하지 않습니다. 운영자 또는 별도 control-plane 작업이 명시적으로 repo-local 기준, enqueue skill 기준, 또는 config를 수정할 때만 routing 기준이 바뀝니다.

기본 운영 원칙:

- `normal`은 일반 implementation fallback입니다. 명시 profile이 없고 high-risk fallback에 걸리지 않는 작업은 우선 normal로 처리합니다.
- `deep`은 손상 비용이 큰 작업의 guardrail입니다. runner state, lock, queue mutation, reviewer safety, worktree apply/recovery, stale-base/rebase, dependency semantics, 자동 review/fix loop처럼 control-plane 의미가 있는 작업은 성공 사례가 누적되어도 기본적으로 deep을 유지합니다.
- `small` 또는 동등한 저비용 profile은 bounded, low-blast-radius 작업에서만 사용합니다. 예시는 공개 문서의 작은 수정, 예제/README 보강, 단순 textual cleanup처럼 실패해도 리뷰 단계에서 쉽게 감지되고 main apply 전 되돌릴 수 있는 작업입니다. `routing_size=tiny|small`, `routing_risk=low`, `verification_scope=docs|none` 조합은 config에 `small` profile이 있을 때 자동 `small` fallback 대상입니다.
- `spark`처럼 별도 capacity slot이 있는 profile이 생겨도 profile 이름만으로 안전하다고 간주하지 않습니다. 비용 profile과 capacity pool은 별도 개념이며, routing 기준은 outcome evidence와 risk factor에 따라 결정합니다.

`routing_experiment` 권장 의미:

- `baseline`: 현재 정책이 선택한 profile입니다. 비교 기준으로 충분한 표본을 모으기 위해 일반 작업의 기본 label로 사용할 수 있습니다.
- `downshift_probe`: 원래 normal 이상으로 처리했을 작업을 한 단계 낮은 profile로 제한적으로 시험합니다. 한 번에 넓히지 않고 category/label/risk factor 조합별로 작게 시작합니다.
- `upshift_guard`: 최근 품질 이슈, 재시도, reviewer needs_human, needs_fix, stale/conflict 위험 때문에 상위 profile을 명시적으로 선택한 작업입니다.
- `manual`: 운영자가 대화 문맥이나 외부 제약 때문에 자동 기준과 다르게 선택한 작업입니다.

Downshift 후보는 아래 조건을 모두 만족할 때만 확대합니다.

- 같은 category/label/risk factor 조합에서 최근 accepted 표본이 충분히 있습니다. 초기 기준은 최소 5건입니다.
- first-pass accepted rate가 높고, 초기 기준은 90% 이상입니다.
- needs-fix/rejected rate가 낮고, 초기 기준은 5% 이하입니다.
- reviewer `needs_human`, `failed_review`, auto-fix 생성, repeated finding, startup/no-progress retry가 최근 표본에서 반복되지 않습니다.
- 변경 범위가 public docs, examples, low-risk tests, local-only operator docs처럼 review surface가 작습니다.

Upshift는 downshift보다 빠르게 적용합니다. 아래 신호 중 하나가 같은 category/label/risk factor 조합에서 반복되면 다음 enqueue 기준을 상위 profile로 올립니다.

- reviewer `needs_fix`, `needs_human`, `failed_review`
- `review_status=rejected` 또는 `needs_followup`
- max attempts에 가까운 반복 retry, startup/no-progress stall 반복
- worktree apply/rebase conflict, stale-base 후 재리뷰에서 의미 있는 수정 필요
- public/private safety 관련 finding
- queue state, lock, dependency, reviewer, worktree apply 같은 control-plane 의미가 뒤늦게 발견됨

증거가 부족한 조합은 비용 최적화 대상이 아니라 baseline 유지 대상입니다. Low-cost 후보 표본이 없거나 1-2건뿐인 경우에는 routing-report 결과가 좋아 보여도 일반 정책을 바꾸지 않습니다. Downshift는 성공 사례가 누적될 때 느리게 넓히고, upshift는 품질 문제가 반복될 때 빠르게 반영합니다.

Policy update는 traceable해야 합니다. 기준을 바꿀 때는 routing-report 근거, 대상 category/label/risk factor, size/risk estimate 또는 verification scope, 변경 전후 requirement/selection rule, 기대 효과, rollback 기준을 public-safe 문서 또는 local operator memo에 기록합니다. 자동화가 이 결정을 수행하는 경우에도 task에는 `routing_reason`, `routing_risk_factors`, `routing_experiment`, `routing_size`, `routing_risk`, `verification_scope`를 남겨 나중에 outcome과 대조할 수 있어야 합니다.

Practical enqueue/model selection loop:

- Enqueue 단계에서 operator 또는 enqueue helper는 prompt를 다시 모델에 보내지 않고 작업 설명, category/label, 예상 변경 범위, 검증 계획만 보고 `routing_size`, `routing_risk`, `verification_scope`를 채웁니다.
- `cbr routing-report --json` 또는 human report에서 `by_routing_decision`은 같은 size/risk/verification 요구가 전반적으로 안정적인지 확인하는 기준이고, `by_model_requirement_routing_decision`은 task requirement 기준 outcome/cost를, `by_model_selection_routing_decision`은 실제 recorded selection rule 기준 outcome/cost를 확인하는 기준입니다. `by_low_cost_candidate`는 conservative low-risk docs/none tuple 후보를 찾는 보조 신호입니다.
- Downshift 후보는 같은 routing decision tuple과 category/label 또는 risk factor가 충분한 accepted 표본을 가진 경우에만 검토합니다. Verification scope가 더 넓어졌거나 risk가 올라간 작업은 기존 low-risk tuple의 성공 사례로 대체하지 않습니다.
- Upshift 후보는 같은 requirement/routing decision tuple에서 reviewer `needs_fix`, `needs_human`, rejected/needs_followup, auto-fix, retry 비용이 반복될 때 검토합니다.
- 실제 model selection 기준 변경은 report 실행과 분리된 operator change로 수행합니다. 대상 tuple, 기존 requirement/selection rule, 새 rule, 근거 report 범위, rollback 기준을 public-safe docs 또는 local operator memo에 남긴 뒤 derivation 기준 또는 config를 수정합니다.


## Capacity, concurrency, and priority config

Capacity config는 여러 worker 또는 여러 Codex profile/provider를 동시에 운영하기 위한 implementation task admission 정책입니다. `run-next`는 한 번 호출될 때 implementation task 하나만 claim하고 실행하지만, queue lock은 selection/claim/start metadata 및 finalize metadata 적용에만 짧게 보유합니다. `run-loop`도 각 iteration에서 같은 claim rule을 사용합니다. Codex 또는 shell subprocess 실행 중에는 queue lock을 보유하지 않으므로, capacity를 2 이상으로 올리고 scheduler가 여러 worker를 겹쳐 실행하면 다른 admissible task를 동시에 claim할 수 있습니다. cbr 자체는 이 버전에서 병렬 in-process dispatcher를 만들지 않습니다.

기본값은 현재 동작과 같은 완전 순차 실행입니다.

```json
{
  "max_total_running": 1,
  "max_running_per_project": 1,
  "capacity_pools": {
    "codex": {
      "max_running": 1
    }
  }
}
```

Field 의미:

- `max_total_running`: 전체 queue에서 동시에 `running` 상태일 수 있는 implementation task 수의 상한입니다. 기본값은 `1`입니다.
- `max_running_per_project`: 같은 `project_id` 또는 같은 normalized `project_root`에 대해 동시에 `running` 상태일 수 있는 implementation task 수의 상한입니다. 기본값은 `1`입니다.
- `capacity_pools`: scarce execution resource별 capacity mapping입니다. 첫 pool 이름은 `codex`로 고정합니다. 각 pool은 `max_running` positive integer를 가집니다.
- Task metadata `capacity_pool`: task가 사용할 pool 이름입니다. 없으면 `codex`로 해석합니다. `cbr enqueue --capacity-pool POOL`로 설정할 수 있습니다.
- Task metadata `task_priority`: 같은 project 안의 task 우선순위입니다. 값은 `asap`, `high`, `normal`, `low`, `background`이며 기본값은 `normal`입니다. `cbr enqueue --priority PRIORITY`로 설정할 수 있습니다.
- `project_priorities`: project id 또는 normalized project root를 integer priority에 매핑합니다. 낮은 숫자가 높은 project priority입니다.
- `default_project_priority`: `project_priorities`에 없는 project의 raw priority입니다. 기본값은 `100`입니다.
- `project_priority_aging_hours`: ready project의 effective priority를 시간에 따라 개선하는 aging 간격입니다. 기본값은 `24`입니다. `0`이면 aging 없이 raw project priority tier가 strict하게 적용됩니다.

Admission rule은 모든 관련 상한을 동시에 만족해야 합니다. 즉 runnable task를 시작하려면 dependency와 cooldown이 ready이고, 전체 running count가 `max_total_running` 미만이고, 해당 project의 running count가 `max_running_per_project` 미만이고, task가 사용할 pool이 config에 존재하고, 그 pool의 running count가 `capacity_pools[pool].max_running` 미만이어야 합니다. 어느 하나라도 초과하면 task JSON을 `blocked_user` 또는 `failed` 같은 상태로 바꾸지 않고, selection 단계에서 일시적으로 건너뜁니다. `list`는 runnable task의 capacity blocker reason을 `NOTE`에 표시하고, `doctor`는 running counts, admissible runnable count, capacity-blocked count/reasons를 read-only evidence로 보고합니다.

Selection order는 deterministic해야 합니다. Runner는 먼저 status/dependency/cooldown/capacity로 candidate를 filter한 뒤 아래 key로 정렬합니다.

1. effective project priority
2. raw project priority
3. task priority rank (`asap` > `high` > `normal` > `low` > `background`)
4. `created_at`
5. task id

Effective project priority는 ready project의 가장 오래된 ready task age를 기준으로 `project_priority_aging_hours`마다 raw priority를 1씩 낮춰 계산합니다. 낮은 숫자가 먼저 실행되므로 aging은 오래 기다린 낮은 priority project의 순서를 점진적으로 당깁니다. 같은 project 안에서는 project priority와 aging 값이 같으므로 task priority가 먼저 적용되고, 같은 task priority이면 FIFO입니다.

Pool 배정 기본값은 다음과 같습니다.

- implementation task는 task metadata에 별도 pool이 없으면 `codex` pool을 사용합니다.
- Reviewer Codex 호출은 별도 pool이 구현되기 전까지 runner invocation 내부의 검토 phase로 취급하며, `auto_review_codex_max_calls_per_run`, reviewer cooldown, bundle size limit이 primary guard입니다. 별도 pool을 쓰는 configuration에서는 이름을 `reviewer-codex`로 둡니다.
- provider quota 또는 model selection rule이 추가되더라도 requirement/selection 이름은 capacity pool 이름과 분리합니다. 여러 selection rule이 같은 `codex` pool을 공유할 수 있고, 별도 scarce slot이 확인된 경우에만 별도 pool로 분리합니다.

동시 실행 예시는 아래와 같습니다.

```json
{
  "max_total_running": 3,
  "max_running_per_project": 1,
  "capacity_pools": {
    "codex": {
      "max_running": 2
    },
    "codex-spark": {
      "max_running": 1
    },
    "reviewer-codex": {
      "max_running": 1
    }
  }
}
```

이 예시는 최대 3개 implementation task를 동시에 허용하되 같은 project는 1개만 실행하고, 기본 Codex pool 2개와 별도 `codex-spark` pool 1개를 허용합니다. 병렬 운영에서 state isolation은 task worktree가 담당합니다. `worktree_mode=task`를 켜면 task별 branch/worktree에서 Codex가 실행되어 서로 다른 project 또는 task의 working tree 변경이 섞이지 않습니다. 완료 처리, review, worktree apply, cleanup, prune, apply-plan 같은 state-changing command는 계속 queue lock 아래에서 atomic mutation으로 유지합니다.


## Queue mutation and replan control plane

Queue mutation은 사람이 task JSON을 직접 편집하지 않고 제한된 metadata/status 변경을 queue lock 아래에서 감사 가능하게 적용하기 위한 surface입니다. 현재 canonical safe mutation command는 `apply-plan`입니다.

```bash
cbr apply-plan queue-plan.json --dry-run
cbr apply-plan queue-plan.json --apply
```

`apply-plan`은 기본적으로 dry-run입니다. `--dry-run`을 생략해도 task JSON을 쓰지 않고 Codex를 실행하지 않으며 post-mutation trigger도 호출하지 않습니다. Dry-run은 plan schema, 지원 operation 이름, 대상 task 존재 여부, running task 대상 금지, operation별 `expected` stale check, dependency cycle 가능성, plan 또는 operation 단위 `reason` 존재 여부를 확인하고 human report 또는 JSON report를 출력합니다. Report는 raw prompt, log path, session/thread id, credential/token 같은 민감한 plan 값을 redaction합니다.

실제 queue 변경은 `--apply`를 명시한 경우에만 수행됩니다. Apply mode는 runner와 같은 queue lock을 잡은 뒤 같은 validation을 다시 실행하고, 검증이 통과한 경우에만 제한된 field를 atomic JSON write로 갱신합니다. 현재 apply 대상 field는 `title`, `description`, `category`, `labels`, `depends_on`, `status`, `model_requirement_vector`, `routing_reason`, `routing_risk_factors`, `routing_experiment`, `routing_size`, `routing_risk`, `verification_scope`입니다. `running` task 대상 mutation과 `status=running` 전환은 거부합니다. `model_requirement_vector`가 제공되면 허용된 dimension/value만 포함하는지 검증하며, `routing_size`/`routing_risk`는 allowlisted enum 값만 허용합니다. 적용된 변경은 sanitized `task_mutated` event로 기록하고, 변경이 있었을 때 configured `post_mutation_trigger_command`를 실행합니다.

지원되는 plan operation은 `pause`, `unpause`, `replan`, `supersede`, `split`, `merge`, `retarget_metadata`, `dependency_changes`, `append_note`, `create_followup`입니다. 현재 apply 가능한 동작은 제한된 field 변경으로 표현할 수 있는 metadata/status/dependency 조정에 한정됩니다. Task 생성이나 다중 재구성이 필요한 operation은 validation/report 대상일 수 있지만, 제한된 apply surface 밖이면 적용 전에 거부됩니다.

안전 규칙:

- Operation과 plan에는 사람이 읽을 수 있는 `reason`이 필요합니다.
- Operation별 `expected` 값이 현재 task JSON과 다르면 dry-run과 apply 모두 실패합니다.
- Dependency cycle, 존재하지 않는 task id, 자기 자신 dependency, running task 대상, `status=running` 전환은 적용 전에 거부합니다.
- 원본 prompt, 실행 history, `last_result`, `last_run`, log path는 보존합니다.
- Plan, task history, event log에는 로컬 runtime state, 실제 raw log/prompt/session id/thread id, credentials, 개인 경로, private queue contents를 넣지 않습니다.
- 여러 task를 바꾸는 plan은 가능한 한 all-or-nothing으로 검증하고, 부분 적용이 불가피하면 event log에 적용 성공/실패 task를 명확히 남깁니다.

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

각 operation은 바꿀 task id, 변경하려는 field, operation별 reason, 기대하는 현재 상태(`expected`)를 포함할 수 있음. `expected`는 stale plan 방지용 optimistic validation으로 사용하며, 지정된 field 값이 현재 task JSON과 정확히 다르면 dry-run과 apply 모두 실패함. 적용 전 validation은 schema, task existence, allowed status transition, dependency graph, public/private safety, task creation limit, atomic write 가능 여부를 검사하고, dry-run report에 `would_change`, `warnings`, `errors`를 구분해 출력함.

Audit 요구사항:

- 모든 mutation은 task의 `history` 배열 또는 별도 append-only queue event log에 기록함.
- 기록에는 mutation id, operation, actor, reason, affected task id, changed fields, before/after summary, validation result, occurred_at을 포함함.
- 나중에 review bundle이나 operator가 “왜 queue가 바뀌었는지”를 재구성할 수 있어야 함.
- event payload는 notification event model과 같은 안전 기준을 따르며, raw prompt/log/session/thread id나 credential을 포함하지 않음.


## Runner execution policy

`run-next`는 1회 실행당 runnable task 하나, auto-review action 하나, 또는 guarded maintenance action 하나만 처리함.

흐름:

1. config 로드
2. global cooldown 확인
3. lock 획득 시도
4. active lock이 있으면 즉시 종료
5. stale lock이면 복구 후 lock 재시도
6. config가 명시적으로 허용한 경우 completed review candidate 하나에 auto-review를 먼저 시도함
7. auto-review가 accept를 적용했거나 reviewer Codex를 호출해 검토 작업을 소비했으면 종료
8. auto-review 후보가 없거나 gate failure처럼 mutation 없이 자동 처리 불가능하면 runnable/needs_resume 후보를 선택함
9. task를 `running`으로 atomic update
10. Codex prompt wrapper 생성
11. 실제 작업이 있을 때만 Codex CLI 호출
12. Codex JSONL stdout을 attempt별 로그 파일에 저장하면서 progress watchdog metadata를 갱신
13. `turn.completed`, `turn.failed`, `error` event와 meaningful progress signal 파싱
14. 최종 JSON 응답 파싱
15. task 상태 갱신
16. lock 해제
17. 다른 eligible task가 있고 global cooldown이 없으면 configured scheduler wake-up hook을 warning-only로 실행

`run-loop`는 launchd/operator single-worker용 반복 command임. 각 iteration은 config를 다시 로드하고 같은 `run-next` path를 1회 호출함. 따라서 pause, cooldown, lock, capacity, dependency, task cooldown, worktree, auto-review gate는 매번 새로 평가되고 기존 단일 worker capacity semantics를 우회하지 않음. Loop 내부에서는 one-shot `run-next`의 post-run wake hook을 suppress함. 같은 process가 다음 iteration에서 follow-up work를 직접 claim하므로 scheduler worker를 중복으로 깨우지 않음. `run-loop --json`은 iteration마다 기존 `run-next` outcome shape의 compact JSON object를 한 줄씩 출력하는 JSONL임. Loop는 `empty`, `paused`, `cooldown`, `locked`, `review_needed`, `stale_finalization`처럼 다음 작업을 계속하면 안 되거나 진행이 없는 outcome에서 멈춤. `--max-iterations`는 runaway 방지용 safety fuse이며 기본값은 100임. 이 값은 correctness mechanism이 아니라 운영 guard임.


## Codex progress watchdog

Runner는 Codex stdout JSONL을 읽는 동안 progress-based watchdog을 실행합니다. 이 정책은 일반적인 wall-clock long-job timeout이 아닙니다. 장시간 실제 작업, 긴 command 실행, 긴 테스트는 JSONL에서 의미 있는 진행 신호가 나온 뒤라면 기본 설정에서 자동 종료 대상이 아닙니다.

Watchdog은 각 attempt에서 다음 metadata를 추적합니다.

- first JSONL event time
- last JSONL event time
- first meaningful event time
- last meaningful event type/time
- stdout이 끝까지 비어 있었는지 여부
- JSONL event가 startup event뿐이었는지 여부
- JSONL/startup/meaningful event count
- watchdog termination reason과 signal

Startup event는 `session.started`, `thread.started`, `turn.started`입니다. Meaningful progress는 assistant/agent message, command/tool execution start/completion, file change, `turn.completed`, `turn.failed`, `error`, final JSON-like result를 포함합니다.

Conservative default config:

- `codex_startup_stall_seconds`: `240`
- `codex_first_meaningful_timeout_seconds`: `420`
- `codex_mid_run_idle_seconds`: `1800`
- `codex_mid_run_idle_kill_enabled`: `false`
- `codex_total_runtime_timeout_seconds`: `null`
- `codex_watchdog_grace_seconds`: `5`
- `codex_startup_stall_cooldown_seconds`: `60`

Startup/no-progress stall이 감지되면 runner는 Codex child process에 `SIGTERM`을 보내고 grace period 안에 종료되지 않을 때만 `SIGKILL`을 보냅니다. 이 class는 기본적으로 permanent failure가 아닙니다. session/thread id가 있으면 task는 `needs_resume`으로 남고, id가 없으면 짧은 cooldown이 있는 `runnable`로 되돌아갑니다. `last_error`는 `codex startup stalled before meaningful JSONL events` 또는 `codex startup stalled before any JSONL output`처럼 stderr-only noise보다 명확한 메시지를 사용합니다.

Runner는 stall task에 `last_progress`, `startup_stalled_at`, `startup_stall_count`를 기록하고, sanitized append-only `task_startup_stalled` event를 남깁니다. Event payload는 raw prompt, raw transcript, session/thread id, credentials, token-like values를 포함하지 않습니다. `cbr summary`는 `last_progress`와 stall marker를 표시하고, `cbr list`는 현재 재시도 대상의 startup stall retry evidence와 완료된 task의 startup stall history를 `NOTE`에서 구분해 표시할 수 있습니다. `cbr doctor`는 최근 startup stall evidence와 오래 running 상태로 남은 no-progress 후보를 operator diagnosis용으로 노출합니다.


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
  "root": "/path/to/codex-batch-runner",
  "codex_command": ["codex", "exec", "--sandbox", "workspace-write", "--json"],
  "codex_resume_command": ["codex", "exec", "--sandbox", "workspace-write", "resume", "{session_id}", "--json"],
  "default_model_requirement_vector": {
    "source": "config_default",
    "confidence": "medium",
    "dimensions": {
      "reasoning_depth": "medium",
      "context_need": "medium",
      "tool_reliability": "medium",
      "latency_priority": "medium",
      "cost_sensitivity": "medium",
      "review_strictness": "medium"
    }
  },
  "default_execution_config": {
    "model": "gpt-5",
    "codex_profile": "batch-normal"
  },
  "model_selection_rules": [
    {
      "name": "low-cost-docs",
      "when": {
        "reasoning_depth": "low",
        "cost_sensitivity": "high"
      },
      "model": "gpt-5-small",
      "codex_profile": "batch-small",
      "config_overrides": {
        "model_reasoning_effort": "low"
      },
      "budget_hint": "small documentation or test-only task"
    },
    {
      "name": "high-capability",
      "when": {"reasoning_depth": "high"},
      "model": "gpt-5",
      "codex_profile": "batch-deep"
    }
  ],
  "post_mutation_trigger_command": []
}
```

`workspace-write`를 기본으로 둠. non-interactive batch 작업은 일반적으로 파일 수정을 해야 하며, read-only sandbox에서는 수정 task가 실패함.
`root`가 있으면 relative `queue_dir`, `log_dir`, `event_dir`, `lock_file`, `state_file`, `worktree_root`, notifier cursor path는 `root` 기준으로 해석함.
`root`가 없으면 compatibility를 위해 process current working directory를 기준으로 해석함.

기본 공개 예시 [examples/config.example.json](../examples/config.example.json)은 이 safe default를 유지함.
완전 비대화형 운영이 필요하고 운영자가 full local access 위험을 수용한 경우에만
[examples/config.automation.example.json](../examples/config.automation.example.json)을 참고할 수 있음.
Automation 예시는 `--dangerously-bypass-approvals-and-sandbox`를 사용해 approval prompt와 sandbox를 모두 비활성화함.
이 설정은 해당 사용자 권한으로 접근 가능한 로컬 파일과 명령에 제한 없는 접근을 허용하므로, trusted queue와 명시적으로 관리되는 scheduler에서만 사용해야 함.

Automation mode는 approval prompt 대기와 sandbox 권한 부족으로 인한 반복 실패를 줄여 pending task와 lock 정체를 완화할 수 있음.
대신 실행 후 review 책임은 더 크며, `summary`, 필요한 경우 `transcript`, 대상 repository의 검증 명령, `doctor`를 이용해 결과와 runner 상태를 확인한 뒤 `accept`를 기록해야 함.

launchd 같은 scheduler는 사용자 shell `PATH`를 그대로 상속하지 않을 수 있음. 운영 config에서는 `codex` 실행 파일을 절대 경로로 지정할 수 있어야 함.

`post_mutation_trigger_command`는 queue mutation 이후, 그리고 `run-next`가 task 하나를 처리한 뒤 eligible follow-up work가 있을 때 외부 scheduler/runner를 즉시 깨우기 위한 optional hook임. `run-loop` iteration은 follow-up work를 같은 process가 계속 처리하므로 이 per-iteration post-run wake hook을 suppress함. 값은 shell string이 아니라 argv string list이며 기본값은 빈 list로 disabled임. 구현은 shell expansion을 하지 않고 짧은 timeout으로 실행함. 실패, non-zero exit, timeout은 stderr warning으로만 표시하고 원래 mutation 또는 처리된 task 결과를 되돌리지 않음.

hook은 durable task JSON/state write와 event emission이 끝난 뒤 실행함. `enqueue`, `accept`, `reject`, `resolve`, `archive`, `cooldown clear`, 성공한 `apply-plan --apply` 같은 queue 또는 runnable-state mutation command에서 호출함. runner pause가 활성인 동안 `enqueue`는 task를 쓰기 전에 거부되므로 hook을 호출하지 않음. `run-next`는 task 하나를 terminal/resumable state로 갱신하거나 completed task 하나를 mechanically accepted로 변경하고 lock을 해제한 뒤, global cooldown이 없고 후속 작업이 즉시 actionable일 때만 hook을 호출함. 구현 task를 처리한 직후의 actionable follow-up은 `select_next_task` 기준 eligible `runnable` 또는 `needs_resume` task이거나 `has_actionable_auto_review_candidate(config)`가 참인 다음 auto-review 후보임. Empty queue, active global cooldown, dependency-blocked-only queue, task cooldown뿐인 queue, 방금 처리한 task가 아직 cooldown 중인 경우, paused work만 남은 경우, mutation 없는 auto-review 시도에는 호출하지 않음. `list`, `show`, `summary`, `review-bundle`, `logs`, `transcript`, `doctor`, `events`, `rate-limits`, `cooldown show`, `cooldown set`, `prune`, `apply-plan` dry-run 같은 read-only, cooldown-setting, 또는 cleanup command에서는 호출하지 않음. 목적은 polling interval로 인한 latency를 줄이는 것이며, polling은 fallback으로 계속 유지함. duplicate wake-up은 안전해야 함. `run-next`가 lock, cooldown, empty queue, dependency, single-task execution 규칙을 계속 강제하기 때문임.

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
  "last_task_id": null,
  "runner_pause": {
    "active": false,
    "reason": null,
    "paused_at": null,
    "paused_by": null
  }
}
```


## Codex CLI maintenance policy

현재 runner는 Codex CLI automatic update discovery를 수행하지 않음. Update는 JSONL schema, resume semantics, permission/sandbox behavior, final response handling을 바꿀 수 있음. 잘못된 update 뒤 자동 실행이 계속되면 usage-limit tokens를 낭비할 수 있고, 설치 방식에 따라 rollback path가 불명확할 수 있음.

권장 운영 정책:

- Queue가 idle일 때만 CLI version check 또는 update를 수행함.
- Idle 기준은 active runner lock 없음, active global cooldown 없음, `runnable`/`needs_resume`/`running` task 없음.
- Manual update 전후에 `cbr doctor --json` 결과를 기록해 configured executable, resolved executable, `codex --version` output을 비교 가능하게 함.
- Manual update 뒤에는 `cbr doctor`와 runner deployment에 맞는 focused tests 또는 smoke command를 실행한 뒤 queued work를 재개함.
- Automatic update discovery는 별도 rollback strategy, compatibility smoke, idle gate, operator approval flow가 설계되기 전까지 추가하지 않음.

`cbr maintenance codex-cli`는 운영자가 config에 명시한 update/smoke command를 guarded maintenance workflow로 실행합니다. 이 명령은 최신 버전을 찾거나 설치 방식을 추론하지 않습니다.

Config:

- `codex_cli_update_command`: Codex CLI를 update하는 argv list command. 예: `["npm", "install", "-g", "@openai/codex"]`.
- `codex_cli_smoke_command`: update 뒤 queue를 재개하기 전에 실행할 argv list smoke command.
- `codex_cli_rollback_command`: optional argv list rollback command. 비어 있으면 rollback을 시도하지 않습니다.
- `codex_cli_maintenance_on_empty`: `true`이면 `cbr run-next`가 실제 work 또는 auto-review accept를 처리한 뒤 queue가 비었을 때 guarded maintenance workflow를 1회 시도합니다. 이미 비어 있는 queue를 polling하는 tick에서는 실행하지 않습니다.

`cbr maintenance codex-cli` 또는 `--dry-run`은 read-only readiness report입니다. `--apply`는 runner lock을 잡아 idle gate를 확인하고, 기존 runner pause가 없을 때만 `runner_pause`를 `Codex CLI maintenance`로 설정합니다. 이후 lock을 해제하고 `doctor-before.json`, update command log, `doctor-after-update.json`, smoke command log, `doctor-after-smoke.json`을 `log_dir/maintenance/codex-cli/run-*` 아래에 저장합니다. Update 또는 smoke가 실패하거나 timeout이면 optional rollback command를 실행하고 `doctor-after-rollback.json`을 저장한 뒤 pause를 유지하고 `status=failed`를 반환합니다. Rollback이 성공해도 queue를 자동 재개하지 않습니다. 둘 다 성공하면 같은 runner lock 아래에서 maintenance pause만 해제하고 post-mutation trigger를 실행할 수 있습니다. Maintenance event payload는 command stdout/stderr 원문을 저장하지 않고 return code, timeout 여부, byte count, log path만 기록합니다.

`codex_cli_maintenance_on_empty=true`인 경우 자동 maintenance는 time interval 기반이 아니라 queue drain event 기반입니다. `run-next`가 아무 work도 처리하지 않은 `empty` poll에서는 실행하지 않으며, runnable/resumable/running task, active global cooldown, runner pause, actionable auto-review candidate가 남아 있으면 deferred됩니다.

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
