# Worktree Isolation and Apply Contract

이 문서는 optional git worktree execution isolation, apply/rebase/conflict-fix, cleanup, branch-prune, recovery contract를 정의합니다.

## Optional git worktree execution isolation

Git worktree 기반 실행 격리는 task별 repository 상태를 분리하기 위한 core optional capability입니다. 기본값은 compatibility를 위해 계속 main worktree mode입니다. 즉, `worktree_mode=disabled`에서는 현재처럼 task의 원래 `cwd`에서 실행하고, queue lock, global cooldown, dependency policy, `run-next` 1회당 task 하나 실행 원칙도 그대로 유지합니다. Worktree는 state isolation을 위한 장치이지 기본 token parallelism 기능이 아닙니다.

Opt-in placeholder config는 다음과 같습니다.

```json
{
  "worktree_mode": "disabled",
  "worktree_root": ".codex-batch-runner/worktrees"
}
```

`worktree_mode`의 허용값은 `disabled`와 `task`입니다. `disabled`에서는 기존처럼 task의 원래 `cwd`에서 실행합니다. `task`에서는 `run-next`가 실행 가능한 task를 처리하기 직전에 task별 branch/worktree를 만들거나 기존 연결 상태를 재확인하고, 통과한 worktree를 worker process `cwd`로 사용합니다. `worktree_root`는 relative path이면 runner root 기준으로 해석하며, 기본값은 runtime directory 아래 local-only 경로입니다. Public example에는 실제 absolute path, private queue path, 작업자 계정명을 넣지 않습니다.

### Project-declared reusable pool

Task worktree directory 재사용은 project repository root의 tracked `.cbr.toml`이 opt-in한
경우에만 허용합니다. 파일이 없으면 기존 task별 disposable worktree를 사용합니다.
파일이 존재하지만 Git에 tracked되지 않았거나 parse/schema/path validation이 실패하면
pool을 추측 적용하거나 disposable mode로 조용히 fallback하지 않고 task 실행 전에
fail closed합니다.

```toml
[worktree]
copy = [".env", ".npmrc"]
retain = ["node_modules", ".cache"]

[worktree.pool]
max_slots = 2
idle_ttl_hours = 24

[[worktree.prepare]]
command = ["npm", "install"]
cwd = "."

[[worktree.prepare]]
command = ["npm", "run", "codegen"]
cwd = "."
```

- `copy`는 canonical checkout의 같은 relative path를 task lease 시작마다 slot에 새로
  복사하는 항목입니다. 이전 slot 항목은 먼저 제거하며 source가 없으면 prepare를
  거부합니다. Optional copy를 위한 별도 암묵 규칙은 두지 않습니다.
- `retain`은 task lease 사이에 slot 내부 값을 유지할 수 있는 untracked path입니다.
  Dependency/cache directory처럼 다음 task의 정상 prepare command가 검증·갱신할 수
  있는 항목만 선언해야 합니다.
- `copy`와 `retain`은 repository-relative path만 허용하고 absolute path, `..`, `.git`,
  중복·상하위 overlap을 거부합니다.
- `[[worktree.prepare]]`는 선언 순서대로 실행하는 bounded argv입니다. `cwd`는 slot
  내부 relative directory여야 합니다. Shell string evaluation은 지원하지 않으며
  runner의 bounded shell timeout을 적용하고 stdout/stderr를 task metadata에 보존하지
  않습니다. Repository 자체를 신뢰할 수 없는 경우 project-declared prepare command를
  실행하면 안 됩니다.
- `max_slots`는 해당 repository와 policy fingerprint에 유지할 최대 slot 수입니다.
  `idle_ttl_hours`를 지난 idle slot은 다음 pool maintenance/acquire 시 제거 후보가 됩니다.
- Policy fingerprint 단위는 canonical repository identity, schema version, normalized
  `copy`/`retain`/prepare/pool declaration입니다. Base commit은 fingerprint에 넣지 않고
  lease 시작 시 Git reset/switch guard로 갱신합니다.

