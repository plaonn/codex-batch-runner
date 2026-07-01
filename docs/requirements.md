# 요구사항 계층 문서

이 문서는 공개 저장소 기준으로 유지되는 요구사항 계층을 정리한다.  
개별 topic 문서는 `docs/spec.md`에서 링크되며, 각 항목은 인덱스 수준으로만 제시한다.

제외 항목:
- R7(라우팅/평가 완전 분리)은 현재는 private/future 전용으로 유지한다.

## R0: 운영 집중도와 토큰 낭비 감소를 지키는 안전 우선 스케줄링

상태: 활성  
요구사항: 코덱스(codex-batch-runner)는 실행 가능한 작업이 있을 때만 Codex를 호출하고, 큐/리뷰/안전 구조를 통해 무인 운영에서도 제어권을 유지한다.  
근거: 반복 호출 중심의 오퍼레이션에서 수동 개입을 줄이고 토큰 낭비를 방지하며, 실행 판단 근거를 잃지 않기 위해.  
방지 실패: 대기 작업 없어도 무의미한 Codex 호출, 큐의 무단 변경, 검토 미완료 작업의 완료 처리, 제어 평면 정보 소실.  
파생 규칙: README와 스펙에서 제시된 제어 중심 운영 모델을 준수한다.  
재검토 시점: 무인 실행 모델, 운영 주기, 또는 수동 개입 정책이 바뀔 때.  
근거 문서: [README.md](../README.md), [docs/spec.md](spec.md)

## R1: 실행성 있는 작업에서만 Codex 호출

상태: 활성  
요구사항: `run-next`와 `run-loop`는 pause/lock/cooldown/의존성/용량/리뷰/큐 준비 상태를 선행 점검한 뒤에만 Codex를 호출하고, 불가 상태이면 비실행 결괏값으로 종료한다.  
근거: 스케줄러의 핵심 가치는 토큰 절감과 무사한 자동 운영이다.  
방지 실패: 중단, 잠금, 쿨다운, 의존성 미해결, 리뷰 블록 상태에서의 불필요한 토큰 소모.  
파생 규칙: `run-next`는 단일 단위 처리, `run-loop`는 매 반복 config/큐 재로딩, 게이트 위반 시 실패 상태 변경 없이 중단.  
재검토 시점: 실행 게이트 우선순위, 용량 정책, 스케줄링 엔진 변경이 있을 때.  
근거 문서: [README.md](../README.md), [docs/spec.md](spec.md), [docs/execution.md](execution.md)

## R2: 큐 상태의 파일 기반 감사 가능성

상태: 활성  
요구사항: 태스크 JSON과 append-only event JSONL이 사실상 단일 mutation/audit 원천이며, 파생 인덱스와 리포트는 파일 집합으로 복구 가능해야 한다.  
근거: DB 없이도 운영 이력을 추적하고 장애 후 복구 가능한 단순한 제어면을 유지해야 한다.  
방지 실패: 큐/이벤트 손실 후 복구 불가, SQLite 또는 임시 상태가 진실 소스가 되는 상태.  
파생 규칙: SQLite는 로컬 읽기 인덱스만 사용, 이벤트 payload는 최소화/마스킹, 핵심 명령은 SQLite 없이 동작.  
재검토 시점: 대체 저장소 또는 색인 전략을 도입할 때.  
근거 문서: [docs/task-schema.md](task-schema.md), [docs/events-and-index.md](events-and-index.md), [docs/spec.md](spec.md)

## R3: 실행 완료와 리뷰 수용 분리

상태: 활성  
요구사항: 실행 완료(`completed`)는 리뷰 통과(`accepted`)와 별개로 취급하고, 승인 기록이 있어야만 공식 완료로 간주한다.  
근거: 실행 결과만으로는 정책·안전·통합 적합성을 보장할 수 없다.  
방지 실패: 코덱스 실행 완료를 곧바로 승인된 결과로 처리하는 오해, 후속 조치 누락.  
파생 규칙: `review_status`를 작업 상태와 분리, `review-next` 기본 report-only, reviewer Codex opt-in.  
재검토 시점: 리뷰 게이팅 모델 또는 자동 승인 정책을 강화/완화할 때.  
근거 문서: [docs/task-schema.md](task-schema.md), [docs/review.md](review.md), [docs/worktrees.md](worktrees.md)

