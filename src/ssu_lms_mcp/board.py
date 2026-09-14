"""LearningX 게시판 인증과 게시글 API. 열람 시 LMS 조회수가 기록될 수 있다."""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from urllib.parse import urljoin, urlparse

from .adapters.notices import html_to_text
from .models import BoardPost, NoticeAttachment
from .session import SessionRequiredError
from .urls import FILE_HOSTS, valid_url

CANVAS = "https://canvas.ssu.ac.kr"
MAX_PAGES = 100
MAX_POSTS = 1000
BOARD_LABELS = {"게시판", "board", "boards", "learningx board"}


def _positive_id(value) -> int:
    if isinstance(value, bool) or not str(value).isdigit() or int(value) <= 0:
        raise ValueError("게시판 ID는 양의 정수여야 합니다.")
    return int(value)


def _board_tab(tabs: list, course_id: int) -> str | None:
    """게시판으로 명시된 현재 과목의 탭만 연다. 다른 외부 도구로 대체하지 않는다."""
    for tab in tabs:
        if tab.get("type") != "external" or str(tab.get("label", "")).strip().lower() not in BOARD_LABELS:
            continue
        url = urljoin(CANVAS, tab.get("full_url") or tab.get("html_url") or "")
        if not valid_url(url, {"canvas.ssu.ac.kr"}):
            continue
        parsed = urlparse(url)
        if (
            re.fullmatch(rf"/courses/{course_id}/external_tools/[1-9]\d*", parsed.path)
            and not parsed.query and not parsed.fragment
        ):
            return url
    return None


class _BoardAPI:
    def __init__(self, request, course_id: int, authorization: str):
        self.request = request
        self.base = f"{CANVAS}/learningx/api/v1/learningx_board/courses/{course_id}/boards"
        self.authorization = authorization

    async def get(self, suffix: str = "", params: dict | None = None):
        # API JSON이 제공하는 URL이나 pagination URL을 따라가지 않는다.
        if not re.fullmatch(r"(?:/[1-9]\d*(?:/posts(?:/[1-9]\d*)?)?)?", suffix):
            raise ValueError("허용되지 않은 게시판 API 경로입니다.")
        response = await self.request.get(
            self.base + suffix, params=params,
            headers={"Authorization": self.authorization}, max_redirects=0, timeout=30_000,
        )
        try:
            if response.status in {401, 403, 419}:
                raise SessionRequiredError()
            if response.status != 200:
                raise RuntimeError(f"게시판 요청 실패 (HTTP {response.status}).")
            try:
                return await response.json()
            except ValueError:
                raise RuntimeError("게시판 API가 올바른 JSON을 반환하지 않았습니다.") from None
        finally:
            await response.dispose()


async def _permit_board_request(route):
    allowed = valid_url(route.request.url, FILE_HOSTS | {"smartid.ssu.ac.kr"})
    await (route.continue_() if allowed else route.abort())


@asynccontextmanager
async def _board_session(client, settings, course_id: int):
    from playwright.async_api import TimeoutError as PlaywrightTimeout

    from .downloads import _browser_context

    course_id = _positive_id(course_id)
    tabs = await client.get_all_pages(f"/courses/{course_id}/tabs", use_canvas=True)
    tab = _board_tab(tabs, course_id)
    if tab is None:
        yield None
        return
    async with _browser_context(settings) as context:
        await context.route("**/*", _permit_board_request)
        page = await context.new_page()
        api_url = f"{CANVAS}/learningx/api/v1/learningx_board/courses/{course_id}/boards"
        try:
            # Board의 토큰은 LMS 토큰과 다르다. 실제 게시판의 인증된 GET
            # 요청에서만 가져와 현재 컨텍스트에서 사용하고 저장하지 않는다.
            try:
                async with page.expect_request(
                    lambda r: r.method == "GET" and r.url == api_url, timeout=30_000
                ) as request_info:
                    await page.goto(tab, wait_until="networkidle", timeout=40_000)
                request = await request_info.value
            except PlaywrightTimeout:
                if "login" in page.url.lower() or urlparse(page.url).hostname == "smartid.ssu.ac.kr":
                    raise SessionRequiredError() from None
                raise RuntimeError("게시판 인증 요청을 확인하지 못했습니다. 세션을 갱신하세요.") from None
            authorization = (await request.all_headers()).get("authorization", "")
            if not authorization.startswith("Bearer "):
                raise SessionRequiredError()
            yield _BoardAPI(context.request, course_id, authorization)
        finally:
            await page.close()


