# Provider resource report

`cbr provider-resource-report`는 provider resource 관측을 위한 read-only evidence surface입니다. Provider quota evidence를 local scheduler capacity, exact-target selector, execution evidence, 기존 global Codex usage admission gate와 분리합니다.

이 report는 advisory-only입니다. Queue claim, cooldown, wake, target substitution, routing policy rewrite, operator config activation을 수행하지 않습니다. Unknown, stale, invalid, unavailable evidence는 resource-aware candidate에서만 제외하며 기존 실행을 막지 않습니다. Mapping v1 입력은 기존 `provider-resource-report-v1` shape를 유지합니다. Mapping v2 또는 admission policy를 제공하면 `provider-resource-report-v2`의 `authority_preview`를 생성하지만 report 자체의 `scheduling_authoritative`는 `false`로 유지됩니다. D2-A의 별도 `provider-resource-simulate` node도 같은 no-mutation 경계를 유지하며 D2-B runtime activation을 수행하지 않습니다.

## Snapshot contract

`provider-resource-snapshot-v1`은 한 provider resource observation을 표현합니다.

- `generated_at`은 snapshot 생성 시각입니다. Missing `observed_at`을 대신하지 않습니다.
- `resource.quota_identity`는 opaque verified identity, `unknown`, `unavailable` 중 하나입니다. Account label, credential, model name, wrapper path, backend, model group, `capacity_pool` 이름은 quota identity가 아닙니다.
- Strict authority preview에 사용되는 observation은 `resource.observation_scope`에서 opaque host, Codex home, source surface, credential context와 scope revision을 모두 명시해야 합니다. 이 값들은 quota identity를 대신하지 않습니다.
- `windows`는 서로 독립적입니다. 한 window의 reset을 다른 window에 복사하지 않습니다.
- `remaining.unit`을 명시합니다. Percent는 `0..100`, token/credit/request absolute value는 finite non-negative number여야 합니다. 서로 다른 unit은 합산하거나 비교하지 않습니다.
- `freshness.status`는 `fresh`, `stale_age`, `stale_after_reset`, `unknown` 중 하나입니다. Operator가 `--max-age-seconds`를 지정한 경우에만 report 시점에 freshness를 다시 계산하며 command 자체에는 product threshold 기본값이 없습니다.
- `diagnostics`는 allowlisted reason code만 포함합니다. Raw command output, path, prompt, transcript, credential, account label, session ID, thread ID는 금지합니다.
- Window `source.timestamp_provenance`는 `provider_observed_at`, `client_event_at`, `generated_at`, `source_file_mtime`을 구분합니다. 마지막 두 값은 advisory compatibility에는 남을 수 있지만 authority admission clock으로는 사용할 수 없습니다.

Strict validator는 unknown field, naive/malformed timestamp, inconsistent observation/reset timestamp, non-finite number, out-of-range percent, duplicate window ID, unsafe identifier를 거부합니다. Report timestamp보다 60초 넘게 미래인 generated/observed timestamp도 invalid입니다.

Native Codex cached-rollout projection은 experimental입니다. Current `codex-session-rollout-v2` input은 `client_event_at`을 window `observed_at`으로, `rollout-envelope-timestamp`를 canonical `client_event_at` provenance로, `source_adapter_revision`을 producer adapter version으로 보존합니다. 이 contract의 timestamp가 missing, malformed, provenance-mismatched이면 file mtime이나 `generated_at`으로 fallback하지 않고 observation time을 invalid로 표시합니다. Adapter revision이 없는 legacy cached shape만 `source_file_mtime` confidence의 advisory compatibility로 유지합니다. 두 shape 모두 quota identity는 `unknown`이며 scheduling-authoritative evidence가 아닙니다. Antigravity projection은 quota identity나 window를 추론하지 않으며 실제 quota/reset source가 생기기 전까지 `resource_capability_unavailable`만 보고합니다.

Sanitized shape는 [synthetic snapshot example](../examples/provider-resource-snapshot-v1.example.json)을 참조합니다.

## Mapping contract

`provider-resource-mapping-v1`은 exact `target_id`를 operator-verified opaque provider quota identity에 bind합니다. Exact target binding이 primary이며 `capacity_pool`은 local pool projection을 만들기 위한 field일 뿐입니다.

Mapping preview semantics:

- active binding 없음: `missing`
- current exact target inventory에 없는 binding: `invalid`
- expired, not-yet-verified, globally stale mapping: `stale`
- 한 target에 active binding이 둘 이상임: `ambiguous`
- current binding이 정확히 하나임: `mapped`

Mapped target은 같은 verified snapshot에 fresh observed window가 하나 이상 있을 때만 resource-aware candidate가 됩니다. D1에서 이 flag는 advisory입니다.