Pool slot은 task review unit이 아닙니다. Task branch와 task metadata가 review/apply
provenance를 계속 소유하며, slot은 한 번에 하나의 task lease만 가질 수 있습니다.
Lease 시작 시 CBR는 tracked state를 requested base commit으로 맞추고, `retain` 이외의
기존 untracked state를 제거하고, `copy`를 canonical checkout에서 refresh한 뒤 prepare
commands를 실행합니다. Prepare command가 tracked state를 변경하면 실행을 거부합니다.

Lease 종료 시 applied/no-change/discard disposition과 branch 보존 정책은 기존 계약을
그대로 따릅니다. Reusable slot은 task branch에서 detach하고 tracked state를 canonical
baseline으로 reset한 뒤 `retain` 외 untracked state를 제거하여 idle로 전환합니다.
Task metadata의 terminal `execution_worktree_status=cleaned`와 pool slot의
`idle|leased` 상태는 별도 lifecycle입니다. 따라서 archived task에 active task lease가
남는 것은 invariant 위반이지만, 해당 task와 연결이 해제된 idle pool slot이 존재하는
것은 위반이 아닙니다.

Worktree mode의 핵심 모델:

- Main worktree는 stable baseline으로 유지합니다. Raw task execution은 기본적으로 main에 직접 commit/merge하지 않습니다.
- 각 implementation task는 task-specific branch와 worktree에서 실행될 수 있습니다. 기본 branch 이름은 `cbr/<task-id>`이며, Git ref 규칙을 통과하도록 sanitize합니다.
- Completed-but-unreviewed task는 자기 branch/worktree에 그대로 남을 수 있습니다. 그동안 독립 task는 다른 task branch/worktree에서 순차 실행할 수 있습니다.
- Review, reject, follow-up fix, accept는 main worktree에 unrelated task commit을 섞지 않고 해당 task branch/worktree를 대상으로 동작해야 합니다.
- Accepted dependency policy가 dependent task의 base를 결정합니다. 독립 task는 configured base branch 또는 main baseline에서 시작하고, accepted parent가 필요한 dependent task는 parent task branch 또는 parent가 explicit merge/apply phase로 main에 반영된 ref에서 시작합니다.
- Accepted task의 main 반영은 raw execution phase가 아니라 explicit merge/apply phase에서 수행합니다. Fast-forward 또는 merge commit 허용 여부는 별도 config와 operator action으로 제한하며, 기본 raw execution은 main을 갱신하지 않습니다.
- Runner는 기본적으로 push하지 않습니다. Remote push helper는 task branch 대상으로만 explicit opt-in이어야 하며, protected baseline branch 직접 push는 금지합니다.

Worktree 격리가 도움을 주는 영역:

- task별 작업 디렉터리를 분리해 main worktree의 dirty file과 충돌할 가능성을 줄입니다.
- 같은 repository에서 여러 branch 또는 여러 project routing target을 다룰 때 작업 산출물을 task 단위로 추적하기 쉽게 합니다.
- 실패한 task의 파일 상태를 보존해 후속 review, 수동 복구, 재시도 판단을 쉽게 합니다.
- single-runner 정책을 유지하면서도 completed-but-unreviewed 산출물과 다른 독립 task 실행이 main worktree를 더럽히거나 서로 다른 task state를 섞지 않고 공존하게 합니다.
- 기본 dependency readiness policy가 review backlog보다 throughput과 latency를 우선하는 동안, worktree 격리는 completed-but-unreviewed 결과를 독립적인 후속 작업과 분리해 운영 위험을 줄이는 보완 장치가 됩니다.

Worktree 격리가 해결하지 않는 영역:

- runner의 queue lock, global cooldown, dependency policy, single-task-at-a-time 기본 실행 정책을 대체하지 않습니다.
- Worker가 의도에 맞는 변경을 했는지, 공개 저장소 안전 정책을 지켰는지, 검증이 충분한지는 여전히 review workflow가 판단해야 합니다.
- stale `git_status` snapshot, 오래된 unpushed/ahead 정보, task 완료 후 operator가 push 또는 추가 commit을 수행한 상태는 worktree만으로 신뢰할 수 없습니다.
- 같은 branch나 같은 파일을 여러 task가 수정할 때 생기는 semantic conflict를 자동으로 해결하지 않습니다.
- credentials, runtime state, 실제 prompt/log/session id/thread id를 보호하는 public/private safety policy를 완화하지 않습니다.

Task metadata model:

- `execution_mode`: `main_worktree` 또는 `git_worktree`
- `execution_original_cwd`: original task `cwd` 또는 sanitized relative reference
- `execution_repo_root`: original repository root, report에는 sanitized 또는 relative 형태로 표시
- `execution_worktree_path`: task worktree path, public report에는 absolute personal path를 그대로 표시하지 않음
- `execution_worktree_root`: configured root의 resolved path 또는 redacted display value
- `execution_branch`: sanitized task branch, 예: `cbr/task-20260620-001`
- `execution_base_ref`: worktree 생성 기준 ref
- `execution_base_head`: worktree 생성 기준 commit
- `execution_parent_task_id`: parent branch 기반 실행이면 parent task id
- `execution_merge_target`: accepted apply 대상 baseline, 예: `main`
- `execution_worktree_status`: `prepared`, `running`, `retained`, `cleanup_candidate`, `cleaned`, `missing`, `recovery_required`
- `execution_apply_status`: accepted task branch가 main worktree에 적용되었으면 `applied`
- `execution_applied_at`: apply 성공 시각
- `execution_applied_head`: apply 후 main worktree `HEAD`
- `execution_apply_target`: apply 대상 branch 또는 baseline 이름
- `execution_rebase_status`: stale-base apply가 task branch를 재배치했거나 막힌 경우 `rebased` 또는 `blocked`
- `execution_rebased_at`: stale-base rebase 성공 시각
- `execution_rebased_from_base`: rebase 전 `execution_base_head`
- `execution_rebased_onto`: rebase 대상이 된 current main `HEAD`
- `execution_rebased_from_head`: rebase 전 task branch `HEAD`
- `execution_rebased_head`: rebase 후 task branch `HEAD`
- `execution_rebase_blocker`: stale-base rebase가 막힌 sanitized reason
- `execution_rebase_blocked_at`: stale-base rebase blocked 기록 시각
- `execution_conflict_fix_status`: stale-base conflict-fix subtask 상태. 현재 값은 `queued` 또는 `applied`
- `execution_conflict_fix_task_id`: stale-base conflict를 port하기 위해 enqueue된 linked conflict-fix task id
- `execution_conflict_fix_queued_at`: conflict-fix subtask enqueue 시각
- `execution_apply_via_task_id`: parent task result가 linked conflict-fix task apply를 통해 integration target에 반영된 경우 해당 task id
- `execution_cleanup_kind`: worktree cleanup 종류. 현재 값은 `applied` 또는 `discard`
- `execution_cleanup_reason`: cleanup 허용 근거. 예: `execution_apply_status=applied`, `resolution=superseded`, `review_status=rejected`
- `execution_cleanup_branch_retained`: cleanup이 local branch를 보존했는지 여부. 현재 cleanup command는 항상 `true`를 기록합니다.
- `execution_cleanup_result_applied`: cleanup 당시 task result가 integration target에 적용된 상태였는지 여부
- `execution_branch_prune_status`: local task branch pruning 결과. 성공 시 `pruned`
- `execution_branch_pruned_at`: local task branch pruning 시각
- `execution_branch_prune_reason`: branch pruning 허용 근거. 현재 값은 `execution_apply_status=applied`
- `execution_branch_pruned_head`: 삭제 직전 local task branch `HEAD`

Branch naming and base policy:

- 기본 branch pattern은 `cbr/<task-id>`입니다. Slash를 포함한 task id 충돌을 피하기 위해 invalid ref 문자는 `-`로 바꾸고, 연속 separator를 축약합니다.
- Existing branch가 있으면 task metadata와 branch HEAD가 일치할 때만 재사용합니다. 다른 task가 만든 branch이거나 base가 맞지 않으면 실행하지 않고 recovery 또는 operator review로 남깁니다.
- Independent task의 기본 base는 main worktree의 current `HEAD` 또는 configured baseline ref입니다. Worktree 생성 또는 재사용 guard가 recovery-required 상태를 감지하면 stale/recovery 상태로 보고 worker 실행을 거부합니다.
- Dependent task는 dependency가 `accepted`이고 dependency policy가 branch inheritance를 요구할 때 parent branch를 base로 삼을 수 있습니다. Parent가 이미 explicit merge/apply phase로 main에 반영되었으면 main baseline에서 시작할 수 있습니다.
- `dependency_requires_accepted_review=false`인 compatibility mode에서도 worktree branch inheritance는 completed-but-unaccepted parent를 자동 base로 쓰지 않습니다. Parent branch 기반 실행은 accepted parent 또는 explicit operator override가 필요합니다.

Review, reject, follow-up, accept model:

