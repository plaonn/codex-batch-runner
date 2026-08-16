# Automatic Chamber Evidence Contract

이 문서는 CBR이 지원하는 ordinary automatic repository execution에서
Chamber execution/correlation evidence를 자동으로 붙이는 경계를 정의합니다.
현재 supported controller는 `codex-batch-runner`의 `run-next`와
`run-loop`입니다.

## 소유권과 범위

- `chatgpt-context`는 개발 자동화의 policy와 semantic contract를 소유하지만
  native Codex subprocess를 시작하지 않습니다.
- `chatgpt-state/shared/scheduled-dispatch/`와 Pi2 scheduler는 assignment,
  retry/recurrence, immutable attempt/result queue state를 소유합니다. Pi2는
  Codex를 직접 launch하지 않습니다.
- CBR는 eligible task selection, claim, per-dispatch attempt identity, native
  Codex command, process result와 review lifecycle을 소유하는
  execution-controller입니다.
- Chamber core와 Orca core는 이 계약의 구현 owner가 아닙니다. 현재 adapter는
  configured argv provider이며, future provider도 같은 run/settle result contract를
  구현하면 launch transport를 교체할 수 있습니다.

따라서 이 계약의 automatic scope는 CBR queue에 machine-readable source와 함께
admit된 Codex task입니다. ChatGPT→Codex validated handoff가 최종적으로 CBR
queue에 같은 contract로 admit되는 경우도 이 scope에 포함됩니다. source 없는
완전히 ad-hoc interactive session은 이 Requirement의 supported automatic scope가
아니며 execution-only evidence로 남습니다.

## Admission contract

Caller는 enqueue 시 source file을 명시적으로 제공합니다.

```json
{
  "kind": "todoist-task",
  "id": "source-123",
  "revision": "revision-7"
}
```

```bash
cbr --config /path/to/cbr-config.json enqueue \
  --cwd /path/to/repository \
  --prompt-file /path/to/private-prompt.txt \
  --evidence-source-json /path/to/private-source.json
```

CBR는 prompt, latest task, current directory, account, terminal title 또는
recent task를 읽어 source를 발명하지 않습니다. `kind`, `id`, `revision`은
enqueue 뒤 immutable task metadata입니다. Source-bearing task에
`execution_evidence_adapter`가 없으면 native launch 전에 fail closed합니다.

Config의 adapter는 argv-only provider boundary입니다. `run_arguments`와
`settle_arguments`는 adapter의 fixed provider arguments이며 CBR가 아래 exact
fields를 추가합니다.

```json
{
  "execution_evidence_adapter": {
    "command": ["/absolute/path/to/python3", "/absolute/path/to/evidence-adapter.py"],
    "run_arguments": [
      "--chamber-entrypoint", "/absolute/path/to/chamber-entrypoint.js",
      "--chamber-executable", "node"
    ],
    "settle_arguments": [
      "--chamber-entrypoint", "/absolute/path/to/chamber-entrypoint.js",
      "--chamber-executable", "node"
    ],
    "timeout_seconds": 30,
    "producer": "execution-controller"
  }
}
```

## Attempt lifecycle

1. CBR scheduler가 task를 select하고 queue lock 아래에서 claim합니다.
2. CBR의 기존 `active_run_id`를 한 native dispatch attempt의 Chamber
   `execution_id`로 lossless reuse합니다. Retry는 새 `active_run_id`와 새
   `execution_id`를 발급합니다.
3. CBR는 native Codex argv를 configured adapter의 `run` command 뒤에 붙이고
   exact source tuple, execution id, result file, child command를 전달합니다.
4. Adapter는 native child 환경에 source와 execution identity를 전파하고,
   CBR는 result file의 schema, source, execution id를 다시 검증합니다.
5. Child가 성공해도 authority가 없으면 task는 `completed` + `unreviewed`로
   남고 evidence는 `acceptance_state=pending-authority`입니다. 이 단계에서는
   Chamber outcome/unknown event 또는 settlement를 만들지 않습니다.
6. CBR의 explicit review authority가 `accepted` 또는 `rejected`를 기록하면
   CBR는 latest exact attempt에 대해 adapter `settle`을 한 번 호출합니다.
   Adapter가 `recorded` 또는 `idempotent`를 반환할 때만 callback evidence를
   확정합니다.

`automatic_evidence_attempts`와
`automatic_evidence_callback_history`는 append-only sanitized lifecycle입니다.
Callback failure, timeout, malformed response, missing result 또는 identity mismatch는
`unknown-effect`/`reconciliation-required`로 기록하며 external effect를 모른 채
자동 retry하지 않습니다.

## Retry와 authority

같은 source/revision의 retry는 scheduler의 retry semantics를 유지하지만 각
native attempt의 execution identity는 다릅니다. Deferred callback은 항상 latest
attempt record의 exact identity와 source를 사용하므로 이전 attempt로 settlement할
수 없습니다.

CBR task completion은 Chamber acceptance와 동일하지 않습니다. 현재 authority
producer가 없는 ordinary run은 정상적인 pending-authority 상태로 남깁니다.
현재 CBR review/accept 또는 configured review-next acceptance가 authoritative
review result를 제공할 때만 deferred callback이 실행됩니다. Child가 `completed`를
출력한 사실만으로 accepted outcome을 만들지 않습니다.

## Provider replacement boundary

CBR source code는 Orca 내부 service, Orca automation scheduler 또는 Orca-specific
Chamber API를 import하지 않습니다. Current adapter가 Orca public terminal을
선택적으로 사용할 수 있는 것은 adapter implementation detail입니다. Equivalent
provider는 동일한 `run` result file와 `settle` JSON result contract를 구현하면
CBR의 source/attempt/callback semantics를 변경하지 않고 교체할 수 있습니다.