여러 target이 quota identity 하나를 공유할 수 있습니다. Report는 resource snapshot 하나를 유지하고 target mapping이 이를 참조하게 하며 pool마다 remaining을 복제하거나 합산하지 않습니다. 한 pool에 여러 quota identity가 있으면 `provider_resource_summary_allowed=false`이고 pool-wide provider remaining을 만들지 않습니다.

Sanitized shape는 [synthetic mapping example](../examples/provider-resource-mapping-v1.example.json)을 참조합니다.

## Authority mapping and admission policy

`provider-resource-mapping-v2`는 scheduling authority 후보를 위한 immutable revision입니다. Mapping은 exact target inventory snapshot을 고정하고, 각 binding은 exact `target_id`, provider와 opaque quota identity, observation scope와 scope revision, producer adapter revision, identity authority, verification/expiry/status를 함께 고정합니다.

- Canonical owner는 operator-managed CBR configuration입니다. Adapter, task, role, model alias, backend, wrapper, `capacity_pool`은 binding을 만들거나 완화할 수 없습니다.
- Strict default identity authority는 `source_attested`입니다.
- `operator_attested_single_context`는 schema extension point로만 정의됩니다. Strict policy validator는 이를 활성화하지 않으며 별도 operator decision 없이는 authority가 되지 않습니다.
- Active binding이 없거나, 여러 개이거나, inventory target이 없거나, mapping/scope/producer revision이 다르거나, verification/expiry 범위를 벗어나면 eligibility를 거부합니다.
- Revision은 in-place 수정하지 않고 새 revision과 `supersedes_binding_id`로 교체합니다. Invalidated/superseded binding은 reason을 남깁니다.

Sanitized shape는 [mapping v2 example](../examples/provider-resource-mapping-v2.example.json)을 참조합니다.

`provider-resource-admission-policy-v1`은 mapping과 별도로 threshold와 시간 정책을 소유합니다.

- `policy_revision`, explicit `enabled`, accepted mapping revisions, exact target/window rules와 remaining unit을 고정합니다.
- Accepted observation clock은 `provider_observed_at` 또는 `client_event_at`만 허용합니다. `generated_at` 및 `source_file_mtime` fallback은 authority preview에서 거부됩니다.
- `max_age_seconds`, allowed clock skew, reset grace는 policy revision에 명시합니다. CLI의 advisory `--max-age-seconds`나 기존 global usage gate 값을 암묵적으로 가져오지 않습니다.
- Missing/stale/invalid evidence는 기존 execution을 막지 않는 `allow_existing_execution`만 허용합니다.
- Existing global gate를 먼저 평가하며 global gate가 terminal이면 target gate를 평가하지 않습니다. 같은 reset이 global gate에 이미 포함되면 target decision은 `covered_by_global` evidence만 남기고 별도 wake를 만들지 않습니다.
- Disable/rollback은 새 typed decision 생성을 중단하지만 append-only evidence는 보존하며 legacy scalar는 global gate 전용으로 유지합니다.

Public example은 [admission policy example](../examples/provider-resource-admission-policy-v1.example.json)을 참조합니다. Example threshold와 timing 값은 synthetic schema fixture이며 운영 권고값이 아닙니다.

### Typed gate and dedup contract

`provider-resource-gate-decision-v1`은 future D2 runtime integration 전에 고정하는 typed evidence contract입니다.

- Resource key는 `(provider_id, quota_identity_id, scope_id, window_id)`의 canonical hash입니다.
- Decision key는 policy/mapping revision, resource tuple, observed/reset timestamp, action을 함께 hash합니다.
- Wake key는 resource tuple과 reset timestamp를 hash합니다. 같은 decision/wake key는 한 번만 기록합니다.
- 같은 resource에는 active gate 하나만 존재합니다. 더 늦은 authoritative reset은 이전 decision을 `supersedes_decision_key`로 연결해야 합니다.
- Global reset이 target reset을 포함하면 action은 `covered_by_global`이어야 하며 duplicate wake를 만들 수 없습니다.

`provider-resource-gate-state-v1`의 migration mode는 typed state를 primary로 두되 기존 scalar를 global-only compatibility projection으로 유지합니다. Rollback은 typed evaluation을 비활성화하되 기록을 삭제하거나 scalar에 target gate를 승격하지 않습니다. 이 schema는 아직 runtime state에 저장되지 않습니다.

## D2-A read-only simulator

`cbr provider-resource-simulate`는 selected exact target의 provider-resource 결정을
preview하고 alternative exact target을 비교하는 read-only node입니다. 입력은 다음
versioned contract로 고정합니다.

- `provider-resource-simulation-request-v1`: selected exact target id, immutable
  requirement v2 revision, explicit global gate result를 소유합니다.