- `run-next`는 task JSON의 canonical `cwd`를 원래 task cwd로 보존하고, worker에 전달하는 실행 cwd만 task worktree로 바꿉니다. 정상 final JSON이 `completed`이고 task worktree에 변경이 남아 있으면 runner는 final JSON의 `changed_files`에 보고된 안전한 상대 경로만 stage하여 task branch에 local commit을 만듭니다. 이 commit은 review unit을 고정하기 위한 것이며 remote push 또는 main 반영은 수행하지 않습니다. `external-json-command` v1 workers must not commit or push; if a completed external-json-command worktree run has worker-created local commits before cbr auto-commit, runner rejects the result and retains the worktree/branch without deleting commits or rewriting history. 저장하는 `git_status` snapshot은 자동 commit 또는 rejection guard 이후 실제 worker 실행 cwd인 task worktree에서 수집합니다.
- `review-bundle`은 main repository state와 task worktree state를 분리해 표시합니다. Completion-time snapshot, review-time current main state, review-time task worktree state, branch, base ref, inferred commits, retained worktree path 존재 여부를 각각 기록합니다. Worktree-backed task에서 `execution_base_head..execution_branch` commit 또는 commit range를 추론할 수 있으면 이를 원자적인 review unit으로 취급합니다. Compatibility field인 `current_git_repository`와 `git_repository`는 review gate가 검사하는 task execution repository를 가리키며, worktree-backed task에서는 task worktree state입니다.
- `summary`, `review-bundle`, `review-next`, `doctor`는 worktree 준비/정리 단계가 저장한 task metadata를 read-only로 표시합니다. 표시 대상은 `execution_mode`, branch, base ref/head, apply status/head/target, worktree status, sanitized worktree path/root이며, 실제 개인 절대 경로는 공개 보고에 그대로 노출하지 않습니다.
- `review-next`는 missing/stale/recovery_required worktree metadata를 별도 report field와 warning으로 표시합니다. 이 warning은 operator review를 돕기 위한 정보이며, 기존 review gate가 명시적으로 요구하지 않는 한 단독으로 fatal gate가 되지 않습니다.
- `doctor`는 configured `worktree_mode`, `worktree_root`, retained/recovery_required/missing metadata task count를 가볍게 요약합니다. 이 점검은 worktree 실행을 시작하거나 정리 작업을 수행하지 않습니다.
- `reject`는 task branch/worktree를 보존하고 `review_status`만 갱신합니다. Reject 자체가 branch를 삭제하거나 main을 되돌리지 않습니다.
- `reject --follow-up`은 새 task를 자동 생성하지 않고 원 task에 `chain_status=needs_fix`와 `review_follow_up` linkage metadata를 기록합니다. Metadata는 원 task id, execution mode, source branch, source worktree status/path, source repo root, `task_generation=not_created`를 포함할 수 있습니다. Follow-up fix가 같은 task branch를 재사용하거나 `cbr/<task-id>-fix-N` branch를 만들 수 있으므로, review bundle은 원 task와 fix branch linkage를 표시해야 합니다. `list`, `summary`, `review-next`는 unresolved `completed + needs_followup` task에 대해 linked follow-up task가 없는지, active/review-needed/accepted/blocked follow-up task가 있는지, 또는 explicit resolution이 필요한지를 next action으로 표시합니다. 이 report는 operator guidance이며 follow-up 생성, resolution, cleanup을 자동 수행하지 않습니다.
- `accept`는 task 결과를 완료로 인정한 뒤 worktree-backed accepted task이면 같은 queue lock 안에서 post-accept worktree apply path를 시도합니다. Main HEAD가 task `execution_base_head`와 같으면 fast-forward apply까지 수행해 dependency availability를 실제 applied 상태와 맞춥니다. Clean stale-base rebase는 re-review로 되돌리고, stale-base conflict는 bounded conflict-fix subtask를 enqueue합니다. Existing review/follow-up chain metadata가 있으면 chain status를 `accepted`로 닫되, rebase/conflict path가 다시 `awaiting_review` 또는 `fixing` 상태로 바꿀 수 있습니다.
- `cbr worktree apply TASK_ID --dry-run`은 accepted worktree task branch의 명시적 main 반영 가능 여부를 보고합니다. Report는 branch, base/head, main head, apply target, apply strategy, commit range summary, gate 결과, errors, warnings를 포함합니다. Main `HEAD == execution_base_head`이면 planned action은 fast-forward apply입니다. Main `HEAD`가 `execution_base_head` 뒤에 clean linear commit으로 이동했고 나머지 guard가 모두 통과하면 planned action은 stale-base rebase입니다.
- `cbr worktree apply TASK_ID --apply`는 runner와 같은 queue lock 아래에서 dry-run과 같은 validation을 다시 수행합니다. Main `HEAD == execution_base_head`인 경우에만 main worktree에서 `git merge --ff-only <execution_branch>`를 실행합니다. 이 fast-forward path는 `status=completed`, `review_status=accepted`, `execution_apply_status`가 아직 `applied`가 아님, `execution_mode=git_worktree`, branch/base/worktree metadata 존재, recovery-required가 아닌 retained worktree, clean main worktree, `execution_base_head` 위에 있는 task branch, branch에 적용할 commit이 하나 이상 있는 상태만 허용합니다.
- Main `HEAD`가 `execution_base_head`와 다르지만 `execution_base_head`를 포함하는 forward-only 상태이고, main worktree와 task worktree가 모두 clean이며, task branch가 `execution_base_head` 위에 있고, detached temporary worktree preflight에서 clean rebase가 확인되면 `--apply`는 task branch/worktree에서만 `git rebase <current-main-head>`를 실행할 수 있습니다. 이 stale-base rebase는 merge commit, cherry-pick, remote push, conflict marker editing을 수행하지 않습니다. 성공 시 task metadata의 `execution_base_head`/base ref/branch head rebase fields를 갱신하고, 이전 accepted review를 무효화하여 `review_status=unreviewed`로 되돌리며, sanitized `task_worktree_rebased` event를 남깁니다. 같은 command 안에서 main fast-forward apply를 이어서 수행하지 않습니다. 운영자는 re-review 후 다시 `accept`하거나 post-accept apply path가 다시 실행되게 해야 합니다.
- Stale-base rebase preflight 또는 actual rebase가 conflict를 보고하면 cbr는 `worktree apply` 안에서 conflict marker를 직접 편집하지 않습니다. Actual rebase conflict는 `git rebase --abort`로 branch/worktree를 원래 상태로 복구하려고 시도하고, task에 `execution_rebase_status=blocked`, sanitized `execution_rebase_blocker`, `execution_conflict_fix_status=queued`를 기록합니다. 그 다음 parent/root chain에 연결된 bounded `worktree_conflict_fix` subtask를 최대 한 개 enqueue하고 `task_worktree_conflict_fix_enqueued` event를 남깁니다. Conflict-fix subtask는 `depends_on=[]`, `subtask_for=<parent>`, `root_task_id`, `parent_task_id`, `blocks_root_completion=true` metadata를 가지며, 자기 worktree에서 parent branch 변경을 current main 위로 port하고 일반 review/apply chain을 통과해야 합니다. Dirty main, missing metadata, non-linear main movement, dirty task worktree, already-applied task, empty commit range 같은 guard failure는 main과 task branch를 변경하지 않고 명확한 report error로 남깁니다.
- Apply command는 merge commit, in-command conflict resolution, cherry-pick, remote push를 수행하지 않습니다. Fast-forward 성공 시 `execution_apply_status=applied`, `execution_applied_at`, `execution_applied_head`, `execution_apply_target`을 task metadata에 기록하고 sanitized `task_worktree_applied` event를 남깁니다. Applied conflict-fix subtask는 linked parent에 `execution_apply_status=applied`, `execution_apply_via_task_id=<conflict-fix-task>`, `execution_conflict_fix_status=applied`를 기록해 existing dependents가 parent changes를 integration target에서 available한 것으로 볼 수 있게 합니다. Worktree cleanup과 branch deletion은 별도 cleanup path에 맡깁니다.

