# Gateway-neutral execution plan

`gateway-neutral-execution-plan-v1`은 canonical queue task와 current CBR config에서
effective execution identity를 계산하는 deterministic read-only projection입니다.
Worker launch 전에 사람이 확인할 수 있는 sanitized contract일 뿐 새 launcher,
execution result, routing decision, policy enforcement 또는 canonical runtime truth가
아닙니다.

## Binding and output

Plan은 trusted pre-execution delegation의 task revision과 exact resolved execution
target에 bind됩니다. Canonical output은 다음 field만 포함합니다.

- `binding`: public-safe task id, delegation task revision, execution target id
- `provenance`: receipt-compatible resolved target/config/command digests
- `execution`: backend, bounded timeout, output-contract revision
- `policy`: environment policy name과 key name, config-mutation policy name,
  process policy name
- `availability`: `available|unavailable`, fail-closed flag, stable reason codes
- `mutation`: 항상 `allowed=false`, `applied=false`
- `plan_digest`: 다른 모든 canonical field의 SHA-256 digest

동일한 task/config input은 byte-equivalent canonical JSON과 같은 digest를
생성합니다. Example은
[`gateway-neutral-execution-plan-v1.example.json`](../examples/gateway-neutral-execution-plan-v1.example.json)에
있습니다.

## Policy metadata

Target의 optional `gateway_neutral_execution_policy`는 아래 strict metadata
contract를 사용합니다.

```json
{
  "revision": "gateway-neutral-execution-policy-v1",
  "environment": {
    "name": "legacy_inherit_current",
    "allowlisted_key_names": ["LANG", "PATH"]
  },
  "config_mutation": {
    "name": "no_persistent_mutation_v1"
  },
  "process": {
    "name": "legacy_direct_child_timeout_v1"
  },
  "output_contract_revision": "cbr-external-json-final-v1"
}
```

Package 1에서 supported name은 현재 behavior를 설명하는
`legacy_inherit_current`, `no_persistent_mutation_v1`,
`legacy_direct_child_timeout_v1`뿐입니다. Environment 목록은 key name metadata일
뿐 값을 읽거나 child environment를 구성하지 않습니다. `allowlist_v1`,
`posix_process_group_v1` 같은 public-safe opt-in metadata는 이름과 environment key
name을 그대로 projection하되 `availability.status=unavailable`로 fail closed됩니다.
실제 enforcement에는 별도 Package 2 authority가 필요합니다.

Metadata가 없는 기존 task/target은 자동 migration하지 않습니다. Delegation
revision이 없는 task는 `legacy_task_revision_unavailable`, policy metadata가 없는
target은 `legacy_policy_metadata_unavailable`로 표시되고 execution behavior는
변하지 않습니다.

## Stable unavailable reasons

Projection은 invalid 또는 unsupported input을 실행 가능하다고 추측하지 않습니다.
대표 reason code는 다음과 같습니다.

- binding: `task_binding_invalid`, `legacy_task_revision_unavailable`,
  `task_revision_invalid`, `task_revision_binding_mismatch`,
  `target_binding_unavailable`
- resolution: `capacity_canary_projection_unavailable`,
  `legacy_backend_projection_unavailable`,
  `legacy_resolved_target_unavailable`,
  `resolved_execution_identity_unavailable`, `timeout_unavailable`
- policy: `legacy_policy_metadata_unavailable`, `policy_metadata_invalid`,
  `policy_revision_unknown`, `environment_policy_invalid`,
  `environment_policy_unknown`, `environment_allowlist_invalid`,
  `config_mutation_policy_invalid`, `config_mutation_policy_unknown`,
  `process_policy_invalid`, `process_policy_unknown`,
  `output_contract_revision_invalid`

Reason codes는 sorted unique list이며 하나라도 있으면
`status=unavailable`, `fail_closed=true`입니다.

## Privacy and mutation boundary

Plan과 renderer에는 raw argv/command, prompt, environment value, credential
reference/value, cwd, private/log path, stdout/stderr, session/thread/account
identity 또는 provider response를 넣을 수 없습니다. Environment key name에도
credential-like name을 허용하지 않습니다.

`cbr execution-plan TASK_ID`는 queue task와 config를 read-only로 읽습니다. Builder는
input task copy에만 target resolution을 적용하고 queue, config, event, state를 쓰지
않습니다. Worker subprocess, provider/API call, credential lookup 또는 validation도
수행하지 않습니다.

이 projection은 existing execution target, delegation receipt,
`external-json-command`, execution evidence v3, selection telemetry,
review/apply, certification, list/graph/JSON 또는 operator UX semantics를 변경하지
않습니다. Environment/process enforcement, gateway PoC, generic explain command,
fallback engine, credential/provider/routing/default activation, release/deployment도
범위 밖입니다.
