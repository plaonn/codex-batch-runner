# codex-batch-runner 스펙

이 문서는 `codex-batch-runner`의 core contract와 topic-specific public specs의 index입니다. README는 사용자용 entrypoint이고, 세부 구현 truth는 아래 topic 문서와 이 문서의 core section을 기준으로 유지합니다.

## Topic map

- [요구사항 계층 (`ROOT-REQ-*`, `REQ-*`)](requirements.md): 토큰 효율/스케줄링·완화·감사·리뷰·격리·안전 경계·모델 요구사항·프로젝트 진실 분리를 의미 기반 stable identifier로 설명.
- [Model routing requirement contract](model-routing-contract.md): role-agnostic issuer-owned requirement v2, rubric anchors, hard constraints/unknown policy, bounded override, exact model attribution, migration/freeze dependency.
- [Task schema and dependency contract](task-schema.md): task JSON fields, task status, review status, project routing metadata, dependency readiness.
- [Execution contract](execution.md): model requirements, opt-in usage-aware Codex admission, shell and external-json-command backends, capacity and priority, queue mutation, runner execution policy, watchdog, lock, atomic writes, Codex command/prompt wrapper, rate-limit, Codex CLI maintenance.
- [Provider resource report and D2-A simulator](provider-resource-report.md): strict read-only provider resource snapshot/mapping contracts, source-attested authority policy preview, exact-target decision/alternative simulation with selector gates, typed gate/dedup prerequisite contracts, cached Codex and Antigravity capability projections, local-capacity separation, explicit D2-B prohibition.
- [Review contract](review.md): review-bundle, review-next, reviewer Codex safety model, mechanical gates, bounded auto-fix loop.
- [Worktree isolation and apply contract](worktrees.md): worktree execution isolation, apply/rebase/conflict-fix, cleanup, branch-prune, recovery.
- [Events, index, and retention contract](events-and-index.md): event log, local SQLite read index, prune/retention behavior.
- [CLI reference](cli-reference.md): command surface and human/JSON output semantics.
- [Operator installation guide](operator-installation.md): fail-closed CLI/environment/XDG config discovery, guarded managed launchd lifecycle, doctor, manual cooldown wake, cross-project enqueue/check flow.
- [Beta operations guide](beta-operations.md): practical beta operating model, inbox triage, review workflow, JSON output use, smoke checklist.

## Core contract summary

- Runtime state, actual queue files, logs, prompts, transcript data, session ids, thread ids, credentials, and `.local` operator notes are not public documentation artifacts.
- JSON task files and append-only event JSONL files remain the canonical mutation source. SQLite is a rebuildable local read index only.
- `run-next` processes at most one implementation task, auto-review action, or guarded maintenance action per invocation and emits one JSON object with `--json`.
- `run-loop` is the single-worker scheduler command; it repeats the `run-next` path with fresh config/queue checks per iteration, emits JSONL with `--json`, and stops on non-actionable outcomes such as empty queue, pause, cooldown, lock contention, or review-needed state.
- Worktree-backed tasks become dependency-ready only after accepted results are applied to the integration target.
- Review automation is explicit opt-in. Default `review-next` behavior is report-only, and reviewer Codex is disabled unless config and command guards allow it.
- Parent linkage가 있는 attention state는 runtime-private durable outbox에 `parent_attention_required`로 기록합니다. Wake는 collection/disposition 요청일 뿐 root goal 완료를 의미하지 않습니다. 실제 전송은 configured local JSON command adapter를 통해서만 opt-in하며, 검증된 Codex non-UI messaging surface가 없으므로 core가 Codex App capability를 가정하지 않습니다.
- Usage-aware admission is explicit opt-in. It reads one provider-neutral JSON snapshot before a native Codex implementation claim, fails open on adapter errors, and reuses global cooldown plus one-shot wake semantics without probing Codex.
- Destructive cleanup and branch pruning commands are dry-run by default and require explicit `--apply`.

## 목표

`codex-batch-runner`는 로컬 파일 기반 큐에 등록된 작업을 Codex CLI로 순차 처리하는 배치 runner임.