Cleanup and retention:

- 기본 retention은 보수적입니다. unresolved `failed`, unresolved `blocked_user`, `needs_resume`, `completed + unreviewed`, `completed + needs_followup` task의 worktree는 review와 recovery를 위해 보존합니다. `completed/archived + rejected` task나 terminal discard resolution이 있는 failed/blocked/completed/archived task는 result가 명시적으로 거부 또는 폐기된 상태이므로 discard cleanup 후보가 될 수 있습니다.
- Applied cleanup은 `execution_apply_status=applied` metadata가 있는 `completed + accepted` 또는 `archived` worktree task만 후보가 됩니다. Accepted-but-not-applied task는 review는 끝났지만 main 반영이 끝나지 않은 상태이므로 retained worktree cleanup 대상이 아닙니다.
- Applied cleanup은 `execution_applied_head`가 현재 apply target에 포함되는지도 확인합니다. 포함되지 않으면 stale applied metadata로 보고 `recovery_required` 상태를 표시하며 cleanup을 거부합니다. 운영자가 해당 result를 적용하지 않기로 결정한 경우 `cbr worktree discard-stale-applied TASK_ID --resolution superseded|wont_fix|duplicate|manual --reason REASON --apply`로 stale applied metadata를 보존용 discard record로 옮기고 task를 discard cleanup 후보로 전환할 수 있습니다. 이 명령은 stale applied metadata가 확인된 `completed + accepted` retained worktree task에만 적용되며, worktree나 branch를 삭제하지 않습니다.
- Discard cleanup은 result를 적용하지 않기로 명시 결정한 retained worktree task에만 허용합니다. 허용 근거는 `completed`/`archived` task의 `review_status=rejected` 또는 terminal discard resolution allowlist(`superseded`, `wont_fix`, `duplicate`, `manual`)입니다. Resolution-based discard cleanup은 terminal task status(`failed`, `blocked_user`, `completed`, `archived`)에서만 허용합니다. Archived `needs_followup` task도 explicit rejected/resolution signal 없이는 cleanup 후보가 아니며, `smoke` resolution은 적용 포기 의미가 명확하지 않아 allowlist에서 제외합니다.
- `cbr worktree cleanup`은 기본 dry-run이며, `--apply`가 명시되고 cleanup guard가 통과한 경우에만 retained task worktree를 삭제합니다. Local branch, task JSON, runtime log, event log, private state는 삭제하지 않습니다. Task/log/event 파일 삭제는 별도 `cbr prune` semantics를 통해서만 수행합니다.
- Discard cleanup apply는 worktree만 삭제하고 branch를 보존하며 `execution_cleanup_kind=discard`, `execution_cleanup_reason`, `execution_cleanup_branch_retained=true`, `execution_cleanup_result_applied=false` metadata와 sanitized `task_worktree_cleaned` event를 남깁니다. Dry-run/human report는 applied cleanup과 discard cleanup을 `cleanup_kind`/`cleanup_reason`으로 구분합니다.
- Cleanup guard는 target path가 configured `worktree_root` 아래인지, path가 비어 있지 않은지, Git worktree registry에 등록된 path인지, task metadata와 branch가 일치하는지, worktree metadata가 missing/stale/recovery_required 상태가 아닌지 확인해야 합니다.
- Branch deletion은 worktree 삭제와 별도 phase입니다. 기본은 local branch 보존이며, `cbr worktree cleanup`은 branch를 삭제하지 않습니다.
- `cbr worktree branch-prune TASK_ID --dry-run|--apply`는 cleaned worktree task의 local branch pruning 가능 여부를 별도로 보고하거나 적용합니다. 이 command는 worktree directory, remote branch, task JSON, runtime log, event log를 삭제하지 않습니다. `--apply`는 queue lock 아래에서 dry-run과 같은 validation을 다시 수행하고 `git branch -d <execution_branch>`만 사용합니다. Force deletion은 지원하지 않습니다.
- 현재 branch pruning 허용 범위는 보수적으로 applied cleanup에 한정합니다. 대상 task는 `execution_mode=git_worktree`, `execution_branch` 보유, `execution_worktree_status=cleaned`, `execution_cleanup_kind=applied`, `execution_cleanup_result_applied=true`, `execution_apply_status=applied`, `completed + accepted` 또는 `archived` 상태여야 합니다. Discard cleanup(`execution_cleanup_kind=discard`, `execution_cleanup_result_applied=false`) branch는 result가 적용되지 않은 local evidence로 간주해 보존합니다.
- Branch pruning guard는 branch name이 Git ref validation을 통과하고 local `cbr/*` task branch namespace 안에 있으며 task id에서 산출한 sanitized branch와 일치하는지 확인합니다. `main`, `master`, `develop`, `release/*`, `origin/*`, configured apply/base target과 일치하는 branch, current checked-out branch, non-cbr branch는 거부합니다. Git worktree registry에서 해당 branch가 checkout된 곳이 있으면 거부합니다. Local branch가 이미 없으면 no-op report state로 처리하고 destructive path로 보지 않습니다.
- Branch pruning은 가능한 경우 branch `HEAD`를 expected head metadata(`execution_applied_head`, fallback `execution_branch_head` 또는 `execution_rebased_head`)와 비교합니다. Reliable expected head가 없거나 현재 local branch `HEAD`가 expected head와 다르면 거부합니다. 성공 시 `execution_branch_prune_status=pruned`, `execution_branch_pruned_at`, `execution_branch_prune_reason`, `execution_branch_pruned_head`, `execution_cleanup_branch_retained=false`를 task metadata에 기록하고 sanitized `task_worktree_branch_pruned` event를 남깁니다.
- `cbr doctor`는 branch lifecycle visibility를 read-only로 제공합니다. JSON `worktree.task_branches`는 retained/cleaned/pruned task branch metadata, local branch existence/head, apply/cleanup/prune status, path existence boolean, and locally known/configured remote task branch refs 전체를 표시합니다. Human output은 task branch total/displayed/omitted count를 먼저 표시하고 상세 행은 처음 20개로 제한합니다. 이 check는 local git metadata만 읽으며 branch deletion, remote pruning, fetch, pull, or push를 수행하지 않습니다.

