# 요구사항 계층 문서

이 문서는 공개 저장소 기준으로 유지되는 durable requirement 계층을 정리한다.
개별 topic 문서는 `docs/spec.md`에서 링크되며, 각 항목은 requirement와 validity
조건을 설명하는 인덱스 수준으로만 제시한다.

각 requirement의 `Assumptions`가 더 이상 성립하지 않거나 `Revisit when`의
observable signal이 발생하면 해당 항목은 자동 폐기되지 않는다. 먼저
`under review`로 전환해 근거 문서와 파생 spec/check를 함께 재검증하고,
`retain`, `narrow`, `supersede`, `discard` 중 하나를 기록한다.

## ROOT-REQ-SAFE-UNATTENDED-OPERATION: 운영 집중도와 토큰 낭비 감소를 지키는 안전 우선 스케줄링

- Parent: none
- Previous identifiers: R0
- Root goal: 반복적인 Codex 실행을 직접 관리하는 부담과 토큰 낭비를 줄이면서도 무인 작업에 대한 운영자 제어권을 유지한다.
- Decision class: Durable Requirement
- Status: active
- Validity scope: Durable
- Requirement: `codex-batch-runner`는 실행 가능한 작업이 있을 때만 Codex를 호출하고, 큐/리뷰/안전 구조를 통해 무인 운영에서도 제어권을 유지한다.
- Rationale: 반복 호출 중심의 오퍼레이션에서 수동 개입을 줄이고 토큰 낭비를 방지하며, 실행 판단 근거를 잃지 않기 위해.
- Failure prevented: 대기 작업이 없어도 발생하는 무의미한 Codex 호출, 큐의 무단 변경, 검토 미완료 작업의 완료 처리, 제어 평면 정보 소실.
- Assumptions: Codex 호출은 운영 비용을 소비하며, 무인 실행에서도 처리 가능성과 상태 변경 권한을 호출 전에 판단할 수 있다.
- Derived specs: README와 스펙에서 제시된 제어 중심 운영 모델을 준수한다.
- Revisit when: Codex 호출 비용이 사실상 사라지거나, 무인 실행 모델·운영 주기·수동 개입 정책이 바뀔 때.
- Revisit signal status: not observed
- Evidence: [README.md](../README.md), [docs/spec.md](spec.md)

## REQ-EXECUTION-READINESS-GATES: 실행성 있는 작업에서만 Codex 호출

- Parent: ROOT-REQ-SAFE-UNATTENDED-OPERATION
- Previous identifiers: R1
- Decision class: Durable Requirement
- Status: active
- Validity scope: Durable
- Requirement: `run-next`와 `run-loop`는 pause/lock/cooldown/의존성/용량/리뷰/큐 준비 상태를 선행 점검한 뒤에만 Codex를 호출하고, 불가 상태이면 비실행 결괏값으로 종료한다.
- Rationale: 스케줄러의 핵심 가치는 토큰 절감과 무사한 자동 운영이다.
- Failure prevented: 중단, 잠금, 쿨다운, 의존성 미해결, 리뷰 블록 상태에서의 불필요한 토큰 소모.
- Assumptions: 실행 가능성을 결정하는 gate 상태를 Codex 호출 전에 로컬에서 읽을 수 있고, gate 확인 비용이 호출 비용보다 작다.
- Derived specs: `run-next`는 단일 단위 처리, `run-loop`는 매 반복 config/큐 재로딩, gate 위반 시 실패 상태 변경 없이 중단.
- Revisit when: 실행 gate 우선순위·용량 정책·스케줄링 엔진이 변경되거나, 호출 전에 readiness를 판정할 수 없는 backend가 도입될 때.
- Revisit signal status: not observed
- Evidence: [README.md](../README.md), [docs/spec.md](spec.md), [docs/execution.md](execution.md)

## REQ-AUDITABLE-RECOVERABLE-STATE: 큐 상태의 감사와 복구 가능성

