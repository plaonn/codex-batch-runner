# Private Tasks Template

이 파일은 개인 운영 환경에서 사용할 작업 대시보드 템플릿입니다. 실제 메모는 저장소 루트의 `.private/TASKS.md`로 복사해서 사용하십시오. `.private/`는 gitignore 대상이어야 하며, 공개 저장소에 커밋하지 않습니다.

`.private/TASKS.md`에는 현재 판단과 실행을 돕는 짧은 작업 목록만 기록합니다. 큰 active task의 본문은 `.private/task-bodies/`에 두고, 완료 로그는 Git 기록과 커밋에 맡깁니다.

## Now

- 없음

<!-- 진행 중인 작업이 있으면 아래 형식을 사용합니다.

- [ ] 작업 제목
  - Surface: direct thread / cbr task / manual
  - Scope: 충돌을 피해야 하는 파일 또는 기능 영역
  - Exit: 완료 여부를 판단할 수 있는 짧은 조건
  - Conflict: 동시에 건드리면 안 되는 영역
  - Body: `.private/task-bodies/example.md` 또는 관련 task id
-->

## Watching

- [ ] 작업 제목
  - Exit: 관찰이나 외부 결과 대기가 끝나는 조건

## Later

- [ ] 작업 제목
  - Exit: 실행 후보에서 제거하거나 `Now`로 옮길 조건