## Direct operator worktree maintenance

Direct operator worktree는 cbr task JSON으로 생성/소유되지 않은 local git worktree입니다. 이 lifecycle은 task worktree lifecycle과 분리됩니다. Direct worktree cleanup은 task metadata를 만들거나 수정하지 않고, `.codex-batch-runner/` runtime state를 roadmap/dashboard처럼 사용하지 않습니다.

`cbr maintenance direct-worktrees --dry-run|--apply|--json`는 target repository의 git registry만 읽습니다. Target repository는 기본적으로 command를 실행한 current working directory의 git root이며, `--repo-root PATH`를 주면 해당 path의 git root를 사용합니다. Runtime queue/lock/event paths and configured task `worktree_root` still come from the cbr config, so a central runtime config can inspect whichever repository the operator runs the command from.

- Discovery는 `git worktree list --porcelain`에 등록된 worktree만 대상으로 하며 arbitrary filesystem glob를 scan하지 않습니다.
- Main worktree와 configured `worktree_root` 아래 cbr task worktree는 제외합니다.
- Initial allowlist는 local branch `codex/*`와 current repo sibling path 중 basename이 `<repo-name>-`로 시작하는 worktree입니다.
- Allowlist 밖 branch/path는 cleanup 대상이 아니며 refused/blocked candidate로 보고합니다.
- Allowlisted candidate는 target branch 기준 merged 여부와 dirty status로 `merged+clean`, `merged+dirty`, `unmerged+clean`, `unmerged+dirty` 중 하나로 분류합니다.
- Cleanup eligibility는 `merged+clean`에만 부여됩니다.

