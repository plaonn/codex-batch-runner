# Execution Contract

이 문서는 model requirement vector, shell/external-json-command backend, capacity/priority, queue admission, Codex command wrapper, watchdog, lock, atomic write, rate-limit, queue mutation control plane을 정의합니다. 핵심 스펙 index는 [spec.md](spec.md)입니다.

## Model Requirements

Task JSON은 immutable v2 `model_requirement_vector` revision을 canonical storage로 사용합니다. 기존 v1 task와 v1 CLI dimension 입력은 deterministic `legacy-derived` v2 projection으로 읽고 저장하며, 기존 config selection behavior는 그 projection으로 유지합니다. Native v2 vector는 versioned `execution_target_inventory`와 `constraint_registry`가 있으면 D2 exact selector를 사용합니다. 승인된 계약과 migration boundary는 [Model routing requirement contract](model-routing-contract.md)에 정의됩니다.

### 모델 신선도와 실제 모델 식별 한계

일반 task intent는 `model_requirement_vector`만 저장하며 provider/model/profile 식별자를 저장하지 않습니다. 유일한 예외인 `scope=single_task` bounded `routing_override`는 D2 selector에서 preference 또는 fail-closed pin으로 적용됩니다. 두 mode 모두 hard constraint와 quality floor를 우회하지 않습니다. Override와 parent requirement revision은 child/retry/review/fix/follow-up에 자동 상속되지 않습니다.

`execution_target_inventory` schema v1은 snapshot id, `current|stale` status, constraint-registry version, target map을 가집니다. 각 native target은 stable target id, execution surface, trust state, per-axis `static_fitness`, latency/cost score와 capability evidence를 선언합니다. Codex target은 exact model/reasoning pair가 필수입니다. External automatic target은 `external-json-command`만 허용하며 non-empty `model`, matching `command_model`, `reasoning_effort`, 그리고 command template 안의 정확히 한 개씩인 `{model}`/`{reasoning_effort}` placeholder가 필수입니다. Shell은 직접 요청 가능한 non-model backend이지만 automatic model target inventory에는 들어가지 않습니다. D2의 `static_fitness`는 `quality_evidence_status=static_non_learned`인 cold-start fitness일 뿐 학습된 capability나 posterior가 아닙니다. 모든 requirement quality axis를 충족한 target만 utility 비교에 들어갑니다. `quality_evidence_status=insufficient`이면 selector는 `insufficient_quality_evidence`로 중단합니다.

D5 capability posterior도 automatic selector나 active inventory를 변경하지 않습니다. Sanitized
outcome projection과 reviewed posterior policy를 `cbr capability-report`에 제공해 read-only snapshot을
rebuild할 수 있습니다. Safe exploration admission은 `cbr exploration-report`로 별도 검사하며 실제
probe dispatch, queue mutation, provider call은 수행하지 않습니다. 탐색률과 half-life/prior는 CLI나
source의 암묵적 default가 아니라 입력 policy의 명시적 versioned 값이어야 합니다.

Constraint evidence source는 `provider_declared`, `surface_reported`, fresh `operator_verified`, `empirically_observed`, `unknown` 중 하나입니다. `operator_verified` evidence는 timezone이 있는 미래 `expires_at`이 있어야 확정 충족 근거로 인정됩니다. 나머지 unknown/empirical evidence는 versioned registry의 `reject|probe_only|soft_penalty|ignore` policy를 따릅니다.

- 로컬 config는 `execution_targets`에서 안정적인 target alias를 정의하고, `default_execution_config` 또는 `model_selection_rules`가 그 alias를 선택할 수 있습니다.
- `model_selection_rules`와 `default_execution_config`는 호환을 위해 `model`/`codex_profile`를 직접 고정할 수도 있지만, direct model pin은 freshness metadata를 담을 수 없으므로 `cbr doctor`가 경고합니다.
- 명시적 모델 핀(`model: ...`)과 target alias 안의 concrete model은 새 모델 출시 시 자동 갱신되지 않습니다. 새 모델이 추가되면 운영자가 최신 CLI/provider 동향을 보고 target을 검토·갱신해야 합니다.
- target alias의 `freshness.last_reviewed_at` + `freshness.review_after_days`가 오늘 기준 review due date에 도달했으면 `cbr doctor`가 stale warning을 표시합니다.
- `cbr policy-proposals execution-target-freshness`는 freshness state를 read-only proposal JSON으로 표시합니다. `cbr policy-proposals direct-model-pin-migration`은 direct model pin만 별도 `direct_model_pin_migration` report로 표시하며, migration 초안 작성 후보와 별도 operator approval blocker만 노출합니다. `cbr policy-proposals preview PROPOSAL_JSON`은 execution target freshness report를 read-only preview로 렌더링하고, `cbr policy-proposals approval-template PREVIEW_JSON`은 사람이 승인 여부를 채울 approval template를 stdout으로 출력합니다. `cbr policy-proposals validate-approval APPROVAL_JSON --preview PREVIEW_JSON`은 승인 JSON이 source preview와 일치하는지 검사합니다. `cbr policy-proposals apply APPROVAL_JSON --preview PREVIEW_JSON --config-target CONFIG_JSON --dry-run|--apply --approve --json`은 명시적인 local/private config JSON의 `execution_targets.<alias>.freshness` metadata만 guarded apply합니다. Guarded apply는 direct model pin migration, task mutation, model replacement, rule replacement, routing rewrite를 수행하지 않습니다.
- CLI 기본 경로는 설치된 Codex/프로바이더 기본값을 따르지만, cbr는 이를 실행 전에는 실제 모델 정체를 알 수 없습니다.
- cbr가 실제 사용 모델을 명시적으로 알 수 있는 경우는 config에 명시되었거나, 실행 결과에서 `last_run.resolved_execution_config`로 신뢰성 있게 관측될 때에 한정됩니다.
- `routing-report`/`cbr doctor`는 진단/자문용 증거 surface로, 모델 자동 발견(auto-discover), 자동 롤아웃(auto-rollout), 정책 자동 변경(auto-mutate) 기능을 수행하지 않습니다.