- Parent: ROOT-REQ-SAFE-UNATTENDED-OPERATION
- Previous identifiers: R2
- Decision class: Durable Requirement
- Status: active
- Validity scope: Durable
- Requirement: 큐 mutation과 감사 상태는 보존된 canonical local artifact에서 복구 가능해야 하며, 파생 인덱스와 리포트가 mutation source가 되어서는 안 된다.
- Rationale: 운영 이력을 추적하고 장애 후 복구 가능한 단순한 제어면을 유지해야 한다.
- Failure prevented: 큐/감사 정보 손실 후 복구 불가, SQLite 또는 임시 파생 상태가 진실 소스가 되는 상태.
- Assumptions: 단일 운영자 중심의 로컬 파일 보존 모델이 현재 규모에 충분하며, retention 정책이 삭제한 과거 전체 이력까지 복구 대상으로 요구하지 않는다.
- Derived specs: task JSON과 append-only event JSONL은 canonical mutation/audit source로 유지하고, SQLite는 로컬 읽기 인덱스로만 사용하며, 핵심 명령은 SQLite 없이 동작한다.
- Revisit when: 대체 canonical store, 다중 writer, 원격 동기화, 전체 이력 보존 의무, 또는 새로운 retention 전략을 도입할 때.
- Revisit signal status: not observed
- Evidence: [docs/task-schema.md](task-schema.md), [docs/events-and-index.md](events-and-index.md), [docs/spec.md](spec.md)

## REQ-SEPARATE-EXECUTION-AND-ACCEPTANCE: 실행 완료와 리뷰 수용 분리

- Parent: ROOT-REQ-SAFE-UNATTENDED-OPERATION
- Previous identifiers: R3
- Decision class: Durable Requirement
- Status: active
- Validity scope: Durable
- Requirement: 실행 완료(`completed`)는 리뷰 통과(`accepted`)와 별개로 취급하고, 승인 기록이 있어야만 공식 완료로 간주한다.
- Rationale: 실행 결과만으로는 정책·안전·통합 적합성을 보장할 수 없다.
- Failure prevented: Codex 실행 완료를 곧바로 승인된 결과로 처리하는 오해, 후속 조치 누락.
- Assumptions: 무인 worker 결과는 독립적인 review gate가 필요하며, review 상태를 실행 상태와 별도로 보존할 수 있다.
- Derived specs: `review_status`를 작업 상태와 분리, `review-next` 기본 report-only, reviewer Codex opt-in.
- Revisit when: 결과 신뢰 모델, review gate, mechanical acceptance, 또는 자동 승인 정책을 강화·완화할 때.
- Revisit signal status: not observed
- Evidence: [docs/task-schema.md](task-schema.md), [docs/review.md](review.md), [docs/worktrees.md](worktrees.md)

## REQ-ISOLATED-REVIEW-UNITS: 작업물 격리와 승인 후 통합

- Parent: ROOT-REQ-SAFE-UNATTENDED-OPERATION
- Previous identifiers: R4
- Decision class: Durable Requirement
- Status: active
- Validity scope: Durable
- Requirement: 변경 작업은 main checkout과 격리된 review unit으로 생성·보관하고, 승인 및 명시적 apply 이후에만 통합 대상으로 간주한다.
- Rationale: 메인 체크아웃 오염을 막고, 리뷰 가능 단위를 유지하며, 실패 복구 경로를 명확히 하기 위해.
- Failure prevented: 임시 변경 유출, 승인되지 않은 상태의 통합, 리뷰/복구 근거 상실.
- Assumptions: Git branch/worktree가 현재 변경 작업의 격리와 provenance를 표현하는 적절한 review unit이며, integration target은 명시적으로 식별 가능하다.
- Derived specs: worktree task mode 사용, apply 완료와 review 수락 충족 시 의존성 준비 가능, 정리와 branch 제거는 명시적 수행.
- Revisit when: Git 이외의 작업물, stacked integration, 다른 isolation primitive, 또는 branch/integration 정책을 도입할 때.
- Revisit signal status: not observed
- Evidence: [docs/worktrees.md](worktrees.md)

## REQ-BOUNDED-OPT-IN-AUTOMATION: 자동화는 보고 우선·범위 제한