`--dry-run`은 eligible과 blocked/refused candidate를 보고만 합니다. `--apply`는 runner lock을 잡고 같은 discovery/classification을 다시 수행한 뒤, 여전히 eligible인 candidate만 `git worktree remove <path>`로 제거하고 local branch를 `git branch -d <branch>`로 삭제합니다. Dirty 또는 unmerged worktree는 삭제하지 않습니다. Force remove, `git branch -D`, fetch, pull, push, rebase, merge, cherry-pick은 이 command에서 실행하지 않습니다.

Worktree removal이 성공했지만 branch deletion이 실패하면 result는 partial로 남고 branch는 수동 확인 대상으로 보고됩니다. Apply는 candidate별 sanitized event를 남기며 prompt text, raw logs, transcripts, session id, thread id, credentials, private operator notes를 event payload에 저장하지 않습니다.

Stale worktree recovery and failure handling:

- Worktree path가 존재하지만 Git registry에 없거나, registry에는 있으나 path가 없으면 `recovery_required`로 표시하고 raw execution을 중단합니다.
- Branch HEAD가 task metadata의 expected head와 다르거나, worktree에 unexpected dirty changes가 있으면 자동 재사용하지 않습니다.
- Worker process 실패, startup stall, final JSON schema failure, runner crash가 발생하면 worktree metadata와 branch ref를 task에 남겨 retry 또는 수동 점검이 가능하게 합니다. Worker 실행이 끝난 뒤 task worktree는 기본적으로 `retained`로 남깁니다. 자동 commit이 실패하거나 `changed_files`가 안전한 상대 경로가 아니어서 stage할 수 없으면 task는 dirty retained worktree로 남고 review gate가 human check를 요구합니다.
- Resume은 기존 session/thread id 정책을 따르되, resume cwd가 같은 retained worktree인지 확인합니다. Retained worktree metadata가 없거나, path/branch/registry check가 recovery-required 상태이면 새 worktree를 만들지 않고 Codex를 호출하지 않습니다. 이 경우 task를 `failed`로 표시하고 `last_error`와 sanitized event에 worktree prepare/recovery failure를 남겨 operator review를 요구합니다.
- Worktree prepare가 실패하면 Codex를 호출하지 않고 task를 `failed` 또는 retryable `runnable`로 돌릴지 phase별로 정합니다. 초기 prepare/cleanup command는 mutation 실패를 task execution 실패와 분리해 report-only로 시작합니다.

Remote push policy:

- Runner execution path는 push하지 않습니다.
- Review bundle과 summary는 local branch ahead/behind, upstream 설정, inferred unpushed commits, optional task result `push_status`를 보고합니다.
- Task branch push helper가 추가되는 경우에도 explicit command와 config opt-in이 필요합니다. 기본 대상은 task branch remote ref이며, main/protected branch push는 지원하지 않거나 별도 hard block을 둡니다.
- Network operation은 `doctor`, `review-bundle`, `run-next` 기본 path에서 실행하지 않습니다.