Config는 선택적으로 아래 field를 가질 수 있습니다.

- `default_model_requirement_vector`: task에 explicit vector가 없을 때 사용할 기본 요구 벡터
- `review_model_requirement_vector`: legacy config compatibility field. D1부터 reviewer role을 근거로 requirement를 재해석하지 않으므로 active selection에는 사용하지 않으며, reviewer/fix issuer가 자기 work unit의 새 revision을 제출해야 합니다.
- `default_execution_config`: selection rule이 match되지 않을 때 사용할 local Codex 실행 설정
- `execution_targets`: stable local alias와 concrete Codex `model`, `codex_profile`, allowlisted `config_overrides`, optional freshness metadata mapping
- `model_selection_rules`: requirement dimension match 조건과 direct Codex 실행 설정 또는 `execution_target` alias mapping
- `worker_targets`: requirement rule이 task를 Codex CLI가 아닌 다른 execution backend로 보낼 때 사용할 backend, capacity pool, command, timeout, worker metadata alias mapping
- `worker_selection_rules`: requirement dimension match 조건과 `worker_target` alias mapping

Task는 complete v2 `model_requirement_vector`를 `--model-requirement-json`으로 받을 수 있으며 issuer-owned `revision_id`가 필수입니다. 일반 task가 이를 생략한 compatibility path는 deterministic, non-comparable `legacy-derived` projection을 유지합니다. Automatic reviewer와 자동 생성 fix/subtask issuer는 현재 work unit metadata에서 별도 native v2 revision을 발행합니다. 기존 저장 task와 명시적 v1 dimension 입력도 읽기 호환을 위해 `legacy-derived`로 유지합니다. Enqueue 뒤 requirement와 override는 수정할 수 없고 정정은 새 task revision으로 발급합니다.

Automatic reviewer는 parent implementation requirement를 재사용하지 않습니다. 호출 전에 `automatic_reviewer_work_units`에 reviewer 전용 native v2 revision을 append하고, 해당 vector만 unified selector에 전달합니다. 이 기록은 append-only이며 이미 발행된 reviewer work unit은 수정할 수 없습니다. Native v2가 없거나 current inventory에서 exact model+reasoning target을 고를 수 없으면 reviewer subprocess를 시작하지 않습니다. Reviewer role은 provenance/stratification metadata일 뿐 vector 의미나 selector 결과를 바꾸지 않습니다. CLI-default reviewer와 기존 legacy reviewer 기록은 읽을 수 있지만 exact/comparable freeze-exit evidence가 아닙니다.

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

`cbr list`와 `cbr summary`는 explicit `model_requirement_vector`를 표시할 수 있습니다. `cbr summary`와 `review-bundle`은 routing decision metadata도 sanitized task metadata로 표시해 review outcome과 original routing decision을 대조할 수 있게 합니다. Runner는 각 Codex 실행의 `last_run.resolved_execution_config`에 worker role, selection rule, `model_source`, `execution_target`, model/profile 존재 여부, override key 이름, 사용한 requirement vector를 기록합니다. `cbr doctor`는 configured model selection rule 이름, target alias, override key 이름만 표시하고 override 값은 출력하지 않습니다.

Policy proposal report JSON shape is fixed at `schema_version: 1` for this spike. Top-level fields are `schema_version`, `kind`, `proposal_class`, `mode`, `generated_at`, `mutation`, `summary`, `items`, `proposals`, `decision_cards`, `warnings`, and `errors`. `items` records each configured target alias and its freshness status. `proposals` is only populated for stale or missing freshness metadata, and each proposal declares `allowed_state_changes: ["none"]` plus prohibited mutation classes. `decision_cards` keeps execution reporting separate from user decision state: normal freshness proposals use `user_decision_status=decision_required`, while direct model pin migration proposals use `approval_blocked` because they need a separate bounded migration approval before any model or rule change.

Policy proposal preview JSON shape is also fixed at `schema_version: 1`. Top-level fields are `schema_version`, `kind: policy_proposal_preview`, `source_schema_version`, `source_kind`, `proposal_class`, `mode`, `mutation`, `summary`, `items`, `decision_cards`, `warnings`, and `errors`. Preview items render the proposed target path and recommended action, but every item remains `would_change: none`, `apply_ready: false`, and `blocked_reason: preview_only_no_apply_target`. Preview `decision_cards` preserve the same user decision axis and add `preview_apply_ready=false` plus `preview_blocked_reason=preview_only_no_apply_target`.

Policy proposal approval template JSON shape is fixed at `schema_version: 1` and `kind: policy_proposal_approval_template`. Top-level fields are `schema_version`, `kind`, `source_schema_version`, `source_kind`, `source_preview_sha256`, `proposal_class`, `mode`, `created_at`, `mutation`, `summary`, `approvals`, `decision_cards`, `warnings`, and `errors`. Each approval records `proposal_id`, target details, `source_item_sha256`, and editable approval fields initialized to `approved: false`, `reviewer: null`, `reviewed_at: null`, and `decision_note: null`. Template `decision_cards` mark the user decision state as `decision_pending`. The template command prints only; it does not persist or apply approvals.

Policy proposal approval validation JSON shape is fixed at `schema_version: 1` and `kind: policy_proposal_approval_validation`. The validator compares approval and preview schema metadata, `source_preview_sha256`, each approved proposal's `source_item_sha256`, and required approval metadata. Approved items require non-empty `reviewer`, ISO datetime `reviewed_at`, and non-empty `decision_note`. Validation `decision_cards` classify each approval as `approved`, `not_approved`, or `invalid`; the validation command is still read-only evidence, and apply requires the separate guarded apply command.

