# SSU LMS MCP

숭실대학교 Smart Campus의 공지·과제·강의자료를 Buzz 등 에이전트 핸들러에서 조회하는 독립 MCP 서버입니다. 이 폴더만 복사해서 설치할 수 있습니다. Python 3.11 이상이 필요합니다.

서버가 LMS 데이터를 수집하고, 연결한 에이전트가 내용을 요약하거나 질문에 답합니다. 프런트엔드, FastAPI 웹앱, LLM API 키, Notion, Obsidian 설정은 필요하지 않습니다.

## 설치

독립 폴더로 옮겨 사용할 때:

```powershell
cd standalone-mcp
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\ssu-lms-mcp.exe login
```

학번과 비밀번호를 터미널에서 입력합니다. 비밀번호는 화면에 표시하거나 파일에 저장하지 않습니다. 로그인한 세션만 로컬에 저장합니다. SSO 화면을 확인해야 한다면 `login --headed`를 사용하세요.

Linux/macOS:

```bash
cd standalone-mcp
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/python -m playwright install chromium
.venv/bin/ssu-lms-mcp login
```

이 저장소 안에서 개발한다면 프로젝트 가상환경에 편집 가능 모드로 설치합니다:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\ssu-lms-mcp.exe login
```

## 에이전트 연결: stdio

핸들러가 서버 프로세스를 직접 실행하는 방식입니다. MCP 추가 화면에 다음 값을 넣으세요.

| 항목 | 값 |
|---|---|
| 이름 | `ssu-lms` |
| 전송 방식 | `stdio` |
| 실행 명령 | 설치한 가상환경의 `python.exe` 절대 경로 |
| 인수 | `-m`, `ssu_lms_mcp`, `serve` |

`mcpServers` 형식을 지원하는 핸들러의 예제입니다. 아래 실행 경로를 자신의 설치 경로로 바꾸세요. [examples/mcp-stdio.json](examples/mcp-stdio.json)에도 같은 예제가 있습니다.

```json
{
  "mcpServers": {
    "ssu-lms": {
      "command": "C:/tools/ssu-lms-mcp/.venv/Scripts/python.exe",
      "args": ["-m", "ssu_lms_mcp", "serve"]
    }
  }
}
```

이 저장소의 가상환경을 사용했다면 `command`는 `C:/Users/picom/orca/LMS-MCP/.venv/Scripts/python.exe`입니다. Unix 계열에서는 설치 폴더의 `.venv/bin/python` 절대 경로를 지정합니다. 실행 작업 디렉터리와 무관하게 동작합니다.

Buzz에서 JSON 입력 대신 명령/인수 입력란을 제공한다면 위 값을 각각 입력하세요. 호스트별 설정 파일 형식은 다를 수 있습니다. 서버 이름이나 호스트가 붙이는 도구 접두사와 무관하게 실제 도구 이름은 아래 표를 따릅니다.

stdio 서버는 핸들러가 시작·종료합니다. 터미널에서 `serve`만 실행하면 MCP 입력을 기다리는 것이 정상입니다. 로그는 stderr, MCP 메시지만 stdout으로 보냅니다.

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

## 개발 및 검증

이 저장소의 가상환경에 `.[dev]`를 설치한 뒤 이 폴더에서 실행합니다:

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check src tests
.\.venv\Scripts\python.exe -m build
```

테스트는 외부 네트워크를 차단하고 임시 데이터 폴더를 사용합니다. stdio, Streamable HTTP, SSE 프로세스에 실제 MCP 클라이언트로 연결해 초기화·도구 목록·호출·오류를 확인합니다. 실제 학번·비밀번호를 사용하는 SSO와 운영 LMS 응답은 자동 테스트에서 검증하지 않습니다.

기존 소스의 출처: `backend/app/adapter/`, `backend/app/services/material_service.py`, `backend/app/mcp_client/materials_server.py`. 필요한 구현을 독립 패키지 안으로 옮겼으며, 설치 후에는 이 경로를 참조하지 않습니다.