## R4: 작업물 격리와 승인 후 통합

상태: 활성  
요구사항: 작업은 작업 단위 브랜치/워크트리에서 생성·보관하고, 승인 및 명시적 apply 이후에만 통합 대상으로 간주한다.  
근거: 메인 체크아웃 오염을 막고, 리뷰 가능 단위를 유지하며, 실패 복구 경로를 명확히 하기 위해.  
방지 실패: 임시 변경 유출, 승인되지 않은 상태의 통합, 리뷰/복구 근거 상실.  
파생 규칙: worktree 모드(task) 사용, apply 완료/리뷰 수락 충족 시 의존성 준비 가능, 정리/가지 제거는 명시적.  
재검토 시점: 통합 파이프라인 혹은 브랜치 정책 변경 시.  
근거 문서: [docs/worktrees.md](worktrees.md)

## R5: 자동화는 보고 우선·범위 제한

상태: 활성  
요구사항: 상태 변경 자동화는 기본 report-only 또는 비활성으로 두고, 파괴적 정리/가지 삭제/리뷰 자동화는 명시적 opt-in에서만 수행한다.  
근거: 큐, 코드, 워크트리 등 제어 평면 자산은 과도한 자동화로 쉽게 손상될 수 있다.  
방지 실패: 무한 리뷰 루프, 근거 정보 삭제, apply 이전 정리, 활성 task의 의도치 않은 변형.  
파생 규칙: `review-next` 기본 보고 전용, reviewer Codex 기본 비활성, auto-fix 상한 적용, apply-cleanup/branch-prune 기본 dry-run.  
재검토 시점: 자동화 정책, 리스크 허용도, 운영자 신뢰 모델이 변경될 때.  
근거 문서: [docs/review.md](review.md), [docs/execution.md](execution.md), [docs/worktrees.md](worktrees.md)

## R6: 공개/비공개 경계 보호

상태: 활성  
요구사항: 공개 문서와 출력물은 실제 프롬프트, 트랜스크립트, 세션/스레드 id, 자격증명, 개인 경로 등을 노출하지 않는다.  
근거: 공개 저장소 특성상 운영 노이즈와 민감정보가 공개되면 부적절한 정보 유출이 발생한다.  
방지 실패: 실제 큐/로그/운영 메모가 public 히스토리에 남는 문제, 감사 모델 오염.  
파생 규칙: `.private/` 및 `.codex-batch-runner/`는 private/local-only, 이벤트/리포트 payload 마스킹, 정제된 fixture만 사용.  
재검토 시점: 감사/리포팅 계약이 추가되거나 로그 스키마가 바뀔 때.  
근거 문서: [AGENTS.md](../AGENTS.md), [README.md](../README.md), [docs/events-and-index.md](events-and-index.md), [docs/review.md](review.md)

## R8: 프로젝트 로컬 진실과 글로벌 큐 분리

상태: 활성  
요구사항: `cbr` 런타임 큐는 운영 실행 상태만 다루고, 프로젝트의 자체 task/roadmap/dashboard 진실은 프로젝트 로컬 문서에서 분리해 유지한다.  
근거: 운영 큐는 실행 진척용이며, 프로젝트 계획/요구사항 본래의 소스 오브 트루쓰를 대체하지 않는다.  
방지 실패: runtime 큐를 프로젝트 진척으로 오해하거나 반대로 프로젝트 진실이 큐로 흡수되는 혼선.  
파생 규칙: `.codex-batch-runner/`는 runtime state 전용, 로컬 task dashboard는 별도 유지, enqueue 메타데이터는 라우팅 보조로 제한.  
재검토 시점: 멀티 프로젝트 오케스트레이션 정책이 변경되거나 큐 오너십 모델이 바뀔 때.  
근거 문서: [AGENTS.md](../AGENTS.md), [docs/spec.md](spec.md)
