# SSU LMS MCP

숭실대학교 Smart Campus의 공지·과제·강의자료를 Buzz 등 에이전트 핸들러에서 조회하는 독립 MCP 서버입니다. 이 폴더만 복사해서 설치할 수 있습니다. Python 3.11 이상이 필요합니다.

서버가 LMS 데이터를 수집하고, 연결한 에이전트가 내용을 요약하거나 질문에 답합니다. 기본 MCP 조회에는 프런트엔드, FastAPI 웹앱, LLM API 키, Notion, Obsidian 설정이 필요하지 않습니다. 아래의 선택 기능인 **OneDrive 아티팩트 학습 자동화**는 작업공간의 `.omp/study`·`.omp/onedrive`, 기존 OMP 모델 인증과 OneDrive 로그인을 사용합니다. Notion 인증은 기존 허브를 가져올 때만 필요합니다.

## 빠른 시작

**Python 3.11 이상과 인터넷 연결**이 필요합니다. 설치할 `LMS-MCP` 폴더의 상위 폴더에서 시작합니다. 폴더만 다른 위치에 복사해도 사용할 수 있으며, 가상환경 활성화나 PowerShell 실행 정책 변경은 필요하지 않습니다.

### Windows / PowerShell

```powershell
cd LMS-MCP
py -3.12 scripts/setup.py
.\.venv\Scripts\python.exe -m ssu_lms_mcp login
.\.venv\Scripts\python.exe -m ssu_lms_mcp config
```

Python 3.11만 설치했다면 첫 설치 명령의 `-3.12`를 `-3.11`로 바꾸세요.

### macOS / Linux

```bash
cd LMS-MCP
python3 scripts/setup.py
.venv/bin/python -m ssu_lms_mcp login
.venv/bin/python -m ssu_lms_mcp config
```

1. `setup.py`는 이 프로젝트의 `.venv`에 패키지와 Chromium을 설치하고, 실행 환경을 진단한 뒤 연결용 JSON을 출력합니다. 전역 Python 패키지와 클라이언트 설정 파일은 변경하지 않습니다. 설치 중 패키지·브라우저 다운로드는 발생하지만 LMS에는 접속하지 않습니다.
2. `login`에서 학번·비밀번호를 **자신의 터미널에만** 입력합니다. 비밀번호는 표시하거나 파일에 저장하지 않으며, 로그인 세션만 로컬에 저장합니다. SSO 화면을 확인하려면 `login --headed`를 사용하세요.
3. `config`가 출력한 JSON의 `ssu-lms` 항목을 핸들러의 MCP 설정에 추가하고 서버를 다시 연결합니다. **기존 서버 설정을 통째로 덮어쓰지 마세요.** 클라이언트별 형식은 아래 안내를 따릅니다.
4. 연결한 에이전트에 “`session_status`와 `storage_status`를 확인한 뒤 수강 과목을 보여줘”라고 요청합니다. 도구 15개가 보이고 `list_courses`가 수강 과목을 반환하면 LMS까지 연결된 것입니다.

로그인하지 않아도 서버 연결과 `storage_status`, `list_files`, `read_file`, `search_files`는 사용할 수 있습니다. 공지·과제 등 LMS 조회에는 유효한 세션이 필요합니다. 학번·비밀번호를 채팅이나 MCP 설정 JSON에 넣지 마세요.

### 설치 옵션과 업데이트

```powershell
# 개발 모드: 소스 변경 즉시 반영 + 테스트/빌드 도구
py -3.12 scripts/setup.py --dev

# 별도 세션·자료 폴더를 쓰는 경우
py -3.12 scripts/setup.py --data-dir "C:/lms-private" --materials-dir "D:/강의자료"
```

사용자 지정 경로를 쓰면 설치 마지막에 출력되는 `login`/`config` 명령을 그대로 사용하세요. 해당 옵션 없이 빠른 시작의 `login`을 실행하면 기본 저장 폴더로 로그인합니다.

`--venv <폴더>`로 설치할 가상환경을 변경할 수 있습니다. 기존 가상환경은 재사용하지만 일반 폴더를 지우거나 덮어써서 가상환경으로 만들지는 않습니다. `--skip-browser`는 이미 설치된 Chromium을 재사용할 때만 사용하세요. 생략해도 Chromium 설치 명령은 기존 설치를 재사용합니다.

소스를 업데이트한 뒤 같은 `setup.py` 명령을 다시 실행하면 패키지를 다시 설치합니다. 세션·자료·다운로드 이력은 삭제하지 않습니다. 설치 폴더나 저장 경로를 옮겼다면 `config`도 다시 생성해 클라이언트에 반영하세요.

자동 설치 대신 직접 설치하려면 `python -m venv .venv` → **가상환경 Python으로** `-m pip install .` → `-m playwright install chromium` 순서로 실행합니다. Linux에서 Chromium 시스템 라이브러리가 부족하면 관리자 권한 정책을 확인하고 가상환경 Python의 `-m playwright install-deps chromium`을 실행하세요. 설치 스크립트는 OS 패키지를 자동 설치하지 않습니다.

## 에이전트 연결: stdio

핸들러가 서버 프로세스를 직접 시작·종료하는 방식입니다. 처음 사용할 때 권장합니다.

### 현재 설치 경로로 설정 생성

```powershell
# Buzz 등 mcpServers JSON 형식
.\.venv\Scripts\python.exe -m ssu_lms_mcp config

# Codex config.toml 형식
.\.venv\Scripts\python.exe -m ssu_lms_mcp config --format codex

# VS Code .vscode/mcp.json 형식
.\.venv\Scripts\python.exe -m ssu_lms_mcp config --format vscode
```

macOS/Linux에서는 실행 파일만 `.venv/bin/python`으로 바꿉니다. **서버를 설치한 가상환경의 Python**으로 실행해야 그 환경의 경로가 생성됩니다.

