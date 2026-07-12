# Model Routing Requirement Contract

이 문서는 role-agnostic task issuer가 제출하는 model requirement v2와 routing rubric의
공개 계약을 정의합니다. Runner는 D1 schema compatibility를 구현했으며 D2 selector 전까지
legacy v1 selection behavior를 deterministic projection으로 유지합니다. Global model-routing
rebuild freeze가 해제되기 전에는 이 계약을 근거로 새 CBR task를 dispatch하지 않습니다.

Requirement reference: `REQ-SEPARATE-ROUTING-EVIDENCE`

## Ownership boundary

- Task issuer는 현재 work unit의 requirement v2, hard constraints, utility preferences를
  소유합니다. Issuer는 implementer, reviewer, retry, fix, follow-up 등 어떤 역할도 될 수
  있으며 role 이름으로 requirement를 추론하지 않습니다.
- CBR selector는 issuer input을 다시 해석하거나 prompt에서 provider/model을 추론하지
  않고, versioned target inventory를 hard constraint, quality, utility 순서로 평가합니다.
- Reviewer, retry, fix, follow-up issuer는 자기 work unit의 새 requirement revision을
  제출합니다. Parent requirement나 `routing_override`는 자동 상속하지 않습니다.
- Report와 recommendation은 evidence를 읽는 advisory surface이며 requirement, inventory,
  routing policy, active config를 자동 변경하지 않습니다.

## Requirement v2 envelope

```json
{
  "schema_version": 2,
  "derivation_version": "requirement-rubric-v1",
  "revision_id": "reqrev-public-safe-id",
  "quality_requirements": {
    "semantic_reasoning": {"score": 750, "confidence": 800, "anchor": 750, "evidence_codes": ["AMBIGUOUS_CONTRACT"]},
    "context_integration": {"score": 500, "confidence": 900, "anchor": 500, "evidence_codes": ["MULTI_MODULE"]},
    "planning_depth": {"score": 750, "confidence": 700, "anchor": 750, "evidence_codes": ["DEPENDENT_STAGES"]},
    "instruction_fidelity": {"score": 1000, "confidence": 1000, "anchor": 1000, "evidence_codes": ["PUBLIC_PRIVATE_BOUNDARY"]},
    "tool_execution_reliability": {"score": 750, "confidence": 900, "anchor": 750, "evidence_codes": ["MULTI_TOOL_MUTATION"]},
    "adversarial_detection": {"score": 250, "confidence": 800, "anchor": 250, "evidence_codes": []}
  },
  "hard_constraints": {},
  "utility_preferences": {
    "latency_weight": 300,
    "cost_weight": 500
  }
}
```

`score`와 `confidence`는 `0..1000` 정수입니다. Issuer가 직접 선택하는 `anchor`는
`0`, `250`, `500`, `750`, `1000` 중 하나여야 합니다. 중간 `score`는 versioned,
deterministic derivation 또는 후속 calibration만 생성할 수 있습니다. `evidence_codes`는
아래 public-safe enum registry 값만 사용하며 free-form rationale은 선택적 설명일 뿐
scoring primitive가 아닙니다.

Requirement revision은 enqueue 뒤 immutable입니다. 정정은 새 `revision_id`를 가진
append-only revision으로 만들고, 각 execution record는 실제 사용한 revision을 고정합니다.

## Quality axes and behavioral anchors

공통 anchor 의미는 다음과 같습니다.

| Score | 의미 |
|---:|---|
| 0 | 해당 능력이 결과에 거의 영향 없음 |
| 250 | 좁고 명시적이며 강한 검증 가능 |
| 500 | 여러 요소를 결합하나 경계가 대체로 명확 |
| 750 | 다단계, 교차 경계, 부분적 모호성 존재 |
| 1000 | 실패 비용이 높고 복합 의미 판단 또는 약한 검증만 가능 |

축별 `0 -> 250 -> 500 -> 750 -> 1000` 해석은 다음과 같습니다.

- `semantic_reasoning`: 명시적 변환 -> local 판단 -> 복수 contract 조정 -> 모호한
  trade-off -> 신규 invariant 도출
- `context_integration`: 단일 artifact -> 소수 local 파일 -> 다중 module -> 코드,
  문서, runtime 종합 -> 다중 subsystem과 장기 결정 종합
- `planning_depth`: 단일 단계 -> 독립 소단계 -> 순차 의존 -> rollback 포함 분기 ->
  장기 상태와 실패 복구
- `instruction_fidelity`: 단순 형식 -> 소수 금지선 -> 여러 scope 규칙 -> 충돌 가능
  규칙 -> 안전과 권한 경계의 literal 준수
- `tool_execution_reliability`: read-only 단일 도구 -> 가역적 단일 변경 -> 다중 도구 ->
  상태 전이와 부분 실패 복구 -> 고위험 외부 상태
- `adversarial_detection`: 비필요 -> 명백한 오류 -> 누락과 회귀 -> 교묘한 contract
  위반 -> 안전과 무결성 적대 검토

`requirement-rubric-v1` evidence code registry는 다음 enum을 정의합니다.

| Code | 의미 |
|---|---|
| `AMBIGUOUS_CONTRACT` | 모호하거나 충돌 가능한 계약 해석 필요 |
| `MULTI_MODULE` | 여러 module의 상태 또는 계약 통합 필요 |
| `DEPENDENT_STAGES` | 순차 의존 단계와 중간 판정 필요 |
| `PUBLIC_PRIVATE_BOUNDARY` | 공개/비공개 또는 권한 경계의 literal 준수 필요 |
| `MULTI_TOOL_MUTATION` | 여러 도구의 상태 변경과 부분 실패 복구 필요 |
| `ADVERSARIAL_REVIEW` | 누락, 우회, 무결성 위반을 적대적으로 탐지해야 함 |