Policy proposal apply JSON shape is fixed at `schema_version: 1` and `kind: policy_proposal_apply`. Top-level fields include `proposal_class`, `mode`, `valid`, `mutation`, `summary`, `config_target`, `source_preview_sha256`, nested `validation`, `items`, `audit`, `warnings`, and `errors`. `--dry-run` reports eligibility and before/after freshness snapshots without writing. In the apply report, `mutation.approve_flag` records whether the CLI `--approve` guard flag was present; approval file decisions remain item-level `approved` fields and summary counts. Human output also shows `source_preview_sha256`, config target `sha256_before`/`sha256_after`, per-item approval reviewer metadata, and nested validation errors so an operator can compare the visible apply summary with the sanitized audit payload without rerunning `validate-approval`. `--apply --approve` writes only `execution_targets.<alias>.freshness`, preserving model, profile, routing rules, task metadata, and other config fields. Repo public paths and `.codex-batch-runner` runtime state paths are rejected as config targets. Missing freshness metadata receives `owner` from the approval reviewer, `last_reviewed_at` from the approval `reviewed_at` date, and a default `review_after_days` of 14 when the target lacks one. Apply emits a sanitized event with hashes and compact diff metadata only.

`cbr routing-report`는 model requirement와 selection rule을 조정하기 위한 read-only evidence surface입니다. 명령은 queue task를 model requirement, model selection rule, category, label, requirement/category 조합, routing experiment, routing experiment lane family, routing size, routing risk, routing risk factor, verification scope, routing decision tuple, requirement/routing decision tuple, selection/routing decision tuple, low-cost candidate 신호, requirement/experiment 조합, provider resource evidence로 집계하고 accepted count, first-pass accepted count, needs-fix/rejected rate, reviewer decision count, auto-fix task frequency, attempts, run count, duration 기반 cost proxy를 출력합니다. `evaluation_diagnostics.task_buckets`는 같은 기존 evidence에서 threshold-only advisory를 추가로 계산합니다. 고정 기준은 `min_accepted_count=5`, `min_first_pass_accept_rate=0.90`, `max_needs_fix_or_rejected_rate=0.05`이고, reviewer/human-review adverse signal이 있으면 `reviewable`이 되지 않습니다. Advisory status는 `insufficient_sample`, `below_threshold`, `reviewable`이며 read-only입니다. `evaluation_diagnostics.probe_lanes`는 `routing_experiment`를 `baseline`, `probe`, `guard`, `manual`, `unspecified`, `other` lane family로 분류해 baseline/probe/guard outcome을 기존 task bucket과 requirement 축에서 비교할 수 있게 합니다. 이 diagnostic은 policy-candidate `task_buckets` 의미를 바꾸지 않습니다. Provider resource evidence는 현재 Codex provider 불확실성을 명시하기 위해 기본적으로 `provider_id=codex`, `quota_boundary=unknown`, `sharing_assumption=not_independent`를 사용합니다. 이 evidence는 local `capacity_pool`, worker/reviewer role, legacy profile name과 분리되며 provider quota bucket을 추론하지 않습니다. Report는 task JSON, event log, review status를 변경하지 않고 Codex 또는 reviewer Codex를 호출하지 않습니다. 운영자는 이 결과를 보고 requirement derivation 또는 selection rule 변경을 별도 policy change로 반영합니다.

`cbr execution-report`는 enqueue intent가 아니라 실제 처리된 task run을 기준으로 worker/model/cost evidence를 보는 read-only surface입니다. `model` section은 `identity_kind=planned_execution`인 selection/config provenance이며 actual model이 아닙니다. Evaluation evidence v2를 가진 row는 별도 `actual_model`, `token_usage`, `evidence.cohort`를 표시합니다. Codex actual model은 allowlisted completion JSONL event가 model을 명시한 경우에만 `provider_observed`로 기록하고, `cli_default`, target alias, config model, profile에서 actual identity를 추론하지 않습니다. Token usage도 run 종료 시 provider JSONL에서 추출한 값을 v2 record에 보존합니다. Shell backend는 `token_free`, 외부 worker가 attestation을 내지 않으면 model/usage는 `unavailable`입니다. Legacy task는 기존 dynamic log lookup을 유지하지만 `legacy-v1` cohort로 분류되어 v2와 comparable하지 않습니다.

같은 command의 `summary.model_measurements`는 queue에 저장된 exact `execution-evidence-v3` run만 target, selected model, reasoning effort, selection cohort별로 descriptively 집계합니다. 집계는 integrity와 provider attestation, distinct execution cohort/version-set 수, review outcome evidence와 comparable sample 수, token component totals, completed/censored latency, timeout/failure evidence를 분리합니다. v2/legacy/non-exact run은 exact 집계에서 제외하고 `non_exact_run_count`로만 표시합니다. 여러 모델이 있더라도 posterior나 우열을 계산하지 않으며 `cross_model_quality_status`가 `no_exact_v3_evidence`, `insufficient_models`, `insufficient_comparable_quality`, `descriptive_only` 중 하나로 추론 경계를 명시합니다. 이 command는 raw prompt, transcript/log body, full command argv, session/thread id, raw log path를 출력하지 않고 task JSON/event log/review status나 routing policy를 변경하지 않습니다.

### Evaluation evidence v2

Runner는 실행 attempt마다 sanitized record를 task의 `execution_evidence_history`에 append하고 `last_run.execution_evidence_id`로 현재 run과 연결합니다. Contract identifier는 `execution-evidence-v2`, cohort definition은 `execution-cohort-v2`입니다. Record는 capture source, backend/worker family, actual model observation, token usage observation, monetary cost observation, cohort components/comparability, privacy flags를 분리합니다.

- `actual_model`은 `observed` 또는 `unavailable`입니다. Planned model, model group, capacity pool, profile, selection alias는 actual value의 fallback이 아닙니다.
- `token_usage`는 `observed`, `token_free`, `unavailable` 중 하나입니다. Values는 input/cached/output/reasoning/uncached/known-total token을 구조화합니다.
- `monetary_cost`는 provider billing evidence가 없으면 `unavailable`입니다. Model alias와 가격표를 조합한 금액 추정은 v2 contract가 수행하지 않습니다.
- Cohort는 evidence contract, backend, observed actual model, selection/worker rule, execution target, routing experiment, review policy, requirement derivation version으로 구분합니다. `model_quality`, `token_cost`, `monetary_cost` comparability를 각각 표시합니다. Actual model이 없으면 model-quality cohort에 넣지 않습니다.
- 기존 task와 v1 supplemental evidence는 dual-read되지만 `legacy-v1-non-comparable` cohort로 분류합니다. 과거 planned model/group을 actual model로 backfill하지 않습니다.