| 클라이언트 | 형식 / 적용 방법 |
|---|---|
| Buzz 및 `mcpServers` 지원 핸들러 | 기본 `config` 출력 사용. JSON 입력이 없으면 `command`, `args`, `env`를 각각 입력 |
| Claude Desktop / Cursor | 기본 `config` 출력의 `mcpServers.ssu-lms` 항목을 해당 앱의 MCP 설정에 병합 |
| Codex CLI / IDE | `--format codex` 출력을 `~/.codex/config.toml` 또는 신뢰한 프로젝트의 `.codex/config.toml`에 병합. 재시작 후 `/mcp`로 확인 ([공식 안내](https://developers.openai.com/codex/mcp/)) |
| VS Code | `--format vscode` 출력을 `.vscode/mcp.json`에 병합. `MCP: List Servers`에서 `ssu-lms` 시작 ([공식 안내](https://code.visualstudio.com/docs/agent-customization/mcp-servers)) |

`config`는 설정을 **출력만** 합니다. 사용자·프로젝트 설정 파일을 자동 수정하지 않습니다. 이미 같은 이름의 서버가 있다면 해당 항목을 교체하고, 다른 서버 항목은 유지하세요.

생성된 설정에는 Python 실행 파일·세션 폴더·자료 폴더의 절대 경로와 필요한 동작 환경 변수만 들어갑니다. 학번·비밀번호·세션 내용은 포함하지 않습니다. `SSU_LMS_HEADLESS`, `SSU_LMS_SESSION_REFRESH_SECONDS`, 설정된 경우 `PLAYWRIGHT_BROWSERS_PATH`도 반영해 클라이언트의 작업 디렉터리와 환경이 달라도 같은 설정을 사용합니다. 설정 파일에 개인 경로가 있으므로 팀 공유 전 확인하세요.

별도 저장 위치는 로그인할 때와 동일하게 지정합니다:

```powershell
.\.venv\Scripts\python.exe -m ssu_lms_mcp config --data-dir "C:/lms-private" --materials-dir "D:/강의자료"
```

### 직접 입력하는 경우

| 항목 | 값 |
|---|---|
| 이름 | `ssu-lms` |
| 전송 방식 | `stdio` |
| 실행 명령 | 설치한 가상환경의 `python.exe` 또는 `bin/python` 절대 경로 |
| 인수 | `-m`, `ssu_lms_mcp`, `serve` (사용자 지정 저장 위치는 추가 옵션 필요) |

[examples/mcp-stdio.json](examples/mcp-stdio.json)은 경로를 교체해서 쓰는 최소 예제입니다. 수동 경로 편집 대신 `config` 출력을 권장합니다.

stdio 서버는 별도 터미널에서 미리 실행할 필요가 없습니다. 터미널에서 `serve`만 실행하면 MCP 입력을 기다리는 것이 정상입니다. 로그는 stderr, MCP 메시지만 stdout으로 보냅니다. 호스트가 도구 이름에 붙이는 접두사는 달라질 수 있습니다.

## 이 작업공간의 OMP 연결과 `/ssu-*`

OMP 프로젝트 설정은 `LMS-MCP`의 **상위 작업공간**에 등록되어 있습니다:

- `.omp/mcp.json`: 이 설치의 Python·세션·자료 경로를 사용하는 `ssu-lms` 및 별도 `onedrive` stdio 연결. 비밀번호·토큰은 넣지 않았으며 머신별 경로이므로 Git에서 제외합니다.
- `.omp/commands/ssu-*.md`: 아래 15개 도구 명령과 `/ssu-help`. OMP 기본 Markdown 프롬프트 명령으로, 에이전트가 실제 MCP 도구를 호출하고 결과를 정리합니다.

해당 상위 작업공간을 cwd로 OMP를 실행하세요. `LMS-MCP` 폴더만 다른 곳으로 복사하면 이 작업공간 전용 설정·명령은 따라가지 않습니다. 전역 OMP 설정은 변경하지 않았습니다.

이미 열려 있는 OMP에서는 다음 두 명령을 **차례로** 입력합니다. 새 세션에서는 자동으로 로드됩니다.

```text
/mcp reload
/reload-plugins
```

`/mcp list`로 연결을 확인하고 `/ssu-help`로 명령 목록을 볼 수 있습니다.

| 명령 | 기능 / 인수 |
|---|---|
| `/ssu-session` | 로컬 세션 상태 (현재 LMS 인증 유효성 검증 아님) |
| `/ssu-refresh` | 저장된 세션 확인·갱신 |
| `/ssu-courses` | 수강 과목과 ID. 선택적으로 과목명 필터 |
| `/ssu-notices [과목명 또는 ID]` | 전체 또는 과목별 공지 |
| `/ssu-assignments <과목명 또는 ID>` | 과제·마감일·제출 여부 |
| `/ssu-deadlines [조건]` | 전체 과제 마감일. 예: `이번 주 미제출` |
| `/ssu-materials <과목명 또는 ID>` | 주차별 자료 목록만 조회 |
| `/ssu-discussions <과목명 또는 ID>` | 일반 토론 조회 |
| `/ssu-board-posts <과목명 또는 ID>` | 게시판 글·게시판 ID·첨부 ID 조회 |
| `/ssu-download-material <과목명 또는 ID> <자료명 또는 ID>` | 원본 다운로드 후 OneDrive 저장. 선택 `allow_lti=false`, `folder="OneDrive 상대 폴더"` |
| `/ssu-download-attachment <course_id> <post_id> <attachment_id>` | 게시판 첨부 하나 다운로드. 선택 `board_id=ID` |
| `/ssu-storage` | 현재 자료 저장 위치 |
| `/ssu-files [course_id]` | 저장된 자료 목록 |
| `/ssu-read <path> [offset=0] [limit=16000]` | 저장된 문서 읽기 |
| `/ssu-search <검색어> [course_id=ID]` | 저장된 자료 본문 검색 |
| `/ssu-study [--key 자료키]` | 신규·개정 자료 수집과 학습 페이지 처리. 지정 키는 기준선도 분석 |
| `/ssu-study-review [복습키] [평가] [답안]` | 복습 문제 조회와 학습자가 선택한 이해도 기록 |
| `/ssu-study-exam --course-id ID 또는 --key 자료키` | 명시한 전체 범위의 비교표·약점·연습문제 |
| `/ssu-study-status` | 로컬 처리 상태·실패 단계·복습 일정 |

```text
/ssu-courses
/ssu-notices 운영체제
/ssu-deadlines 이번 주 미제출
/ssu-read "123/교안 1.pdf" limit=8000
/ssu-search 스케줄링
```

과목명·자료명은 실제 목록에서 ID를 확인합니다. 후보가 여러 개면 선택을 요청하며, ID를 추측하지 않습니다. 다운로드 명령은 기존 서버의 LTI 허용 설정을 유지하므로 출결·진도 기록이 발생할 수 있습니다. 이를 막으려면 `allow_lti=false`를 명시하세요. 게시판 조회도 열람 기록이 발생할 수 있습니다. 비밀번호나 세션 내용은 슬래시 명령 인수에 넣지 마세요.

### OneDrive 원본 저장

이 작업공간은 [Softeria의 Microsoft 365 MCP](https://github.com/Softeria/ms-365-mcp-server)를 **OneDrive 파일 기능만** 활성화해 사용합니다. Microsoft 공식 배포 서버는 아닙니다. 버전은 `.omp/onedrive/package.json`·`package-lock.json`에 고정되어 있으며, 전역 npm 설치·전역 OMP 설정은 변경하지 않습니다.

```text
OneDrive/Soongsil Univ/2026-2/<기존 과목명_교수명>/<원본 파일명>
```

- 기본 폴더·사용할 개인 드라이브 ID·과목별 폴더 매핑은 `.omp/onedrive/settings.json`에 저장합니다. 이 개인별 설정은 Git에서 제외합니다.
- 데이터사이언스, 데이터통신과네트워크, 운영체제, 알고리즘, 글로벌도시이해, 자율주행프로그래밍을 기존 과목 폴더에 연결했습니다. 미등록 과목은 폴더를 지정해야 하며, 임의로 새 폴더를 만들지 않습니다.
- 과목 폴더 최상위의 같은 이름 파일이 사용자가 필기한 사본이면, 필기 없는 LMS 원본은 `<과목 폴더>/LMS 원본/`에 같은 이름으로 보관합니다. 전송기는 필기본을 덮어쓰지 않으며 새 수정본과 이름이 겹치면 `onedrive_name_conflict`로 중단합니다.
- 원본 이름은 LMS 다운로드 매니페스트의 `filename`에서 가져옵니다. 로컬 캐시의 `주차_제목_ID.pdf`를 클라우드 파일명으로 사용하지 않습니다. PDF·PPTX 등 파일 내용·확장자는 변환하지 않습니다.
- `folder="..."`는 이번 호출에만 적용되는 **OneDrive 루트 기준 상대 경로**입니다. 해당 폴더는 이미 존재해야 합니다. 영구 변경은 `settings.json`의 `rootFolder`와 `courseFolders`를 수정합니다.
- 동일 이름·동일 SHA-256은 재업로드하지 않습니다. 내용이 다른 동명 파일은 덮어쓰거나 자동으로 이름을 바꾸지 않고 중단합니다. 업로드 후 클라우드 파일을 다시 읽어 SHA-256을 검증합니다.
- `.omp/onedrive/save-material.mjs`가 같은 OneDrive MCP 인증으로 업로드 세션을 생성하고 5MiB 단위로 원본을 전송합니다. 바이너리·base64·서명 URL을 에이전트 대화로 전달하지 않습니다. 개인 OneDrive가 요구하는 OData 속성 순서를 보존하기 위해 전송 프로세스 내부에서만 `graph-batch`를 사용하며, 일반 OMP 도구 목록에는 노출하지 않습니다.
- 로컬 원본은 그대로 보존합니다. 업로드만 실패했다면 아래 전송 명령만 다시 실행하면 됩니다. LTI를 다시 열 필요가 없습니다.
- 직접 `download_material` MCP를 호출하거나 `/ssu-download-attachment`를 실행하는 경우에는 기존 로컬 저장 동작을 유지합니다. 수동 명령 중 `/ssu-download-material`은 OneDrive 전송까지 수행합니다. 선택 기능인 학습 자동화도 같은 전송기와 기존 과목 폴더 규칙을 사용하며 별도의 클라우드 버전 폴더를 만들지 않습니다.

처음 설치한 환경의 인증·의존성 복구 명령은 작업공간 루트에서 실행합니다:

```powershell
npm ci --prefix .omp/onedrive
node .omp/onedrive/client.mjs --login
node .omp/onedrive/client.mjs --verify-login
```

Microsoft 로그인 화면에서 개인 OneDrive 계정으로 로그인합니다. `--verify-login`의 종료 코드만 보지 말고 JSON의 `success: true`를 확인하세요. 권한은 `Files.ReadWrite`이며 메일·일정 권한은 요청하지 않습니다. 이 OAuth 권한은 특정 폴더만의 권한이 아니라 로그인 계정 파일에 대한 권한입니다. 토큰 저장은 서버의 OS 자격 증명 저장소/암호화 캐시를 사용하며, 지정된 캐시 경로는 작업공간 밖입니다. 토큰·비밀번호·매니페스트 전체를 출력하거나 Git에 넣지 마세요.

사용 예:

```text
/ssu-download-material 데이터사이언스 L5-simitem-2 allow_lti=false
/ssu-download-material 48549 3390259 folder="Soongsil Univ/2026-2/데이터사이언스_정진홍"
```

이미 다운로드한 자료의 목적지 확인 또는 업로드만 재실행:

```powershell
node .omp/onedrive/save-material.mjs --course-id 48549 --material-id 3390259 --dry-run
node .omp/onedrive/save-material.mjs --course-id 48549 --material-id 3390259
```

`--dry-run`은 로컬 자료·원본 이름·설정 경로만 검사하며 로그인이나 클라우드 저장을 수행하지 않습니다. 실제 전송이 성공하면 `status`, `onedrivePath`, `size`, `sha256`, `driveId`, `itemId`를 반환합니다. 경로 이탈·다른 과목 자료·동명 충돌은 `node --test tests/ssu-onedrive.test.mjs`로 검사합니다.

### OneDrive 아티팩트 학습 자동화

이 선택 기능의 예약을 활성화하면 **Windows 작업 스케줄러에서 매일 07:30, Asia/Seoul**에 실행합니다. 현재 사용자로 로그인한 데스크톱이 필요합니다. PC를 깨우지 않으며, 예약 시각에 꺼져 있었다면 이후 실행 가능 시점·로그인 시 미완료 작업을 처리합니다. 같은 날 성공한 예약 실행은 다시 처리하지 않고, 실패한 단계는 다음 실행에서 이어갑니다.

- 주차 자료와 전체 게시판 첨부를 함께 검사합니다. LMS ID·제목이 같아도 원본 바이트의 SHA-256이 달라지면 새 수정본입니다. 동일 내용으로 되돌아간 경우 기존 원본·학습 페이지·복습 이력은 재사용하고 현재 버전만 전환합니다.
- OneDrive는 수동 다운로드와 같은 `Soongsil Univ/2026-2/<기존 과목명_교수명>/<원본 파일명>`에 저장합니다. `.omp/onedrive/settings.json`과 기존 전송기의 경로·중복 검증을 재사용하며 `자료버전`·해시·주차·날짜 폴더를 임의로 생성하거나 사용하지 않습니다. 원본 이름·확장자·바이트를 보존하고, 같은 이름·같은 내용은 기존 파일을 재사용합니다. 같은 이름·다른 내용은 덮어쓰기·자동 이름 변경·새 폴더 우회 없이 중단합니다. 해시 기반 버전 이력은 로컬에 유지합니다. 기존에 생성한 클라우드 폴더·파일은 별도 정리 요청 없이 이동·삭제하지 않습니다.
- 동명 충돌은 `onedrive_name_conflict`로 보고하며 브리핑에 기존 파일을 보존했다는 설명을 남깁니다. 전송 실패 후 LMS가 같은 바이트를 새 원본 이름으로 제공하면, 재시도에는 확인된 새 이름·경로를 사용하되 원래 버전 기록과 완료된 전송 이력은 바꾸지 않습니다.
- 원본 이름이 없거나 증명되지 않으면 `original_filename_missing`·`original_filename_unverified`로 중단합니다. Commons의 새 이력은 실제 `file_name`에서 확인한 `filename_verified`를 보존합니다. 구형 이력은 당시 생성 규칙으로 확인 가능한 이름만 재사용하며, 출처가 모호한 대체 이름은 추측하거나 LTI를 다시 열어 해결하지 않습니다. 이 차단은 통신 장애로 해제되지 않고 해당 바이트 수정본의 이름을 다시 확인해야 해제됩니다.
- 수동 다운로드가 동영상을 건너뛴 이력도 미디어 유형을 보존합니다. 이후 학습 자동화는 이를 `skipped/video`로 제외하며 LTI 뷰어를 다시 실행하지 않습니다.
- `init`은 목록 기준선과 비공개 아티팩트 허브만 만듭니다. 첫 `run`은 기존 파일을 보관하고 자료 컬렉션에 등록하되 일괄 AI 분석하지 않습니다. 기존 자료를 분석하려면 `--key`를 명시합니다. 이후 새 자료·개정본은 자동 분석 대상입니다.
- PDF는 모든 페이지의 텍스트와 렌더링 이미지를 함께 분석합니다. 그림·표·수식, 개념·직관·예시, 오해와 교정, 10분·심화 가이드, 정답 토글이 있는 회상 퀴즈, 과목에 맞는 풀이·계산·코드 추적·논증 실습을 페이지 근거와 함께 만듭니다. 빠른 가이드의 단계별 시간 합계는 정확히 10분이어야 하며 생성 결과를 검증합니다. 보충 설명과 AI 생성 문제는 구분합니다.
- 읽기 어려운 페이지는 검토 필요로 표시합니다. 손상·암호화·자원 한계·비 PDF 자료는 원본 보관과 검토 필요 상태를 유지하며, 불완전한 분석을 완성된 학습 페이지로 대체하지 않습니다. PDF 분석 한계는 100MiB·500페이지이며 다운로드 단계의 제한은 별도로 적용됩니다.
- 선수지식과 이미 분석한 같은 과목 자료를 연결합니다. 수정본 설명은 실제 이전 현재본과의 페이지 텍스트·이미지 비교에 근거합니다. 아직 분석하지 않은 기준선을 학습 완료로 간주하지 않습니다.
- 비공개 허브에 **학습 자료 / 복습 큐 / 학습 기록** 컬렉션과 검색·오늘 복습·취약 개념·시험·일일 브리핑 화면을 제공합니다. 자동 본문 변경은 새 스냅샷을 앞에 추가하며 기존 내용·개인 노트·직접 수정한 제목과 속성을 덮어쓰지 않습니다. 같은 스냅샷은 중복 추가하지 않고, 사용자가 삭제한 스냅샷도 재실행으로 복원하지 않습니다.
- 하루 브리핑은 날짜당 한 페이지, 데스크톱 알림 요청은 하루 한 번입니다. 알림 요청 수락과 실제 화면 표시는 다릅니다. 한 과목 실패가 다른 과목의 원본 보관·처리를 막지 않습니다.

설정은 Git에서 제외된 `.omp/study/settings.json`에 둡니다. `schema_version: 1`, 작업공간 밖의 `data_dir`, `lms.data_dir`·`lms.materials_dir`, `artifact: {"root":"C:/Users/picom/OneDrive/Soongsil Univ/학습 아티팩트","port":8766}`, 대상 `course_ids`, `cloud_command: [node 실행 파일, cloud.mjs]`, `model.command`·`model`·`thinking`·`timeout_seconds`·`pages_per_batch`, `schedule: {"time":"07:30","timezone":"Asia/Seoul"}`를 사용합니다. 실행 파일·스크립트·데이터 디렉터리는 실제 절대 경로여야 합니다. 아티팩트 폴더는 상태·LMS·원본 폴더와 겹치면 안 됩니다. SQLite와 복습 이력은 OneDrive 밖의 사용자 홈 `.ssu-study/study.sqlite3`에 두고 보존하세요.

작업공간 루트에서 의존성·인증을 복구할 때:

```powershell
npm ci --prefix .omp/study
# OneDrive 원본 전송 인증: node .omp/onedrive/client.mjs --login
# 기존 Notion 허브가 있으면 init 대신 아래 migrate-notion 절차부터 실행
$study = ".\LMS-MCP\.venv\Scripts\ssu-study.exe"
& $study --config .omp/study/settings.json init
& $study --config .omp/study/settings.json run
& $study --config .omp/study/settings.json status
& $study --config .omp/study/settings.json library --open
```

일상적인 `run`·`review`·`exam`은 Notion을 읽거나 수정하지 않습니다. 모델은 기존 OMP 인증으로 도구·확장·프로젝트 규칙을 끈 별도 프로세스에서 실행합니다. 기존 계정의 모델 사용량·서비스 한도가 적용되며 유료 플랜을 자동 신청하지 않습니다. 예약 실행은 대화형 로그인 창을 열지 않습니다.

#### 범용 AI 산출물 보관·링크

`Soongsil Univ/학습 아티팩트/열기.cmd` 또는 `library --open`을 실행하면 `http://127.0.0.1:8766/`의 파일 목록이 열립니다. AI 요약·정리 산출물은 `artifacts/<학기>/<과목 폴더와 같은 이름>/<파일명>`에 형식 그대로 저장합니다(예: `artifacts/2026-2/운영체제_공영호/강의 요약 (OS 01-04).html`). 목록은 폴더 경로별로 묶여 표시됩니다. Markdown 전용 구조나 변환은 없습니다. HTML은 스크립트를 차단하는 샌드박스 안에서 열리고, 나머지 형식은 원본 바이트 그대로 다운로드됩니다.

`학습 아티팩트` 최상위에는 `열기.cmd`, `library.json`, `documents/`, `assets/`, `artifacts/`만 있습니다. 목록·학습 화면의 HTML·JS·CSS·글꼴은 설치 패키지에서 직접 제공하며 OneDrive에 복사하지 않습니다. `열기.cmd`는 이 PC의 실행 경로를 담으므로 관리 기록은 `.ssu-study/library-launcher.json`에 둡니다. 자동화에서 계속 사용하는 기존 학습 허브의 JSON 문서·첨부는 `documents/`, `assets/`에 보존하며 [`study.html`](http://127.0.0.1:8766/study.html)에서만 엽니다. 예전 `artifact_url`의 페이지 링크도 `/study.html`로 연결되며, 공개 공유 링크를 만들지 않습니다.

로컬 파일 저장과 OneDrive 클라우드 동기화는 별개입니다. OneDrive 앱이 실행·로그인되어야 동기화되며 상태는 OneDrive에서 확인하세요. 저장소 서버는 현재 PC의 루프백에서만 열립니다. 실행 코드·목록 렌더러는 신뢰된 설치 패키지에서 제공하고, 산출물 HTML도 샌드박스에서만 표시합니다. 자격 증명과 SQLite는 저장소에 복사하지 않습니다.

#### 기존 SSU Notion 허브 이관

```powershell
& $study --config .omp/study/settings.json schedule --disable
node .omp/study/cloud.mjs --login  # 이관용 공식 Notion MCP OAuth
& $study --config .omp/study/settings.json migrate-notion
& $study --config .omp/study/settings.json library --open
& $study --config .omp/study/settings.json run
& $study --config .omp/study/settings.json schedule --enable
```

로컬 상태에 등록된 **SSU 허브와 그 하위 페이지·데이터베이스**만 가져옵니다. 다른 개인 페이지 링크를 따라 범위를 넓히지 않습니다. 기존 무필터 보기의 커서를 끝까지 읽어 활성·보관 항목을 확인하므로 SQL 조회 한도를 사용하지 않습니다. 본문·계층·속성·스키마·뷰·제공된 댓글·첨부를 보존하고 내부 페이지 링크를 로컬 주소로 바꿉니다. 읽기 권한·원문 완전성·실제 상위 관계를 증명할 수 없거나 이관 중 원문이 바뀌면 실패하며, Notion 원본은 수정·삭제하지 않습니다.

첨부는 상태 폴더에서 임시 다운로드한 후 검증된 저장소에 설치합니다. 기본 제한은 파일당 512MiB, 이관 전체 2GiB이며 `artifact.max_attachment_bytes`·`artifact.max_import_bytes`로 조정합니다. 초과 항목을 누락시켜 성공 처리하지 않습니다. SQLite의 검증된 백업을 남긴 후 파일 검증이 끝나야 게시 영수증·복습 이벤트를 한 트랜잭션으로 전환합니다. 학습자 답안은 링크처럼 보여도 원문 그대로 유지합니다. 부분 이관 중에는 일일 게시·복습 처리를 차단합니다. 동일 원문을 재이관하면 이후 개인 수정과 새 아티팩트 문서를 보존합니다. 전환 후에는 노션이 아니라 이 저장소에서 편집하세요.


기준선 학습, 정확한 시험 범위, 예약 작업 제어:

```powershell
& $study --config .omp/study/settings.json run --key material:48549:3390259
& $study --config .omp/study/settings.json exam --key material:48549:3390259 --title "L5 복습"
& $study --config .omp/study/settings.json schedule --disable
& $study --config .omp/study/settings.json schedule --enable
& $study --config .omp/study/settings.json schedule --status
```

`--key`·`--course-id`는 반복 지정할 수 있습니다. 시험 모드는 선택한 범위의 분석이 하나라도 빠지면 중단하며 몰래 범위를 줄이지 않습니다. 생성물은 출처 있는 비교표·약점·연습문제·해설이지 실제 시험 출제 예측이 아닙니다. `schedule --disable`은 이 설치가 소유한 비활성 작업을 먼저 등록하고, `--enable`이 활성화합니다. 이름만 같은 타 작업·관리자 권한 작업은 덮어쓰지 않습니다.

`schedule --status`는 실제 Windows 등록 상태를 읽습니다. 활성화 후 `enabled`, `timing.daily_enabled`, `timing.logon_catchup`이 모두 `true`인지 확인합니다. Windows가 XML에서 기본 `Enabled=true`를 생략해도 활성 상태로 처리하며, 명시적으로 꺼진 트리거나 없는 트리거는 비활성으로 보고합니다.

복습은 아티팩트 카드의 답안과 이해도를 직접 선택하고 **복습 제출**로 저장합니다. 다음 `run`이 제출을 반영합니다. CLI는 `review --key <복습키> --rating understood|uncertain|unknown --answer "내 답안"`을 사용하며, 복습키는 자료키와 다릅니다. 연속 이해함은 1·3·7·14·30일 간격, 애매함·모름은 1일 뒤 재학습으로 초기화합니다. 평가는 학습자가 선택한 값만 기록하고, 모름·애매함 카드를 우선합니다. `status`는 로컬 처리 상태 조회이며 대기 중인 아티팩트 제출을 즉시 처리하지 않습니다.

저장은 리비전 비교 후 원자적으로 수행합니다. 다른 탭이나 자동화가 먼저 저장했으면 충돌을 표시하고 작성 중인 초안을 유지합니다. 최신본과 비교·합친 뒤 저장하세요. OneDrive 충돌 사본·손상 문서도 조용히 덮어쓰거나 누락시키지 않습니다. 복습 제출의 중단·재전송은 이벤트와 리비전으로 구분하며, 확인 처리에서 답안 텍스트·개인 본문을 지우지 않습니다. `키`, `처리이벤트`, `SSU-AUTO` 경계는 자동화 관리용입니다.

LMS 세션 만료는 기존 로그인 명령으로 복구합니다. 영상은 재생하지 않고, 한 번 시도한 LTI를 자동으로 다시 열지 않습니다. 최초 허용된 LTI·게시판 접근에서는 LMS 열람·출결 기록이 발생할 수 있으며 이를 출석 증명으로 주장하지 않습니다. 과제를 제출하거나 학습자의 이해도를 대신 만들어 저장하지 않습니다.

## 에이전트 연결: HTTP / SSE

URL로 연결하는 핸들러에서는 먼저 별도 터미널에서 서버를 실행합니다:

```powershell
.\.venv\Scripts\ssu-lms-mcp.exe serve --transport streamable-http --port 8765
```

연결 URL: `http://127.0.0.1:8765/mcp`  
전송 방식: **Streamable HTTP**

[examples/mcp-http.json](examples/mcp-http.json)은 URL 설정 예제입니다. HTTP 프로세스는 연결하는 동안 계속 실행해야 합니다.

기존 SSE만 지원하는 핸들러에서는:

```powershell
.\.venv\Scripts\ssu-lms-mcp.exe serve --transport sse --port 8765
```

연결 URL은 `http://127.0.0.1:8765/sse`입니다. HTTP/SSE는 로컬 인터페이스만 허용하고 Host/Origin을 검사합니다. 다른 컴퓨터에서 연결할 때는 SSH 터널 등 접근 통제가 있는 연결을 사용하세요. 공용 인터넷에 공개하는 서버 구성은 포함하지 않습니다.

전송 구현은 [공식 MCP Python SDK 1.x](https://github.com/modelcontextprotocol/python-sdk/tree/v1.26.0)를 사용합니다.

## 제공 도구

| 도구 | 용도 / 주요 인수 |
|---|---|
| `session_status` | 로컬 세션 파일 상태 조회. 실제 로그인 유효성 검사는 하지 않음 |
| `refresh_session` | 저장된 세션을 Playwright로 확인·연장 |
| `list_courses` | 수강 과목 ID·이름·교수·자료 수 |
| `list_notices` | 공지 제목·본문. `course_id` 생략 시 전 과목 |
| `list_assignments` | 과제 본문·마감·배점·제출 여부. `course_id` 필수 |
| `list_deadlines` | 전 과목 과제를 마감일 순으로 조회 |
| `list_materials` | 주차별 자료 ID·유형·LMS 링크. `course_id` 필수 |
| `list_discussions` | 일반 토론 제목·본문. `course_id` 필수 |
| `list_board_posts` | LearningX 전체 게시판의 글·본문·게시판 ID·첨부 ID. `course_id` 필수 |
| `download_material` | 원본 파일 저장. `course_id`, `material_id`, 선택 `allow_lti` |
| `download_board_attachment` | 첨부 하나 저장. `course_id`, `post_id`, `attachment_id`, 선택 `board_id`. 기존 `filename` 방식도 지원 |
| `storage_status` | 현재 자료 저장 폴더의 절대 경로와 존재 여부 |
| `list_files` | 지정 폴더의 기존 파일·다운로드 자료의 상대·절대 경로. 선택 `course_id` |
| `read_file` | 자료 본문. `path`, 선택 `offset`, `limit` |
| `search_files` | 저장한 자료 본문 검색. `query`, 선택 `course_id` |

연결한 에이전트에 다음과 같이 요청할 수 있습니다:

- “수강 과목을 확인하고 이번 주 공지를 정리해줘.”
- “전체 과제 마감일을 가까운 순서로 알려줘.”
- “운영체제 강의자료 목록을 보여줘.”
- “저장된 자료에서 프로세스 스케줄링을 검색하고 근거 파일과 함께 설명해줘.”

공지·과제 본문은 기존 수집기의 8,000자 상한을 유지합니다. `list_deadlines`는 완료·과거 과제도 포함할 수 있으므로 에이전트가 `submitted`와 `due_at`을 확인해야 합니다. `progress`는 LMS에서 얻은 보조 정보이며 공식 출석 증빙이 아닙니다.

## 강의자료 다운로드와 읽기

1. `list_courses`에서 과목 ID를 얻습니다.
2. `list_materials(course_id=...)`에서 자료 ID와 유형을 확인합니다.
3. `download_material(course_id=..., material_id=...)`로 원본을 저장합니다.
4. 반환된 `path`를 `read_file(path=...)`에 넘깁니다. `next_offset`이 있으면 그 값으로 이어서 읽습니다.

교안은 주차학습과 게시판 양쪽에 등록될 수 있으므로 `list_materials`와 `list_board_posts`를 함께 확인합니다.
게시판 교안은 `list_board_posts(course_id=...)`의 게시글 `id`, `board_id`, 첨부의 `id`로
`download_board_attachment(course_id=..., post_id=..., board_id=..., attachment_id=...)`를 호출합니다.
같은 이름·확장자의 첨부도 ID별로 저장하며, 재호출하면 기존 파일을 재사용합니다.
기존 `filename=...`도 사용 가능하지만 같은 이름이 여러 개면 `attachment_id`로 지정해야 합니다.
게시판 첨부도 저장 후 `read_file`과 `search_files`에서 동일하게 읽고 검색할 수 있습니다.

LearningX 게시판은 과목의 게시판 탭으로 인증한 뒤 전용 API의 페이지를 순회합니다.
게시판마다 최대 100페이지, 과목당 최대 1,000글을 조회하며 상한을 넘으면 오류로 알립니다.
게시판 조회·첨부 다운로드는 LMS 열람·조회수 기록이 발생할 수 있습니다.

이 개인용 구성은 자료를 열 때 발생하는 LMS 출결·진도 기록을 사용자가 허용한 설정입니다.
`download_material`은 기본 `allow_lti=true`로 새 `ExternalTool` 자료도 추가 확인 없이 엽니다.
특정 호출에서 열기를 막으려면 `allow_lti=false`를 지정합니다. 이미 저장한 자료는 재사용합니다.
게시판 파일은 원본 다운로드 주소로 내려받으며, 정상적인 인라인 PDF도 저장합니다.

LTI 시도 이력은 `material_manifest.json`에 기록합니다. 프로세스 중단이나 다운로드 실패 후에도 같은 자료를 자동으로 재실행하지 않습니다. 이 파일을 지우면 해당 기록도 사라지므로 유지하세요. 영상·외부 링크와 80MB 초과 파일은 건너뜁니다.

PDF, PPTX, DOCX, Jupyter Notebook, TXT, Markdown 본문을 읽습니다. 이미지 OCR 및 구형 HWP/PPT/DOC는 지원하지 않습니다. 본문 추출은 파일당 20MiB·100만 자·PDF 500쪽까지이며, PPTX/DOCX의 압축 해제 크기도 20MiB 상한을 적용합니다. 한 번에 최대 16,000자를 반환합니다. 검색은 최대 50개 파일의 앞부분 16,000자씩을 대상으로 하므로 결과의 제한 안내를 확인하세요. `read_file`은 지정한 자료 폴더 내부의 상대·절대 경로를 허용하며, 폴더 밖 경로는 거부합니다.

## 원하는 폴더에서 자료 관리

자료만 외장 디스크·동기화 폴더·마운트된 네트워크 드라이브 등에 저장하려면 서버 인수에 `--materials-dir`을 추가하세요. 서버 프로세스가 읽고 쓸 수 있는 **폴더 경로**를 사용합니다. 클라우드 URL 자체는 지원하지 않습니다.

```powershell
ssu-lms-mcp serve --materials-dir "D:/강의자료"
```

stdio 핸들러에서는 기존 `command`를 유지하고 `args`를 다음처럼 설정합니다:

```json
"args": ["-m", "ssu_lms_mcp", "serve", "--materials-dir", "D:/강의자료"]
```

또는 MCP 서버 환경 변수 `SSU_LMS_MATERIALS_DIR`에 같은 경로를 지정하세요. 우선순위는 **CLI 옵션 → 환경 변수 → `<data-dir>/materials`**입니다. 절대 경로를 권장하며, 설정을 바꾼 뒤 MCP 서버를 재시작해야 적용됩니다. `storage_status`로 실제 적용 경로를 확인할 수 있습니다.

다운로드는 지정 폴더 아래 `<과목 ID>/`에 저장됩니다. `list_files`·`read_file`·`search_files`도 같은 폴더를 사용하며, 이미 있던 문서와 이름으로 만든 하위 폴더도 포함합니다. 숨김 파일·숨김 폴더와 다운로드 중인 `.part` 파일은 목록에서 제외합니다. `course_id` 필터를 지정하면 해당 숫자 이름의 하위 폴더만 조회합니다.

로그인 세션과 다운로드·LTI 이력은 기존 비공개 `data-dir`에 남습니다. 이전 자료를 자동으로 옮기거나 삭제하지 않으므로 필요한 파일은 직접 복사하세요. 경로를 변경한 뒤 다운로드를 요청하면 새 위치를 확인하며, 저장 위치 변경만으로 이미 시도한 LTI를 다시 열지는 않습니다. 새 위치의 같은 이름 파일이 다운로드 기록으로 확인되지 않으면 덮어쓰지 않고 충돌을 알립니다. 게시판 첨부는 고유 ID 기반 경로에 이미 있는 파일을 재사용합니다.

## 세션과 저장 위치

기본 저장 폴더는 운영체제 사용자 홈 아래 `.ssu-lms-mcp`입니다. 설치 위치나 핸들러 작업 디렉터리를 바꿔도 같습니다.

```text
~/.ssu-lms-mcp/
  session_state.json       # 인증 쿠키·스토리지 (공유 금지)
  material_manifest.json  # 다운로드·LTI 실행 이력
  materials/
    12345/                # 과목 ID별 자료
```

별도 계정이나 저장 폴더를 사용하려면 로그인과 서버 실행에 **같은** `--data-dir`을 지정합니다:

```powershell
ssu-lms-mcp login --data-dir C:/lms-private
ssu-lms-mcp serve --data-dir C:/lms-private
```

또는 핸들러의 MCP 환경 변수에 `SSU_LMS_DATA_DIR`을 설정하세요. 경로는 절대 경로를 권장합니다.

| 환경 변수 | 기본값 | 의미 |
|---|---|---|
| `SSU_LMS_DATA_DIR` | `~/.ssu-lms-mcp` | 세션·다운로드 이력 저장 폴더 |
| `SSU_LMS_MATERIALS_DIR` | `<data-dir>/materials` | 다운로드·목록·읽기·검색에 사용할 자료 폴더 |
| `SSU_LMS_HEADLESS` | `true` | Playwright 창 숨김 |
| `SSU_LMS_SESSION_REFRESH_SECONDS` | `5400` | 도구 호출 시 세션 갱신이 필요한 경과 시간 |
| `SSU_LMS_USERNAME`, `SSU_LMS_PASSWORD` | 없음 | 명시적 `login` 명령에서만 사용하는 선택적 입력 |

서버 시작이나 도구 호출에서 학번·비밀번호를 자동으로 입력받지 않습니다. 저장된 세션이 오래되면 다음 LMS 도구 호출 때 확인·연장합니다. 서버를 사용하지 않는 동안 예약 작업이나 크롤링은 실행하지 않습니다. 세션이 만료되면 CLI에서 다시 로그인하세요.

```powershell
ssu-lms-mcp status
ssu-lms-mcp refresh
ssu-lms-mcp login
```

`status`의 `authenticated`는 로컬 캐시 상태입니다. 현재 LMS 접속 가능 여부는 `refresh` 또는 실제 조회로 확인합니다. 세션 파일에는 인증 정보가 들어 있으므로 Git, 채팅, 공유 폴더에 올리지 마세요. 기존 웹앱의 `.env`나 세션 파일은 자동으로 가져오지 않습니다.

## 환경 진단과 문제 해결

```powershell
.\.venv\Scripts\python.exe -m ssu_lms_mcp doctor
.\.venv\Scripts\python.exe -m ssu_lms_mcp doctor --json
```

`doctor`는 저장 폴더(없으면 기존 상위 폴더)에 임시 파일을 만들었다가 제거해 쓰기를 확인하고, headless Chromium으로 `about:blank`를 열고 종료합니다. 로컬 세션 상태도 확인하지만 **LMS에 접속하거나 로그인·세션 갱신을 수행하지 않습니다.** 자료 폴더·세션 파일을 새로 만들지 않습니다.

- `OK`: 해당 환경 점검 통과. 세션의 `OK`도 **로컬 캐시 확인**이지 현재 LMS 인증 성공을 뜻하지 않습니다.
- `WARNING`: 로그인 세션이 없거나 갱신이 필요합니다. 안내된 `login` 또는 `refresh` 명령을 실행하세요.
- `ERROR`: 폴더 권한·Chromium 실행 문제입니다. 해당 문제를 해결한 뒤 다시 진단하세요.
- 종료 코드 `0`: 로컬 실행 환경 점검 통과 (세션 경고가 있어도 `0`). `1`: 환경 점검 또는 설정 오류. 예기치 않은 실행 오류는 `2`, 사용자 중단은 `130`입니다. `--json`의 `ok`와 `checks`, `session`, `next_steps`로 구분할 수 있습니다.

| 증상 | 조치 |
|---|---|
| `No module named ssu_lms_mcp` | 설치한 가상환경 Python인지 확인하고 `scripts/setup.py` 재실행 |
| Chromium 실행 실패 | 같은 가상환경 Python으로 `-m playwright install chromium`; Linux에서는 OS 라이브러리도 확인 |
| 도구는 보이지만 세션 오류 | `login` 실행. 사용자 지정 `--data-dir`이 서버와 같은지 확인 |
| `status`는 인증됨인데 조회 실패 | `status`는 캐시 확인입니다. `refresh` 후 실패하면 다시 `login` |
| 로그인 입력을 요구하는데 MCP 클라이언트에서 멈춤 | 클라이언트 실행 인수는 `login`이 아니라 `serve`여야 함. 로그인은 별도 터미널에서 실행 |
| JSON/TOML 설정 후 서버가 안 뜸 | 사용한 클라이언트 형식과 가상환경 경로를 확인하고 `config` 재생성. 서버 재시작 |
| HTTP 연결 거부 | 별도 `serve --transport streamable-http` 프로세스 실행 여부와 `/mcp` URL 확인 |
| 포트 사용 중 | `--port 8766` 등 빈 포트로 실행하고 클라이언트 URL도 동일하게 변경 |
| 자료를 찾지 못함 | `storage_status`로 실제 `materials_dir` 확인. 폴더 변경 후 서버 재시작 |

## 개발 및 검증

`scripts/setup.py --dev`로 개발 환경을 설치한 뒤 `LMS-MCP` 폴더에서 실행합니다:

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check src tests scripts/setup.py
.\.venv\Scripts\python.exe -m build
```

테스트는 외부 네트워크를 차단하고 임시 데이터 폴더를 사용합니다. stdio, Streamable HTTP, SSE 프로세스에 실제 MCP 클라이언트로 연결해 초기화·도구 목록·호출·오류를 확인합니다. `config`가 생성한 설정으로 다른 작업 디렉터리에서 연결해 자료 읽기와 로그인 정보 미포함을 확인하며, `doctor`의 환경 오류와 기존 파일 보존도 검사합니다. 실제 학번·비밀번호를 사용하는 SSO와 운영 LMS 응답은 자동 테스트에서 검증하지 않습니다.

전체 테스트에는 별도의 기존 예약 큐레이션 스크립트 검증도 포함됩니다. 이 테스트는 임시 스킬 입력을 사용해 설치 환경과 독립적으로 실패 보존·정확한 배치 승인·오래된 영수증 거부를 검사합니다. 기존 스크립트의 실제 실행에 필요한 `.agents/skills/ssu-lms-curate/SKILL.md` 원본은 여전히 유실된 상태입니다([기존 복구 기록](RECOVERY.md)). 테스트 통과가 그 스킬의 복구를 뜻하지는 않습니다. 독립 MCP와 위의 `ssu-study` 학습 자동화는 해당 스킬을 사용하지 않습니다.

기존 소스의 출처: `backend/app/adapter/`, `backend/app/services/material_service.py`, `backend/app/mcp_client/materials_server.py`. 필요한 구현을 독립 패키지 안으로 옮겼으며, 설치 후에는 이 경로를 참조하지 않습니다.
