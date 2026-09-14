# 복구 기록 (2026-09-14)

이 저장소의 초기 커밋은 **작업 트리에서 소스가 유실된 뒤 빌드 산출물과 바이트코드로부터
복구한 스냅샷**입니다.

## 무슨 일이 있었나

- `LMS-MCP/` 작업 디렉터리의 모든 `.py` 소스가 2026-09-14 10:56경 삭제되었습니다.
- 이 프로젝트는 자체 git 저장소를 가진 적이 없었고 상위 리포에도 커밋된 적이 없어,
  되돌릴 이력이 없었습니다.
- 남아 있던 것은 `__pycache__`(.pyc), `dist/` 빌드 산출물, `graphify-out/` 뿐이었습니다.

## 복구 방법과 검증

두 단계로 복구했습니다.

1. **스냅샷 복원** — `%TEMP%/lms-mcp-external-storage-build/ssu_lms_mcp-0.1.0.tar.gz`
   (2026-09-11 15:10 빌드)에서 21개 파일을 복원했습니다.
2. **바이트코드 복원** — 스냅샷에 없던 8개 파일을 `__pycache__`의 CPython 3.12
   바이트코드에서 재작성했습니다.

**검증 방법**: 복구한 소스를 다시 컴파일해 원본 `.pyc`와 대조했습니다. 코드 객체의
바이트코드·상수·이름·플래그뿐 아니라 **줄 번호/열 위치 테이블(`co_linetable`)까지**
비교했으므로, 토큰 단위 위치가 원본과 일치합니다. 테스트 파일은 pytest의 assertion
rewriting을 동일하게 적용한 뒤 대조했습니다.

결과: **소스 34개 파일 전부가 원본 바이트코드와 완전히 일치**합니다.

| 구분 | 파일 수 | 검증 |
| --- | --- | --- |
| `src/ssu_lms_mcp/` | 23 | 바이트코드 + 줄/열 테이블 일치 |
| `tests/` | 10 | pytest 재작성 후 바이트코드 + 줄/열 테이블 일치 |
| `scripts/` | 1 | 바이트코드 + 줄/열 테이블 일치 |

바이트코드 복원 대상 8개 파일:

| 파일 | 마지막 컴파일 시각 |
| --- | --- |
| `src/ssu_lms_mcp/automation.py` | 09-12 17:07 |
| `src/ssu_lms_mcp/reminders.py` | 09-12 22:58 |
| `src/ssu_lms_mcp/desk_events.py` | 09-13 11:17 |
| `src/ssu_lms_mcp/credentials.py` | 09-14 04:20 |
| `scripts/automation-task.py` | 09-12 19:12 |
| `tests/test_automation.py` | 09-12 17:07 |
| `tests/test_reminders.py` | 09-12 17:08 |
| `tests/test_task_runner.py` | 09-12 19:12 |

### 복구되지 않은 정보

바이트코드에 보존되지 않는 것은 복원할 수 없습니다.

- **주석**: 원본에 주석이 있던 줄은 빈 줄로 남아 있습니다
  (`scripts/automation-task.py` 105행, `collect_with_relogin`의 313·316행 등).
- **동등한 표현의 선택**: 상수 폴딩·집합 리터럴 순서 등 바이트코드가 같아지는 표기 차이.

## 아직 유실된 상태 (복구 불가)

바이트코드가 없는 비(非)파이썬 파일 4개는 복원할 근거가 없습니다.

| 파일 | 비고 |
| --- | --- |
| `.agents/skills/ssu-lms-curate/SKILL.md` | 4,349바이트 / 611단어. graphify 캐시에 frontmatter(`name: ssu-lms-curate`)와 제목 `SSU LMS Curation`만 남음 |
| `scripts/install-automation.ps1` | 흔적 없음 |
| `scripts/test-scheduler.ps1` | 흔적 없음 |
| `docs/automation.md` | 흔적 없음 |

`SKILL.md`는 큐레이션 프롬프트 본문이므로 임의로 작성하지 않았습니다. 내용을 지어내면
자동 큐레이션이 잘못된 지시로 동작할 수 있습니다.

## 검증 결과

```
pytest  ->  186 passed, 3 failed, 1 skipped
ruff    ->  All checks passed!
```

실패한 3건은 모두 `scripts/automation-task.py`가 `SKILL.md`를 읽는 경로에서 발생하는
`FileNotFoundError`이며, 복구된 코드의 결함이 아닙니다.

- `test_no_pending_does_not_start_codex_and_failure_keeps_pending`
- `test_curation_success_acknowledges_exact_batch`
- `test_stale_receipt_is_never_acknowledged`

MCP 서버는 두 전송 방식 모두 실제로 동작합니다.

- **stdio**: `initialize` → 프로토콜 `2025-11-25`, 툴 15개 노출, `session_status` ·
  `storage_status` · `list_files` 정상 응답, 세션 없는 `list_courses`는 크래시 없이
  "LMS 세션이 없거나 만료되었습니다" 오류 반환, `course_id=-1` 입력 검증 동작.
- **streamable-http**: 로컬 바인딩 후 `initialize` · `tools/list` · `call_tool` 정상.
- **CLI**: `--version` → `0.1.0`, `status` → JSON 정상 출력.

## 남은 작업

- 위 4개 파일의 원본이 다른 머신·백업에 남아 있는지 확인 (특히 `SKILL.md`)
- 앞으로는 작업 트리를 이 저장소에 계속 커밋해 동일한 유실이 재발하지 않도록 할 것