External JSON command의 final response는 optional `execution_evidence` capability를 포함할 수 있습니다. 이 capability는 baseline 필수가 아니며 아래 allowlisted shape만 허용합니다.

```json
{
  "execution_evidence": {
    "schema_version": 2,
    "capability": "actual-model+usage-attestation",
    "actual_model": "provider-model-name",
    "token_usage": {
      "input_tokens": 100,
      "cached_input_tokens": 25,
      "output_tokens": 10,
      "reasoning_output_tokens": 2
    }
  }
}
```

At least one of `actual_model` or `token_usage` must be present. Values are recorded with `confidence=wrapper_attested`; cbr does not upgrade them to provider-observed evidence. Invalid attestation makes the external final response invalid. A worker that omits the capability continues to run normally with explicit unavailable evidence.

`cbr routing-policy-candidates`는 같은 routing-report/evaluation diagnostics의 task bucket threshold advisory를 read-only candidate report로 재구성합니다. 기본적으로 `reviewable` bucket만 candidate로 표시하고, candidate마다 stable `candidate_id`, task bucket key, evidence counts/rates, advisory reasons, thresholds, `recommended_next_step=operator_review`를 제공합니다. `decision_cards`는 실행 후보 보고 상태와 사용자 결정 상태를 분리해 reviewable bucket은 `decision_required`, non-reviewable bucket은 `not_ready`로 표시합니다. `--include-non-reviewable`을 사용하면 insufficient/below-threshold bucket도 별도 section과 observation decision card에 표시하며 blocked/rejection reason을 포함하고, human output은 recommendation/blocker summary groups를 표 위에 표시합니다. 이 report는 operator review 대상 목록을 드러내는 표면일 뿐 candidate approval, routing/model/provider config write, policy apply, task/event/runtime mutation을 수행하지 않습니다.

`cbr decision-cards`는 current config의 execution target freshness decision cards와 routing policy candidate decision cards를 하나의 read-only inventory로 모읍니다. 기본 scope는 사용자 결정이 필요한 `decision_required`와 별도 승인 제약이 있는 `approval_blocked` 카드이며, `--include-observations`를 사용하면 `not_ready` 관찰 카드도 포함합니다. `--decision-axis`와 `--user-decision-status`는 repeatable allowlist filter로 특정 결정 축 또는 사용자 결정 상태만 볼 때 사용합니다. Inventory JSON은 snapshot `generated_at`, 각 source report의 generated timestamp/read-only metadata, recommendation/blocker summary groups, `summary.next_action`을 포함합니다. `summary.next_action`은 카드가 없거나 모든 카드가 terminal `approved`/`not_approved` 상태이면 `none`, `invalid` 카드가 있으면 `fix_invalid_decision_cards`, actionable `decision_required`/`approval_blocked`/`decision_pending` 카드가 있으면 `review_decision_cards`, 그 외 `not_ready` 관찰 카드가 있으면 `continue_observing`입니다. Human `open_decisions`는 `summary.next_action`이 `fix_invalid_decision_cards` 또는 `review_decision_cards`일 때만 `present`입니다. Human table은 각 card의 `blocked_reason`을 `BLOCKED` column에 표시하고 값이 없으면 `-`로 표시합니다. 이 inventory는 하위 read-only report를 다시 계산해 카드만 정규화하며 approval file 생성, guarded apply, routing/model/provider config 변경, task/event/runtime mutation을 수행하지 않습니다.