- Parent: ROOT-REQ-SAFE-UNATTENDED-OPERATION
- Previous identifiers: R5
- Decision class: Durable Requirement
- Status: active
- Validity scope: Durable
- Requirement: 상태 변경 자동화는 기본 report-only 또는 비활성으로 두고, 파괴적 정리·branch 삭제·review 자동화는 명시적 opt-in에서만 수행한다.
- Rationale: 큐, 코드, worktree 등 제어 평면 자산은 과도한 자동화로 쉽게 손상될 수 있다.
- Failure prevented: 무한 review loop, 근거 정보 삭제, apply 이전 정리, active task의 의도치 않은 변형.
- Assumptions: false positive 자동 변경의 손실이 추가 operator 확인 비용보다 크며, 안전한 mutation class를 opt-in과 bounded gate로 구분할 수 있다.
- Derived specs: `review-next` 기본 보고 전용, reviewer Codex 기본 비활성, auto-fix 상한 적용, apply-cleanup/branch-prune 기본 dry-run.
- Revisit when: 자동화 정책, risk 허용도, operator trust model, rollback 보장, 또는 mutation class별 검증 근거가 바뀔 때.
- Revisit signal status: not observed
- Evidence: [docs/review.md](review.md), [docs/execution.md](execution.md), [docs/worktrees.md](worktrees.md)

## REQ-PUBLIC-PRIVATE-BOUNDARY: 공개/비공개 경계 보호

- Parent: ROOT-REQ-SAFE-UNATTENDED-OPERATION
- Previous identifiers: R6
- Decision class: Durable Requirement
- Status: active
- Validity scope: Durable
- Requirement: 공개 문서와 출력물은 실제 prompt, transcript, session/thread id, credential, 개인 경로 등을 노출하지 않는다.
- Rationale: 공개 저장소 특성상 운영 노이즈와 민감정보가 공개되면 부적절한 정보 유출이 발생한다.
- Failure prevented: 실제 queue/log/operator note가 public history에 남는 문제, 감사 모델 오염.
- Assumptions: 저장소와 배포 artifact는 계속 공개되며, local operator state에는 공개할 수 없는 식별자와 실행 context가 포함될 수 있다.
- Derived specs: `.private/` 및 `.codex-batch-runner/`는 private/local-only, event/report payload masking, sanitized fixture만 사용.
- Revisit when: 저장소 공개 범위, 감사/report contract, log schema, redaction model, 또는 credential boundary가 바뀔 때.
- Revisit signal status: not observed
- Evidence: [AGENTS.md](../AGENTS.md), [README.md](../README.md), [docs/events-and-index.md](events-and-index.md), [docs/review.md](review.md)

## REQ-SEPARATE-ROUTING-EVIDENCE: 작업 특성, 실행 선택, 평가 근거 분리

- Parent: ROOT-REQ-SAFE-UNATTENDED-OPERATION
- Previous identifiers: R7
- Decision class: Durable Requirement
- Status: active
- Validity scope: Durable
- Requirement: task intent, model requirement, worker target, concrete execution config, worker/reviewer evidence, provider resource evidence, policy evaluation은 서로 다른 개념과 provenance로 유지한다.
- Rationale: routing/cost 최적화는 작업 요구, worker/backend 선택, provider availability, reviewer reliability, worker quality를 opaque profile label 하나로 합치지 않을 때만 검토 가능한 근거가 된다.
- Failure prevented: legacy profile 이름이 durable policy primitive가 되는 문제, report가 정책을 자동 변경하는 문제, reviewer drift가 worker 평가를 오염시키는 문제, `capacity_pool`이나 local role 이름에서 provider quota bucket을 추론하는 문제.
- Assumptions: 모델·worker·provider 선택 축은 독립적으로 변화할 수 있고, 각 선택의 provenance를 별도 field와 evidence row로 보존할 가치가 있다.
- Derived specs: task JSON은 requirement vector를 저장하고, 실제 선택은 resolved execution/worker config에 기록하며, routing/provider evidence report는 read-only/advisory 진단으로만 사용한다. Queue 밖 실행 근거는 fake task가 아니라 execution surface가 구분된 supplemental evidence row로 병합한다.
- Revisit when: public schema 통합이 더 단순하다는 evidence가 축적되거나, accepted/rejected/routed 표본을 바탕으로 자동 routing policy 변경을 검토할 때.
- Revisit signal status: not observed
- Evidence: [docs/execution.md](execution.md)