핵심 목표는 Codex CLI를 직접 주기적으로 호출하되, 처리할 작업이 없거나 지금 처리하면 안 되는 상태에서는 Codex를 호출하지 않아 불필요한 Codex 토큰 소모를 줄이는 것임.

runner는 cron, launchd, systemd처럼 외부 스케줄러가 자주 실행해도 안전해야 함. macOS에서는 launchd를 기본 운영 방식으로 문서화하고, cron은 portable fallback으로만 다룸.


## 비목표

- DB 기반 queue는 초기 구현 범위가 아님. SQLite가 명확히 유리해지기 전까지 JSON/JSONL 파일 기반으로 시작함.
- Codex usage remaining을 안정적으로 조회할 수 있다고 가정하지 않음.
- ChromeDriver, browser UI scraping, GUI automation은 core runner에 넣지 않음. 추후 optional adapter 여지는 남김.
- rate-limit reset 시간이 항상 공식적으로 제공된다고 가정하지 않음.
- runner가 코드 diff를 분석해 작업 완료/부분 완료를 자체 판단하지 않음.


## 구현 선택

초기 구현은 Python 표준 라이브러리 기반 CLI로 작성함.

이유:

- 현재 repo에 기존 Node/Python 패턴이 없음.
- 핵심 기능이 파일 I/O, atomic write, lock, subprocess, JSONL parsing, timestamp 처리임.
- runtime dependency 없이 구현 가능함.
- Python dependency/version 관리 약점은 `pyproject.toml`에 Python 버전을 명시하고 외부 runtime dependency를 두지 않는 방식으로 줄임.

권장 기준:

- Python `>=3.11`
- runtime dependency 없음
- CLI entrypoint 제공
- 테스트는 표준 `unittest` 또는 최소 dev dependency로 시작


## 문서 정책

이 프로젝트의 Markdown 문서는 기본적으로 한국어로 작성함.

- README: 한국어 사용자 문서
- 이 스펙: 현재 구현 기준과 public contract를 담는 한국어 기준 문서
- 로컬 private roadmap/task 템플릿: 한국어
- CLI option, config key, JSON schema field, log key, test name은 영어 유지

문서 역할은 공개 여부와 별개로 분리함. 이 저장소의 공개 roadmap/task dashboard를 아직 유지하지 않는 경우에도, 개인 운영 환경에서 미래 방향은 gitignore되는 `.private/ROADMAP.md`, 현재 작업 대시보드는 `.private/TASKS.md`, 큰 active task 본문은 `.private/task-bodies/`로 분리할 수 있음. `codex-batch-runner`의 runtime queue는 여러 프로젝트를 아우르는 Codex 작업 오케스트레이션 상태이며, 이 프로젝트 자체의 task dashboard를 대체하지 않음.

향후 public roadmap 또는 장기 설계 문서가 필요해지면 별도 문서로 분리할 수 있음. `docs/spec.md`는 현재 truth의 index와 core contract를 유지하고, topic-specific contract는 관련 topic 문서에 둠. 구현 전 아이디어나 과거 작업 로그는 누적하지 않음.


## 저장소 공개 운영 정책

이 저장소는 공개 저장소로 운영함.

- 로컬 runtime state, 실제 queue, 실제 로그, 개인 작업 메모는 commit하지 않음.
- 실제 Codex prompt, JSONL 로그, session id, thread id, usage-limit 메시지는 commit하지 않음.
- 테스트 fixture는 sanitized synthetic data만 사용함.
- root `AGENTS.md`는 public-safe bootstrap으로 tracking하고, 로컬 개인 지침은 gitignore되는 `.private/AGENTS.md`에 둠.


## 파일 구조

대표 구조:

```text
codex-batch-runner/
  README.md
  AGENTS.md
  .gitignore
  pyproject.toml
  docs/
    spec.md
    task-schema.md
    execution.md
    review.md
    worktrees.md
    events-and-index.md
    cli-reference.md
    operator-installation.md
    beta-operations.md
  src/
    codex_batch_runner/
      __init__.py
      cli.py
      config.py
      lock.py
      queue.py
      runner.py
      codex.py
      prompts.py
      limits.py
  tests/
    test_queue.py
    test_lock.py
    test_runner.py
  examples/
    config.example.json
    task.example.json
    private/
      ROADMAP.md
      TASKS.md
      task-bodies/
        README.md
  .private/          # optional, gitignored local project-control surface
    AGENTS.md
    ROADMAP.md
    TASKS.md
    task-bodies/
```

실제 runtime state는 기본적으로 아래에 둠.

```text
.codex-batch-runner/
  tasks/
  logs/
  index.sqlite3
  rate-limits/
  runner.lock
  state.json
```

`.codex-batch-runner/`는 gitignore 대상이며 runtime state만 담음. 프로젝트 roadmap, task dashboard, task body, operator planning 문서는 `.private/` 같은 별도 private project-control surface에 둠.


## macOS launchd 운영

macOS 기본 운영 방식은 launchd임.

Guarded lifecycle operation은 기본 dry-run이고 apply에 explicit flag와 exact label confirmation을
요구함. Current-user `gui/UID`와 exact `HOME/Library/LaunchAgents/LABEL.plist` destination만
허용하며, foreign/manual plist를 adoption하지 않음. Owned update와 uninstall은 same-directory
backup, atomic replacement/move, rollback 또는 `recovery_required` result로 recoverability를
보존함. Service lifecycle은 `bootstrap`/`bootout` contract이고 아래 `kickstart` hook은 이미
설치된 job을 깨우는 별도 execution contract임.

Lifecycle의 namespace concurrency threat model은 accidental/non-adversarial concurrency임.
Held directory descriptor, no-follow identity snapshot, atomic rename, mutation 직전 재검증으로
identity 변화를 탐지하고 rollback을 best-effort로 수행함. `bootout`은 plist path가 아니라 exact
`gui/UID/LABEL` service target을 사용함. `bootstrap`은 public `launchctl` contract에 따라 검증된
absolute plist path를 사용하고 호출 직전에 identity를 다시 검증하며, undocumented `/dev/fd`
경로를 사용하지 않음. Public macOS rename/launchctl interface는 inode identity를 mutation 조건으로
원자적으로 묶지 않으므로 active same-UID adversarial namespace mutation은 지원하지 않음. 그 위협을
지원하려면 protected directory 또는 privileged helper 경계가 필요함. CLI/JSON operation result는
이 supported/unsupported boundary를 `namespace_concurrency` contract로 명시함.

권장 모델:

- `StartInterval = 600`
- single-worker LaunchAgent는 `run-loop --json`을 실행해 한 tick 안에서 즉시 eligible follow-up work를 drain함
- optional `post_mutation_trigger_command`로 queue mutation 직후 또는 one-shot `run-next`에서 eligible follow-up work가 남은 task 처리 직후 `launchctl kickstart` 호출 가능
- runner 내부에서 lock, dependency, cooldown, empty queue를 판단
- 실행할 작업이 없으면 즉시 종료
- rate-limit 발생 시 launchd interval을 바꾸지 않고 runner 내부 global cooldown으로 Codex 호출을 막음

cron 예시는 portable fallback으로만 문서화함.

Linux systemd user service 운영에서는 같은 hook에 `["systemctl", "--user", "start", "codex-batch-runner.service"]`를 사용할 수 있음. timer/cron polling은 fallback으로 유지함.


## 최소 테스트

초기 테스트 범위:

- enqueue가 task JSON 생성
- dependency가 완료되지 않은 task는 실행 후보에서 제외
- cooldown 중인 task는 실행 후보에서 제외
- lock acquire/release
- stale lock 복구
- fake Codex success JSONL 처리
- fake Codex `needs_resume` JSON 처리
- fake Codex rate-limit error 처리와 cooldown 설정
- malformed final JSON 처리

fake Codex는 실제 Codex CLI를 호출하지 않고 synthetic JSONL을 stdout으로 출력하는 helper를 사용함.