`routing-report`와 `routing-eval-report`는 `--execution-evidence-json PATH`를 반복 지정해 queue 밖에서 나온 sanitized 실행 근거를 supplemental evaluation row로 함께 볼 수 있습니다. 지원 record kind는 legacy `codex_subagent_execution`, v2 `execution_evidence_v2`, exact-target v3 `execution_evidence_v3`, review-only `review_outcome_evidence_v1`입니다. 마지막 kind는 execution evidence와 task review metadata를 재작성하지 않고 append-only review outcome record만 projection에 붙입니다. 이 입력은 queue task로 변환되지 않고 `task_rows`, task count, queue group 집계에도 섞이지 않습니다. JSON output은 `execution_evidence_rows`와 `execution_evidence_count`에 별도로 표시하며, row에는 execution surface, `subject.queue_task=false`, hashed work id만 포함합니다. v2 example은 `examples/execution-evidence-v2.example.json`, review example은 `examples/review-outcome-evidence-v1.example.json`입니다. Diagnostics는 evidence contract와 cohort별 rows 및 model/token/money comparability를 분리하며 review quality는 [review outcome contract](review.md#review-outcome-evidence-and-evaluation-boundaries)의 exact stratum에서만 표시합니다. Legacy, v2, v3 cohort는 합산 가능한 actual-model cohort로 취급하지 않습니다. Supplemental record는 `routing_experiment` 같은 structured routing metadata를 포함할 수 있지만 raw prompt, transcript/log body, stdout/stderr dump, session id, thread id, credential, private absolute path를 장기 저장하거나 report에 노출하는 용도로 쓰면 안 됩니다.

### Supplemental Codex subagent execution evidence

Codex parent thread나 operator script가 subagent 실행 결과를 평가 report에 합치려면 `examples/subagent-execution-evidence.example.json` 형식의 public-safe summary artifact를 만듭니다. `work_id`는 parent가 만든 synthetic correlation id여야 하며 Codex thread id, session id, app URL, transcript path, log path, private prompt hash에서 파생하면 안 됩니다. 실제 subagent/thread와의 매핑이 필요하면 `.private/` 또는 runtime state에만 보관합니다.

Producer는 allowlisted structured field만 넣습니다. 권장 필드는 `record_kind`, `work_id`, `execution_surface`, `execution_backend`, task characterization metadata, terminal `status`, `review_status`, `attempts`, `run_count`, sanitized `last_run.resolved_execution_config`, sanitized `last_result.changed_files`, sanitized `last_result.verification`, optional `reviewer_codex` decision metadata입니다. `changed_files`는 repo-relative public-safe path만 넣고 private/local file은 omit합니다. 실제 Codex app model identity를 신뢰할 수 없으면 `model_source=codex_app_default` 또는 `unknown`을 사용하고 actual model name은 쓰지 않습니다.

`routing-report`는 의사결정 근거가 아니라 운영 진단입니다. task 선택, dependency readiness, review acceptance, cleanup/apply/archiving, cooldown, reject/resolve, run/claim 정책을 바꾸지 않습니다. 보고가 보여주지 않는 항목은 다음과 같습니다.
- 개별 task의 raw prompt, full transcript/JSONL, raw log body
- 다음 실행에서 바꿔 적용할 patch나 실행 계획
- 안전성 판단 근거의 원문 근거 데이터(요약되지 않은 증거)
- local `capacity_pool`에서 추론한 provider quota/resource identity
- 정책 변경 자동 실행

그래서 `routing-report`는 운영자 판단의 입력값으로만 쓰고, policy 변경은 별도 제어면에서 수동으로 수행해야 합니다. 즉 advisory + read-only입니다.


## Usage-aware Codex admission

Usage-aware admission은 native `execution_backend=codex` implementation task를 claim하기
직전에 한 번만 실행되는 opt-in gate입니다. 기본값은 disabled이므로 기존 pause, global
cooldown, queue lock, capacity, dependency, review/apply, one-task-per-invocation 동작을
바꾸지 않습니다. Shell 및 external JSON backend에는 적용하지 않습니다.

활성화하면 runner는 configured argv command를 shell 평가 없이 bounded timeout으로 한 번
실행하고 stdout의 JSON object만 읽습니다. cbr는 특정 provider, account, credential,
private log, personal path에 의존하지 않으며 command를 설치하거나 인증하거나 별도 Codex
probe를 실행하지 않습니다. Snapshot command가 cached state를 반환한다면 그 호출 자체가
새 provider observation을 만든다고 간주하지 않습니다.

지원하는 snapshot field는 다음과 같습니다.

- top-level `available`, `observed_at` 또는 `generated_at`
- provider boundary의 `primary` / `secondary` 각 window에 `window_minutes`, `remaining_percent`
- short window의 `resets_at` 또는 `resets_at_iso`; top-level `resets_at` 또는
  `resets_at_iso`도 fallback으로 허용
- long window가 0%이면 해당 window의 `resets_at` 또는 `resets_at_iso`

Runner는 `window_minutes`가 짧은 값을 short window, 긴 값을 long window로 해석합니다.
Fresh snapshot에서 short window remaining이 configured threshold 이하이면 short reset +
`usage_admission_reset_grace_seconds`까지 global cooldown을 설정합니다. Long window의 낮은
잔여량은 planning pressure이며 hard gate가 아닙니다. 단, long window가 실제 0%이면 long reset
까지 gate합니다. 둘 다 낮지만 long window가 0%는 아니면 short reset 뒤 재판정합니다.
Triggering window의 reset이 없거나 invalid이면 다른 window reset으로 대체하지 않고 fail open합니다.
Cooldown state를 먼저 저장한 뒤 기존 manual cooldown one-shot wake
adapter를 호출하므로 wake scheduling failure는 warning-only이고 polling이 fallback입니다.

낮은 remaining을 담은 snapshot의 reset이 이미 지났고 observation도 그 reset 이전이면,
runner는 old low value를 근거로 계속 연기하지 않고 정상적인 bounded task attempt 한 건을
허용합니다. Capacity가 2 이상이어도 이 stale-after-reset 확인 attempt가 끝나기 전에는 같은
snapshot reset에 대한 두 번째 Codex task를 동시에 시작하지 않습니다. 이 attempt에 provider가 실제 rate limit을 반환하면 기존 Codex rate-limit
detection이 authoritative하며 새 global cooldown과 evidence를 기록합니다. 성공하거나
non-rate-limit 결과이면 stale snapshot만으로 추가 cooldown을 만들지 않습니다.

Command failure, nonzero exit, timeout, invalid/non-object JSON, `available=false`, missing or
invalid required field, future/inconsistent timestamp, reset 전 max-age 초과 snapshot은 sanitized
warning/event를 남기고 fail open합니다. Raw stdout, stderr, command, credential-like data는
warning evidence에 복사하지 않습니다.

## Shell execution backend

`execution_backend=shell` task는 Codex를 호출하지 않고 local argv list command를 실행합니다. 기본값은 backward-compatible `codex`입니다. Shell backend는 simple verification, maintenance, dependency gate용이며 token-free queue task로 동작합니다.

Enqueue CLI는 `--backend shell`과 함께 `--command-json '["cmd", "arg"]'` 또는 마지막 option인 `--command cmd arg`를 받습니다. cbr는 문자열을 암묵적으로 shell 평가하지 않습니다. Pipe, redirect, `&&` 같은 shell syntax가 필요하면 command argv에 `bash -lc` 또는 동등한 explicit shell invocation을 넣어야 합니다.

Runner는 shell task에도 기존 queue ordering, dependency readiness, cooldown skip, runner lock, stale running recovery, worktree cwd adapter, attempts/run count, log path, status transition event, post-run wake trigger를 적용합니다. Exit code `0`은 `completed`와 `review_status=unreviewed`를 기록하고, nonzero exit, executable failure, timeout은 `failed`를 기록합니다. Downstream task는 기존 dependency rule 때문에 failed shell dependency를 unmet dependency로 봅니다.

Shell attempt log는 stdout/stderr 전체를 task log file에 저장합니다. Task JSON의 `last_run`은 `execution_backend=shell`, `command_kind=shell`, argv command, returncode, started/finished time, duration, timeout flag/seconds, stdout/stderr byte count, log path만 저장합니다. `last_result`는 Codex final JSON과 같은 review/list surface에서 읽을 수 있도록 `task_id`, terminal `status`, compact `summary`, empty `changed_files`, verification summary를 저장합니다. Event payload에는 raw stdout/stderr를 넣지 않고 sanitized summary/count/path metadata만 남깁니다.

`shell_task_timeout_seconds` config 기본값은 `900`입니다. `--shell-timeout` 또는 task `shell_timeout_seconds`가 있으면 해당 task에만 override합니다.

Codex CLI update 같은 guarded maintenance workflow는 runner-level maintenance로 처리합니다. Shell task는 프로젝트별 ordered dependency gate로 사용할 수 있지만, runner pause를 잡고 queue idle gate를 확인하는 solo maintenance mode 자체는 shell backend가 아니라 별도 maintenance command가 담당합니다.


## Cost-aware routing evidence

`routing-cost-evidence-v1` remains the read-only compatibility contract for execution
evidence v2 and legacy routing readers. Exact execution evidence v3 produces the new
`routing-cost-evidence-v2` contract. Its cohort includes selection cohort
(`automatic` or `override`), target id, selected and command model, exact reasoning,
and every execution contract version. Provider omission retains command attribution;
provider mismatch remains adverse and makes quality/cost comparison ineligible.
Automatic and override evidence, evidence v2, CLI-default, and legacy records are not
merged by reports or recommendations.

`routing-cost-evidence-v1` is an append-only supplemental contract over execution
evidence and review outcome evidence. It does not replace either source contract.
It keeps these comparison axes explicit:

- planned model and reasoning from resolved execution config;
- provider/wrapper-observed actual model;
- execution surface and backend, task bucket, prompt contract version, and context
  contract version;
- uncached input, cached input, cache-write, output, and reasoning-output tokens;
- objective verification, semantic review, human acceptance, rejection, follow-up,
  and rework counts.

Usage attribution is one of `provider_attributed`, `window_estimated`,
`concurrent_confounded`, or `unavailable`. An isolated before/after usage-window
estimate remains a different cohort from provider-attributed usage. A window
with concurrent work is `concurrent_confounded` and is excluded from cost
comparison; it must not be relabeled as an estimate merely because a numeric
delta is available. `unavailable` evidence contains no usage values.

Quality and cost can be compared only when the cohort has an observed actual
model, versioned task/prompt/context axes, comparable review outcome evidence,
and non-confounded usage attribution. The attribution class and review outcome
cohort id are cohort components, so unlike observations are never pooled.
Legacy tasks without this supplemental history are projected as
`legacy-routing-cost-unknown` and excluded from quality and cost denominators.

The public-safe projection contains no prompt/context text, transcript, raw
provider output, session/thread id, local path, or personal usage-limit message.
Those values are forbidden in the contract rather than merely hidden by the
human-readable renderer. See
[`examples/routing-cost-evidence-v1.example.json`](../examples/routing-cost-evidence-v1.example.json)
for a sanitized shape excerpt. Persisted records also include evidence identity,
timestamps, source-contract references, and the derived cohort.

This evidence is read-only/advisory. It does not mutate the online routing
policy, runner config, or queue.

## External JSON command backend

`execution_backend=external-json-command` task는 Codex를 호출하지 않고 generic local argv list command를 실행하되, command stdout에서 cbr-compatible final JSON object를 읽어 task 결과로 사용합니다. 이 backend는 vendor-neutral adapter boundary입니다. cbr는 provider-native resume id, model/quota identity, auth, GUI automation, model discovery, quota probing을 추론하거나 구현하지 않습니다. v1 commit boundary는 cbr-owned입니다. External workers modify files and report results; they do not commit or push.

Enqueue CLI는 `--backend external-json-command`와 함께 `--command-json '["path/to/wrapper", "--flag"]'` 또는 마지막 option인 `--command path/to/wrapper --flag`를 받습니다. Raw shell string은 받지 않습니다. Runner는 기존 cbr task prompt wrapper를 만들고 그 prompt를 final argv argument로 append합니다. External wrapper는 마지막 argv argument를 작업 지시문으로 읽어야 합니다.

Runner는 external-json-command task에도 기존 queue ordering, dependency readiness, cooldown skip, runner lock, stale running recovery, worktree cwd adapter, attempts/run count, log path, status transition event, post-run wake trigger를 적용합니다. `worktree_mode=task`이면 command cwd는 prepared task worktree입니다. Timeout은 Codex JSONL progress watchdog이 아니라 wall-clock subprocess timeout입니다.

`worktree_mode=task`에서 external worker는 task worktree 파일을 수정하고 final JSON `changed_files`에 안전한 상대 경로를 보고해야 합니다. External worker는 직접 commit하거나 push하지 않습니다. Valid `completed` final JSON이면 cbr가 보고된 safe `changed_files`만 stage하여 task branch에 local auto-commit을 만들고, 그 commit을 review/apply unit으로 기록합니다. cbr-created commit은 `last_result.commits`에 추가되고 `last_result.push_status.status=not_pushed`로 저장됩니다. If an external-json-command worker creates local commits before cbr auto-commit, cbr v1 rejects the completed result with a sanitized `last_error` and retains the task worktree/branch for recovery or review. The runner execution path does not fetch, push, delete the worker commit, or rewrite history for this guard.

External command stdout은 하나의 JSON object여야 합니다. Required final JSON shape는 Codex final response와 같습니다: `task_id`, `status`, `summary`, `changed_files`, `verification`, optional `next_prompt`, optional `commits`, optional `push_status`. 허용 status는 `completed`, `needs_resume`, `blocked_user`, `failed`입니다. `completed`는 `review_status=unreviewed`를 기록합니다. `needs_resume`은 `next_prompt`를 저장하고 다음 실행에서 provider-native conversation id 없이 resume-unavailable continuation prompt를 사용합니다.

Invalid JSON, task id mismatch, missing required key, invalid status, executable failure, timeout, and nonzero exit without valid `status=failed` or `status=blocked_user` final JSON은 `failed`와 sanitized `last_error`로 기록합니다. Nonzero exit가 valid final JSON을 출력했더라도 `completed` 또는 `needs_resume`은 성공으로 인정하지 않습니다. Nonzero exit with valid `failed` 또는 `blocked_user` final JSON은 external worker가 보고한 terminal result로 기록할 수 있습니다.

External attempt log는 command, cwd, started/finished time, duration, timeout, return code, timeout flag, sanitized error metadata, stdout, stderr를 task log file에 저장합니다. Task JSON의 `last_run`은 execution backend, command kind, configured argv command without the appended prompt, returncode, timing, timeout flag/seconds, stdout/stderr byte count, log path만 저장합니다. Event payload에는 raw stdout/stderr 또는 prompt text를 넣지 않고 sanitized summary/count/path metadata만 남깁니다.

`external_json_command_timeout_seconds` config 기본값은 `900`입니다. `--external-timeout` 또는 task `external_timeout_seconds`가 있으면 해당 task에만 override합니다.

## Worker target routing

`model_selection_rules`는 Codex CLI model/profile/config option만 선택합니다. External worker, shell worker, Antigravity wrapper처럼 execution backend와 pool 자체를 바꾸려면 `worker_targets`와 `worker_selection_rules`를 사용합니다.

Example:

```json
{
  "capacity_pools": {
    "codex": {"max_running": 1},
    "external-review": {"max_running": 1}
  },
  "worker_targets": {
    "external_strict_review": {
      "execution_backend": "external-json-command",
      "capacity_pool": "external-review",
      "external_command": ["path/to/cbr-json-wrapper", "--model-group", "claude-gpt"],
      "external_timeout_seconds": 900,
      "worker_family": "external",
      "model_group": "claude-gpt",
      "budget_hint": "strict-review"
    }
  },
  "worker_selection_rules": [
    {
      "name": "strict-review-external",
      "when": {"review_strictness": "high"},
      "worker_target": "external_strict_review"
    }
  ]
}
```

Only implicit default `codex` tasks are eligible for worker target routing. An explicit `--backend` always wins over worker-selection policy, including `--backend codex`. Tasks already enqueued with `execution_backend=shell` or `execution_backend=external-json-command`, tasks with explicit `shell_command` or `external_command`, and `needs_resume` tasks keep their stored execution backend. On claim, cbr validates the selected target's backend command contract, then applies the target to the task JSON by setting `execution_backend`, `capacity_pool`, command, timeout, and sanitized worker metadata before running the worker.

Queue admission uses the planned worker target capacity pool for matching runnable tasks. This means a strict-review task routed to `external-review` is not blocked merely because the default `codex` pool is full, while running capacity still counts the pool stored on already claimed tasks.


## Model requirement routing optimization policy

Model requirement routing 최적화는 비용을 줄이기 위한 운영 루프이지만, task prompt와 verification 요구를 낮추는 방식으로 사용하지 않습니다. Runner는 routing-report 결과를 근거로 자동 policy mutation을 수행하지 않습니다. 운영자 또는 별도 control-plane 작업이 명시적으로 repo-local 기준, enqueue skill 기준, 또는 config를 수정할 때만 routing 기준이 바뀝니다.

기본 운영 원칙:

- General implementation fallback은 config의 `default_execution_config`와 `model_selection_rules`가 결정합니다.
- 손상 비용이 큰 작업은 높은 `model_requirement_vector` 또는 보수적인 selection rule을 유지합니다. runner state, lock, queue mutation, reviewer safety, worktree apply/recovery, stale-base/rebase, dependency semantics, 자동 review/fix loop처럼 control-plane 의미가 있는 작업은 성공 사례가 누적되어도 낮은 requirement 후보로 자동 전환하지 않습니다.
- 낮은 비용의 execution config 또는 selection rule은 bounded, low-blast-radius 작업에서만 사용합니다. 예시는 공개 문서의 작은 수정, 예제/README 보강, 단순 textual cleanup처럼 실패해도 리뷰 단계에서 쉽게 감지되고 main apply 전 되돌릴 수 있는 작업입니다. `routing_size=tiny|small`, `routing_risk=low`, `verification_scope=docs|none` 조합은 low-cost candidate가 될 수 있습니다.
- `small`, `normal`, `deep`, `spark` 같은 이름이 local config나 historical output에 남아 있더라도 durable policy primitive가 아닙니다. 비용 설정, model requirement, concrete execution config, provider resource evidence, capacity pool은 별도 개념이며, routing 기준은 outcome evidence와 risk factor에 따라 결정합니다.

`routing_experiment` 권장 의미:

- `baseline`: 현재 정책이 선택한 model requirement와 execution config입니다. 비교 기준으로 충분한 표본을 모으기 위해 일반 작업의 기본 label로 사용할 수 있습니다.
- `downshift_probe`: 원래 더 높은 requirement 또는 더 보수적인 execution config로 처리했을 작업을 낮은 비용 후보로 제한적으로 시험합니다. 한 번에 넓히지 않고 category/label/risk factor 조합별로 작게 시작합니다.
- `upshift_guard`: 최근 품질 이슈, 재시도, reviewer needs_human, needs_fix, stale/conflict 위험 때문에 더 보수적인 requirement 또는 execution config를 명시적으로 선택한 작업입니다.
- `manual`: 운영자가 대화 문맥이나 외부 제약 때문에 자동 기준과 다르게 선택한 작업입니다.

`routing-report`는 이 값을 lane family로도 요약합니다. `*_probe` 또는 `probe`가 포함된 값은 `probe`, `*_guard` 또는 `guard`가 포함된 값은 `guard`, `baseline`과 `manual`은 각각 별도 family로 표시합니다. 이 lane 분류는 read-only comparison aid이며, probe success나 guard failure를 routing/model policy 변경으로 자동 승격하지 않습니다.

Downshift 후보는 아래 조건을 모두 만족할 때만 확대합니다.

- 같은 category/label/risk factor 조합에서 최근 accepted 표본이 충분히 있습니다. 초기 기준은 최소 5건입니다.
- first-pass accepted rate가 높고, 초기 기준은 90% 이상입니다.
- needs-fix/rejected rate가 낮고, 초기 기준은 5% 이하입니다.
- reviewer `needs_human`, `failed_review`, auto-fix 생성, repeated finding, startup/no-progress retry가 최근 표본에서 반복되지 않습니다.
- 변경 범위가 public docs, examples, low-risk tests, local-only operator docs처럼 review surface가 작습니다.

Upshift는 downshift보다 빠르게 적용합니다. 아래 신호 중 하나가 같은 category/label/risk factor 조합에서 반복되면 다음 enqueue 기준을 더 보수적인 `model_requirement_vector`로 올리거나, 더 보수적인 execution config가 선택되도록 selection 기준을 조정합니다.

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
- `cbr routing-report --json` 또는 human report에서 `by_routing_decision`은 같은 size/risk/verification 요구가 전반적으로 안정적인지 확인하는 기준이고, `by_model_requirement_routing_decision`은 task requirement 기준 outcome/cost를, `by_model_selection_routing_decision`은 실제 recorded selection rule 기준 outcome/cost를 확인하는 기준입니다. `by_low_cost_candidate`는 conservative low-risk docs/none tuple 후보를 찾는 보조 신호입니다. `evaluation_diagnostics.task_buckets[].threshold_advisory_status`는 같은 bucket의 표본이 고정 기준을 넘었는지 알려주는 advisory-only 힌트이며 selection rule을 직접 바꾸지 않습니다.
- Downshift 후보는 같은 routing decision tuple과 category/label 또는 risk factor가 충분한 accepted 표본을 가진 경우에만 검토합니다. Verification scope가 더 넓어졌거나 risk가 올라간 작업은 기존 low-risk tuple의 성공 사례로 대체하지 않습니다.
- Upshift 후보는 같은 requirement/routing decision tuple에서 reviewer `needs_fix`, `needs_human`, rejected/needs_followup, auto-fix, retry 비용이 반복될 때 검토합니다.
- 실제 model requirement 또는 model selection 기준 변경은 report 실행과 분리된 operator change로 수행합니다. 대상 tuple, 기존 requirement/selection rule, 새 rule, 근거 report 범위, rollback 기준을 public-safe docs 또는 local operator memo에 남긴 뒤 derivation 기준 또는 config를 수정합니다.


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
- Task metadata `capacity_pool`: task가 사용할 pool 이름입니다. 없으면 `codex`로 해석합니다. `cbr enqueue --capacity-pool POOL`로 설정할 수 있습니다. `worker_selection_rules`가 match되는 아직 claim되지 않은 default Codex task는 해당 `worker_target.capacity_pool`을 planned pool로 사용합니다.
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

실제 queue 변경은 `--apply`를 명시한 경우에만 수행됩니다. Apply mode는 runner와 같은 queue lock을 잡은 뒤 같은 validation을 다시 실행하고, 검증이 통과한 경우에만 제한된 field를 atomic JSON write로 갱신합니다. 현재 apply 대상 field는 `title`, `description`, `category`, `labels`, `depends_on`, `status`, `routing_reason`, `routing_risk_factors`, `routing_experiment`, `routing_size`, `routing_risk`, `verification_scope`입니다. `running` task 대상 mutation과 `status=running` 전환은 거부합니다. Enqueue 뒤 `model_requirement_vector`와 `routing_override` 수정은 거부하며 정정은 새 task revision으로 발급합니다. `routing_size`/`routing_risk`는 allowlisted enum 값만 허용합니다. 적용된 변경은 sanitized `task_mutated` event로 기록하고, 변경이 있었을 때 configured `post_mutation_trigger_command`를 실행합니다.

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

lock 복구 후 `running` task의 `active_runner_hostname`이 현재 host와 같고 valid `active_runner_pid`가 dead로 확인되면 age threshold를 기다리지 않고 즉시 복구합니다. Live same-host PID는 복구하지 않으며, remote/unknown host, missing/invalid PID처럼 liveness를 확인할 수 없으면 `started_at` 기반 stale threshold를 사용합니다. 복구 상태는 `next_prompt`가 있으면 `needs_resume`, 없으면 `runnable`이며 active-run metadata를 지우고 `running_recovered_at`, `running_recovery_reason`, 관측 runner hostname/PID를 남깁니다.


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
  "execution_targets": {
    "balanced_current": {
      "model": "gpt-5",
      "codex_profile": "batch-normal",
      "freshness": {
        "owner": "operator",
        "last_reviewed_at": "2026-07-03",
        "review_after_days": 14
      }
    },
    "low_cost_current": {
      "model": "gpt-5-small",
      "codex_profile": "batch-small",
      "config_overrides": {
        "model_reasoning_effort": "low"
      },
      "freshness": {
        "owner": "operator",
        "last_reviewed_at": "2026-07-03",
        "review_after_days": 14
      }
    },
    "high_capability_current": {
      "model": "gpt-5",
      "codex_profile": "batch-deep",
      "freshness": {
        "owner": "operator",
        "last_reviewed_at": "2026-07-03",
        "review_after_days": 14
      }
    }
  },
  "default_execution_config": {
    "execution_target": "balanced_current"
  },
  "model_selection_rules": [
    {
      "name": "low-cost-docs",
      "when": {
        "reasoning_depth": "low",
        "cost_sensitivity": "high"
      },
      "execution_target": "low_cost_current"
    },
    {
      "name": "high-capability",
      "when": {"reasoning_depth": "high"},
      "execution_target": "high_capability_current"
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