## REQ-SEPARATE-PROJECT-AND-RUNTIME-TRUTH: 프로젝트 로컬 진실과 글로벌 큐 분리

- Parent: ROOT-REQ-SAFE-UNATTENDED-OPERATION
- Previous identifiers: R8
- Decision class: Durable Requirement
- Status: active
- Validity scope: Durable
- Requirement: `cbr` runtime queue는 운영 실행 상태만 다루고, 프로젝트 자체의 task/roadmap/dashboard 진실은 project-local 또는 명시된 coordination surface에서 분리해 유지한다.
- Rationale: 운영 queue는 실행 진척용이며, 프로젝트 계획과 요구사항의 source of truth를 대체하지 않는다.
- Failure prevented: runtime queue를 프로젝트 진척으로 오해하거나 프로젝트 truth가 queue로 흡수되는 혼선.
- Assumptions: 여러 프로젝트가 하나의 runner queue를 공유하며, 각 프로젝트가 독립적인 planning/coordination surface를 소유한다.
- Derived specs: `.codex-batch-runner/`는 runtime state 전용, project-local dashboard는 별도 유지, enqueue metadata는 routing 보조로 제한.
- Revisit when: queue가 단일 프로젝트 전용으로 바뀌거나, 명시적으로 project planning truth까지 소유하는 orchestration model을 도입할 때.
- Revisit signal status: not observed
- Evidence: [AGENTS.md](../AGENTS.md), [docs/spec.md](spec.md)

## Revalidation contract

`Revisit when` 신호가 관찰되면 다음 순서로 처리한다.

1. 대상 requirement 상태를 `under review`로 표시한다.
2. `Assumptions`, 근거 문서, 파생 spec/check, automation boundary를 재검증한다.
3. `Revalidation outcome`을 `retain`, `narrow`, `supersede`, `discard` 중 하나로 기록한다.
4. `Affected traces`에 변경되는 requirement, topic spec, README/CLI contract, test, roadmap/policy record를 열거하고 같은 loop에서 갱신한다.

## Tests / Checks

공통 검증 기준:

- 변경된 requirement가 public contract를 바꾸면 관련 topic spec, README, CLI reference, test를 함께 확인한다.
- queue state, review/apply, worktree, event/index, routing behavior를 바꾸는 구현은 관련 unit test와 public/private safety review를 통과해야 한다.
- requirement hierarchy 자체는 index 문서이므로 상세 검증 절차는 topic spec과 test에 둔다.
- durable requirement는 `Validity scope`, `Assumptions`, observable `Revisit when`을 유지한다.
- temporary/scoped policy는 durable hierarchy에 섞지 않고 policy record에 acceptance scope와 `Expiration rule`을 둔다.

## Non-goals

공통 비목표:

- 이 문서는 topic-specific spec을 대체하지 않는다.
- 이 문서만으로 queue mutation, active runner config 변경, review acceptance, worktree apply/cleanup, provider routing policy 변경을 수행하지 않는다.
- private runtime state, 실제 prompt/log/transcript/session/thread id, personal path, Todoist id 같은 operator-local detail을 공개 requirement로 승격하지 않는다.
- temporary experiment policy나 provider workaround를 반복 사용만으로 durable requirement로 승격하지 않는다.

## Automation boundary

공통 자동화 경계:

- `routing-report`, dashboard, review bundle, doctor 같은 진단 표면은 기본적으로 read-only evidence로 취급한다.
- state-changing automation은 각 topic spec의 opt-in, dry-run, stale check, safety gate를 통과해야 한다.
- provider routing policy, reviewer policy, active runner config, public/private 문서 경계 변경은 자동 진단 결과만으로 적용하지 않고 별도 operator decision으로 처리한다.
- revisit signal 검출은 review를 시작할 수 있지만 requirement를 자동 폐기하거나 downstream trace를 자동 변경하지 않는다.