Registry 확장은 rubric version 변경이며 unknown code를 조용히 수용하지 않습니다.

## Hard constraints and unknown policy

```json
{
  "required_execution_surfaces": ["codex"],
  "required_tools": ["filesystem", "shell"],
  "minimum_context_tokens": 200000,
  "allowed_reasoning_efforts": ["medium", "high"],
  "forbidden_provider_families": [],
  "interactive_input_required": false,
  "independent_provider_required": false
}
```

Hard constraints는 quality score나 utility로 보상할 수 없는 eligibility 조건입니다.
Constraint key, value schema, evidence source, freshness, unknown handling은 task가 아니라
versioned constraint registry가 소유합니다.

Unknown policy enum:

- `reject`: safety 또는 mandatory capability이므로 unknown target을 제외함.
- `probe_only`: bounded low-risk discovery에서만 허용하며 정상 automatic routing에서는 제외함.
- `soft_penalty`: cost/latency 최적화 정보로만 사용하며 capability 충족으로 간주하지 않음.
- `ignore`: 해당 registry version에서 routing 입력으로 사용하지 않는 metadata임.

Hard constraint 판정은 `provider_declared`, `surface_reported`, 또는 fresh
`operator_verified` evidence만 확정 충족 근거로 사용할 수 있습니다.
`empirically_observed`와 `unknown`은 해당 constraint registry의 unknown policy를 따릅니다.
Issuer가 task별 unknown policy를 지정하거나 완화할 수 없습니다.

## Utility preferences are not capabilities

`latency_weight`와 `cost_weight`는 hard constraints를 통과하고 quality floor를 만족한
target 사이의 utility tie-break에만 사용합니다. 낮은 latency나 cost는 model capability,
quality requirement, safety constraint가 아니며 quality floor 또는 hard constraint를
완화하지 않습니다. Usage pressure도 quality floor를 낮추지 않습니다.

## Bounded routing override exception

일반 task intent에는 provider/model/profile 이름을 저장하지 않습니다. 유일한 예외는
advanced operator가 exact versioned target을 지정하는 아래 `routing_override`입니다.

```json
{
  "routing_override": {
    "mode": "preference",
    "target_id": "exact-versioned-target",
    "reason": "public-safe reason",
    "scope": "single_task",
    "allow_fallback": false,
    "provenance": "operator_override"
  }
}
```

- `mode`는 `preference` 또는 `pin`입니다.
- `scope`는 항상 `single_task`이며 retry, review, fix, follow-up에 상속되지 않습니다.
- `preference`는 `allow_fallback=true`일 때만 fallback할 수 있고 fallback target도 hard
  constraints를 다시 통과해야 합니다.
- `pin`은 target이 unavailable 또는 ineligible이면 fail closed합니다.
- 두 mode 모두 hard constraints, unknown policy, public/private boundary를 우회하지 않습니다.
- Override는 automatic routing과 별도 evidence cohort로 기록합니다.
- `target_id`는 inventory key일 뿐 task issuer가 provider capability를 선언하는 표면이
  아닙니다. Task에는 별도 model/provider/profile field를 허용하지 않습니다.

## Exact model attribution and provider attestation

Automatic target은 exact model과 reasoning effort를 command에 명시해야 합니다.
`selected_model == command_model`은 execution 전 invariant이며 mismatch는 실행을 막고
integrity evidence를 기록합니다. CLI default에 의존한 run은 exact-model cohort에 넣지
않습니다.

`provider_reported_model`은 optional compliance attestation입니다. Provider가 model을
보고하지 않아도 command-enforced attribution은 유지되지만 attestation confidence는
낮아집니다. Provider-reported mismatch는 결과를 지우거나 command attribution을 바꾸지
않고 adverse integrity evidence로 기록합니다.

Evidence v3는 최소한 `selected_model`, `command_model`, `provider_reported_model`, exact
`reasoning_effort`, `target_id`, `inventory_snapshot_id`, `selection_policy_version`과 사용한
requirement/rubric/constraint/target/review/outcome version을 저장합니다. Evidence v2,
CLI-default, legacy run은 v3 exact-model quality cohort와 합치지 않습니다.

## Versioning, migration, and freeze dependency

Requirement schema/rubric, constraint registry, inventory schema/snapshot, selection policy,
target contract, quality outcome, review policy/rubric, posterior, decay, exploration, cohort
definition은 독립 version을 가집니다. 한 version 변경을 다른 계약의 silent reinterpretation으로
처리하지 않습니다.

Migration order는 `D0 -> D1 -> D2 -> D3 -> D4 -> D5 -> D6`입니다.

- D0: 이 public requirement/rubric contract를 수용함.
- D1: v2 schema, validation, revision identity, legacy projection을 구현함. 새 task는 v2를
  쓰고 v1은 read-only `legacy-derived`로만 읽음. Legacy projection은
  `exact_v2_cohort_eligible=false`로 표시되며 v2 exact cohort에 포함하지 않음.
- D2-D5: exact selector, evidence v3, reports, posterior/exploration을 순차 구현함.
- D6: end-to-end matrix, public/private safety, fresh independent review, operator config
  migration과 명시적 승인을 완료함.

D1 이후 구현은 parent가 D0를 수용하기 전 시작할 수 없습니다. Global CBR dispatch
freeze는 D6 수용과 operator의 명시적 해제 전까지 유지되며, 문서 계약 또는 부분 구현은
freeze 해제 근거가 아닙니다.
