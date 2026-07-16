# Provider resource report

`cbr provider-resource-report`는 provider resource 관측을 위한 read-only evidence surface입니다. Provider quota evidence를 local scheduler capacity, exact-target selector, execution evidence, 기존 global Codex usage admission gate와 분리합니다.

이 report는 advisory-only입니다. Queue claim, cooldown, wake, target substitution, routing policy rewrite, operator config activation을 수행하지 않습니다. Unknown, stale, invalid, unavailable evidence는 resource-aware candidate에서만 제외하며 기존 실행을 막지 않습니다.

## Snapshot contract

`provider-resource-snapshot-v1`은 한 provider resource observation을 표현합니다.

- `generated_at`은 snapshot 생성 시각입니다. Missing `observed_at`을 대신하지 않습니다.
- `resource.quota_identity`는 opaque verified identity, `unknown`, `unavailable` 중 하나입니다. Account label, credential, model name, wrapper path, backend, model group, `capacity_pool` 이름은 quota identity가 아닙니다.
- `windows`는 서로 독립적입니다. 한 window의 reset을 다른 window에 복사하지 않습니다.
- `remaining.unit`을 명시합니다. Percent는 `0..100`, token/credit/request absolute value는 finite non-negative number여야 합니다. 서로 다른 unit은 합산하거나 비교하지 않습니다.
- `freshness.status`는 `fresh`, `stale_age`, `stale_after_reset`, `unknown` 중 하나입니다. Operator가 `--max-age-seconds`를 지정한 경우에만 report 시점에 freshness를 다시 계산하며 command 자체에는 product threshold 기본값이 없습니다.
- `diagnostics`는 allowlisted reason code만 포함합니다. Raw command output, path, prompt, transcript, credential, account label, session ID, thread ID는 금지합니다.

Strict validator는 unknown field, naive/malformed timestamp, inconsistent observation/reset timestamp, non-finite number, out-of-range percent, duplicate window ID, unsafe identifier를 거부합니다. Report timestamp보다 60초 넘게 미래인 generated/observed timestamp도 invalid입니다.

Native Codex cached-rollout projection은 experimental입니다. Cached `primary`/`secondary` window를 projection하고 quota identity를 `unknown`, observation confidence를 `source_file_mtime`으로 표시합니다. Scheduling-authoritative evidence가 아닙니다. Antigravity projection은 quota identity나 window를 추론하지 않으며 실제 quota/reset source가 생기기 전까지 `resource_capability_unavailable`만 보고합니다.

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

## CLI

이미 projection된 file을 읽는 예시:

```bash
cbr provider-resource-report \
  --snapshot-json snapshot.json \
  --mapping-json mapping.json \
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

Adapter는 implicit shell evaluation 없이 argv list로 실행하며 timeout 상한은 60초, accepted JSON input 상한은 1 MiB입니다. Nonzero exit, timeout, invalid JSON, invalid snapshot은 sanitized status/reason으로 축약합니다. Command argv, stdout, stderr는 report에 복사하지 않습니다.

`--max-age-seconds`를 생략하면 freshness는 `unknown`입니다. Global usage-admission max age를 암묵적으로 재사용하지 않습니다. `--evaluated-at`은 deterministic report/test를 위한 option이며 timezone-aware timestamp만 허용합니다.