- `provider-resource-mapping-v2`: exact target과 source-attested resource identity,
  scope, producer revision을 소유합니다.
- `provider-resource-admission-policy-v1`: threshold, accepted event-time provenance,
  max age, clock skew, reset grace를 소유합니다.

Simulator는 current execution target inventory와 constraint registry를 사용해 모든
alternative에 기존 hard constraint와 static quality floor를 다시 적용합니다.
Selector에서 제외된 target이나 provider-resource authority가 missing, stale, invalid,
unavailable, ambiguous인 target은 alternative recommendation에 포함하지 않습니다.
Selected target의 evidence가 불완전해도 기존 실행은 유지하며 action은
`evidence_only`입니다. 불완전한 evidence에서 `defer`를 만들지 않습니다.

Recommendation action은 `allow`, `defer`, `covered_by_global`,
`evidence_only`만 사용합니다. Authoritative remaining이 versioned policy threshold
이하이고 reset이 유효할 때만 `defer` preview를 만들며, wake preview는 policy의
`reset_grace_seconds`만 더해 계산합니다. Typed decision의 resource, decision, wake
key는 D1.6 canonical key helper로 계산합니다. 이 key와 wake time은 report evidence일
뿐 scheduler wake로 등록되지 않습니다.

Global gate는 항상 먼저 평가합니다. Explicit global input이 terminal `gated`이면
target-scoped defer evaluation을 진행하지 않습니다. Low-resource target reset이 global
reset에 포함되면 `covered_by_global` evidence와 canonical keys를 표시하되 duplicate
wake는 만들지 않습니다. 포함되지 않거나 target이 low-resource가 아니면
`evidence_only`로 남깁니다. Global input 자체가 `unknown` 또는 `fail_open`이면 target
resource가 낮아도 `evidence_only`로 유지하며 defer하지 않습니다.

JSON과 human output은 `read_only=true`, `mutation_allowed=false`,
`scheduling_authoritative=false`, `automatic_substitution=false`,
`d2b_activation=false`를 명시합니다. Queue, event, state, cooldown, wake, config,
routing policy를 읽기 결과로 수정하지 않습니다. D2-B activation, automatic
substitution, provider identity exception, 운영 threshold 선택은 이 command의 범위가
아닙니다.

## CLI

이미 projection된 file을 읽는 예시:

```bash
cbr provider-resource-report \
  --snapshot-json snapshot.json \
  --mapping-json mapping-v2.json \
  --policy-json policy.json \
  --max-age-seconds 300 \
  --json
```

Strict snapshot object를 반환하는 bounded argv adapter 예시:

```bash
cbr provider-resource-report \
  --snapshot-command-json '["resource-adapter", "--json"]' \
  --adapter-timeout 5
```

Cached native Codex usage object와 Antigravity capability absence를 projection하는 예시:

```bash
cbr provider-resource-report \
  --codex-cached-command-json '["cached-usage-reader", "--json"]' \
  --include-antigravity-unavailable
```

`codex-context/scripts/codex-usage-snapshot.sh`처럼 `codex-radar usage --json`을 변환 없이 전달하는 adapter를 사용할 수 있습니다. Current Radar event-time field는 위 projection에서 보존되지만, account/quota identity가 source-attested되지 않은 한 authority preview eligibility는 `false`로 유지됩니다.

Adapter는 implicit shell evaluation 없이 argv list로 실행하며 timeout 상한은 60초, accepted JSON input 상한은 1 MiB입니다. Nonzero exit, timeout, invalid JSON, invalid snapshot은 sanitized status/reason으로 축약합니다. Command argv, stdout, stderr는 report에 복사하지 않습니다.

`--max-age-seconds`를 생략하면 v1 advisory freshness는 `unknown`입니다. Authority preview는 policy revision의 timing만 사용합니다. Global usage-admission max age를 암묵적으로 재사용하지 않습니다. `--evaluated-at`은 deterministic report/test를 위한 option이며 timezone-aware timestamp만 허용합니다.

D2-A simulation 예시:

```bash
cbr provider-resource-simulate \
  --request-json simulation-request.json \
  --snapshot-json snapshot.json \
  --mapping-json mapping-v2.json \
  --policy-json policy.json \
  --evaluated-at 2030-01-02T04:00:00Z \
  --json
```

Sanitized request shape는
[simulation request example](../examples/provider-resource-simulation-request-v1.example.json)을
참조합니다. Example threshold와 timing 값은 synthetic fixture이며 운영 권고값이
아닙니다. Simulator는 provider API나 credential을 조회하지 않으므로 snapshot,
mapping, policy, global gate result를 모두 explicit file input으로 받아야 합니다.
