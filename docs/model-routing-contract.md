# Model Routing Requirement Contract

이 문서는 role-agnostic task issuer가 제출하는 model requirement v2와 routing rubric의
공개 계약을 정의합니다. Runner는 D1 schema compatibility와 D2 exact target selector를
구현했습니다. Legacy v1 config/task는 계속 읽지만 exact automatic cohort에는 들어가지 않습니다. Global model-routing
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
`reasoning_effort`, `command_reasoning_effort`, `target_id`, `inventory_snapshot_id`, `selection_policy_version`과 사용한
requirement/rubric/constraint/target/review/outcome version을 저장합니다. Evidence v2,
CLI-default, legacy run은 v3 exact-model quality cohort와 합치지 않습니다.

Runner는 claim 시 확정한 execution setting을 provider 호출 경계까지 그대로 전달하고,
Codex argv의 단일 `--model` 값 또는 exact external target의 versioned `command_model`을
호출 직전에 다시 확인합니다. 값이 없거나 `selected_model`과 다르면 provider process를
시작하지 않고 `selected_command_mismatch` integrity evidence를 append합니다. External
target은 동일한 `model`과 `command_model`, 그리고 `reasoning_effort`를 모두 제공할 때만 v3 exact cohort에
진입하며 command argv에 각각 정확히 하나인 독립된 `{model}`, `{reasoning_effort}` placeholder를 포함해
wrapper invocation에 두 값을 직접 결속해야 합니다. 기존 external target은 v2
compatibility cohort에 남습니다.

Exact external automatic target은 claim 시 inventory snapshot과 함께 resolved execution
setting에 복사됩니다. 이후 invocation은 mutable task의 `external_command`,
`worker_command_model`, `worker_reasoning_effort`가 아니라 이 snapshot의 command template과
identity를 사용합니다. 실제 치환된 argv에서 model과 reasoning 값이 누락, 중복 또는 불일치하면
provider process를 시작하지 않습니다.

Provider attestation은 optional입니다. Codex의 trusted completion event 또는 external
wrapper의 allowlisted `provider-model+usage-attestation`만
`provider_reported_model`로 읽습니다. 누락 시 `command_attributed`, 일치 시 `verified`,
불일치 시 결과와 command attribution을 보존한 채 `provider_model_mismatch` adverse
integrity evidence로 기록합니다. Raw provider output, prompt, path, session/thread id는
evidence record에 포함하지 않습니다.

Evidence v3 validator는 저장된 파생 판정을 신뢰하지 않습니다. Selected/command/provider
identity에서 integrity와 attestation을, token observation에서 comparability를, routing과
version components에서 selection cohort, exclusion reasons와 cohort id를 다시 계산해 모두
일치할 때만 report reader에 record를 반환합니다.

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
- D2: `execution_target_inventory`와 `constraint_registry`를 사용하는 단일 selector를 구현함.
  Native requirement v2만 automatic exact selection에 진입하며 legacy model/worker first-match
  config는 compatibility path로만 유지함. Codex automatic target은 exact `model`과
  `reasoning_effort`가 필수이고 CLI-default target은 config validation에서 거부함.
- D3-D5: evidence v3, reports, posterior/exploration을 순차 구현함.
- D4 report reader는 v3 identity를 `selected_model`, `command_model`,
  `provider_reported_model`로 분리해 표시하고 adverse integrity를 보존합니다. Routing-cost v2와
  recommendation은 exact automatic/override 및 모든 contract-version 경계를 비교 key에
  포함하며 invalid comparison은 mutation 없이 explicit `insufficient`로 끝납니다.
- D5 outcome projection v1은 append-only raw evidence에서 재생성하는 public-safe 파생
  record입니다. `root_lineage_id`별 최신 projection 하나만 독립 표본으로 사용하고 first-pass와
  recovery-inclusive outcome을 분리합니다. Exact `execution-evidence-v3`와 requirement region의
  `0/250/500/750/1000` anchor만 capability cohort에 들어가며 v1/v2/legacy 및 다른 contract-version
  cohort와 합치지 않습니다.
- `capability-report`는 명시적 versioned decay policy, half-life, Beta prior, Dirichlet prior와
  sanitized outcome projection을 입력받아 posterior를 deterministic하게 rebuild하는 read-only
  surface입니다. Cached input, uncached input, output, reasoning token을 분리하고 log1p weighted
  mean/variance, median/p80/p95/effective sample size를 출력합니다. Timeout/cancel latency는 censored로
  남고 auth/quota/timeout/provider outage는 availability evidence일 뿐 quality failure가 아닙니다.
- `exploration-report`는 explicit reviewed selection probability와 exploration policy version을
  요구하는 read-only admission surface입니다. Eligible candidate, chosen/baseline target, probability,
  probe kind를 기록할 수 있지만 target을 실행하거나 routing/config를 변경하지 않습니다. 한 project의
  동시 probe 1개 제한, high failure-cost 금지, sensitive boundary 금지, budget, rollback/fallback,
  strong objective verification, adverse target/region cooldown을 강제합니다. Contextual Thompson
  Sampling은 D5에 포함하지 않습니다.
- D6: end-to-end matrix, public/private safety, fresh independent review, operator config
  migration과 명시적 승인을 완료함.

D1 이후 구현은 parent가 D0를 수용하기 전 시작할 수 없습니다. Global CBR dispatch
freeze는 D6 수용과 operator의 명시적 해제 전까지 유지되며, 문서 계약 또는 부분 구현은
freeze 해제 근거가 아닙니다.
