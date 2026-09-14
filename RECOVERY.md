# 복구 기록 (2026-09-14)

이 저장소의 초기 커밋은 **작업 트리에서 소스가 유실된 뒤 빌드 산출물로부터 복구한 스냅샷**입니다.

## 무슨 일이 있었나

- `LMS-MCP/` 작업 디렉터리의 모든 `.py` 소스가 2026-09-14 10:56경 삭제되었습니다.
- 이 프로젝트는 자체 git 저장소를 가진 적이 없었고 상위 리포에도 커밋된 적이 없어, 되돌릴 이력이 없었습니다.
- 남아 있던 것은 `__pycache__`(.pyc), `dist/` 빌드 산출물, `graphify-out/` 뿐이었습니다.

## 무엇을 복구했나

`%TEMP%/lms-mcp-external-storage-build/ssu_lms_mcp-0.1.0.tar.gz` (2026-09-11 15:10 빌드) 를
가장 최신의 온전한 스냅샷으로 사용했습니다. 복구 후 테스트 전체 통과를 확인했습니다
(`161 passed, 1 skipped`).

## 아직 유실된 상태 (미복구)

2026-09-12 ~ 09-14 사이에 추가된 아래 파일들은 이 스냅샷 이후의 작업분이라 포함되어 있지
않습니다. `.pyc` 바이트코드로만 흔적이 남아 있습니다.

| 파일 | 마지막 컴파일 시각 |
| --- | --- |
| `src/ssu_lms_mcp/automation.py` | 09-12 17:07 |
| `src/ssu_lms_mcp/reminders.py` | 09-12 22:58 |
| `src/ssu_lms_mcp/desk_events.py` | 09-13 11:17 |
| `src/ssu_lms_mcp/credentials.py` | 09-14 04:20 |
| `scripts/automation-task.py` | 09-12 19:12 |
| `scripts/install-automation.ps1` | — |
| `scripts/test-scheduler.ps1` | — |
| `docs/automation.md` | — |
| `tests/test_automation.py` | 09-12 17:07 |
| `tests/test_reminders.py` | 09-12 17:08 |
| `tests/test_task_runner.py` | 09-12 19:12 |
| `.agents/skills/ssu-lms-curate/SKILL.md` | — |

또한 `auth.py`(09-14 04:35), `cli.py`(09-14 04:26), `config.py`(09-14 04:19),
`documents.py`(09-12 19:14) 는 09-11 이후에도 수정된 흔적이 있어, 이 저장소의 버전이
최신이 아닐 수 있습니다.

## 남은 작업

- 위 파일들의 원본이 다른 머신·워크트리·백업에 남아 있는지 확인
- 없다면 `.pyc` 디컴파일 또는 재작성
- 앞으로는 작업 트리를 이 저장소에 계속 커밋해 동일한 유실이 재발하지 않도록 할 것