def _boards(raw) -> list[dict]:
    if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
        raise ValueError("게시판 목록 응답 형식이 올바르지 않습니다.")
    for item in raw:
        _positive_id(item.get("id"))
    return raw


async def _post_summaries(api, board_id: int):
    seen: set[int] = set()
    for page in range(1, MAX_PAGES + 1):
        raw = await api.get(f"/{board_id}/posts", {"page": page})
        if not isinstance(raw, dict) or not isinstance(raw.get("items"), list):
            raise ValueError("게시글 목록 응답 형식이 올바르지 않습니다.")
        pagination = raw.get("pagination") or {}
        last = _positive_id(pagination.get("last_page", 1))
        if _positive_id(pagination.get("current_page", page)) != page:
            raise ValueError("게시글 페이지가 반복되어 조회를 중단했습니다.")
        for row in raw["items"]:
            post_id = _positive_id(row.get("id"))
            if post_id not in seen:
                seen.add(post_id)
                yield row
        if page >= last:
            return
    raise ValueError("게시글 조회 페이지 상한(100)을 초과했습니다.")


def _post(raw: dict, course_id: int, board: dict) -> BoardPost:
    post_id, board_id = _positive_id(raw.get("id")), _positive_id(board.get("id"))
    attachments = []
    for item in raw.get("attachments") or []:
        url = item.get("url")
        attachments.append(NoticeAttachment(
            id=str(_positive_id(item.get("id"))),
            name=str(item.get("filename") or "첨부파일"),
            url=url if isinstance(url, str) and valid_url(url) else None,
            size=item.get("filesize"), content_type=item.get("content_type"),
        ))
    return BoardPost(
        id=post_id, course_id=course_id, board_id=board_id, board_name=board.get("title", ""),
        title=raw.get("title", ""), message_text=html_to_text(raw.get("content") or ""),
        posted_at=raw.get("created_at"), author=raw.get("user_name", ""),
        html_url=f"{CANVAS}/learningx/lti/learningx_board/boards/{board_id}/posts/{post_id}",
        attachments=attachments,
    )


async def list_board_posts(client, settings, course_id: int) -> list[BoardPost]:
    """전체 게시판의 페이지를 순회한다. 첨부가 있는 글만 상세 API로 보충한다."""
    async with _board_session(client, settings, course_id) as api:
        if api is None:
            return []
        posts = []
        for board in _boards(await api.get()):
            board_id = _positive_id(board["id"])
            async for row in _post_summaries(api, board_id):
                if len(posts) >= MAX_POSTS:
                    raise ValueError("게시글 조회 상한(1000)을 초과했습니다.")
                if row.get("attachment_count"):
                    row = await api.get(f"/{board_id}/posts/{_positive_id(row['id'])}")
                posts.append(_post(row, course_id, board))
        return posts


async def get_board_post(client, settings, course_id: int, post_id: int, *, board_id: int | None = None) -> BoardPost:
    """선택한 게시글의 최신 첨부 URL을 얻는다. 과목의 게시판 소속을 확인한다."""
    post_id = _positive_id(post_id)
    if board_id is not None:
        board_id = _positive_id(board_id)
    async with _board_session(client, settings, course_id) as api:
        if api is not None:
            boards = _boards(await api.get())
            for board in boards:
                bid = _positive_id(board["id"])
                if board_id is not None:
                    if bid != board_id:
                        continue
                else:
                    found = False
                    async for row in _post_summaries(api, bid):
                        if _positive_id(row["id"]) == post_id:
                            found = True
                            break
                    if not found:
                        continue
                raw = await api.get(f"/{bid}/posts/{post_id}")
                if _positive_id(raw.get("id")) != post_id:
                    raise ValueError("게시글 응답 ID가 일치하지 않습니다.")
                return _post(raw, course_id, board)
    raise ValueError("해당 과목에서 게시글을 찾을 수 없습니다.")
