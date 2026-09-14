from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ssu_lms_mcp import board
from ssu_lms_mcp.adapters.notices import _notice
from ssu_lms_mcp.models import NoticeAttachment
from ssu_lms_mcp.session import SessionRequiredError
from ssu_lms_mcp.urls import valid_url


def test_notice_preserves_api_attachments():
    notice = _notice(
        {
            "id": 7,
            "title": "교안",
            "message": "<p>확인</p>",
            "attachments": [{"id": 11, "filename": "lecture.pdf", "url": "https://canvas.ssu.ac.kr/files/11"}],
        },
        12,
    )
    assert notice.attachments == [
        NoticeAttachment(id="11", name="lecture.pdf", url="https://canvas.ssu.ac.kr/files/11")
    ]


def test_board_tab_requires_label_host_and_course():
    unrelated = {"type": "external", "label": "주차학습", "full_url": "https://canvas.ssu.ac.kr/courses/12/external_tools/71"}
    assert board._board_tab([unrelated], 12) is None
    valid = {"type": "external", "label": "게시판", "full_url": "https://canvas.ssu.ac.kr/courses/12/external_tools/41"}
    assert board._board_tab([unrelated, valid], 12) == valid["full_url"]
    assert board._board_tab([{**valid, "full_url": "https://evil.example/courses/12/external_tools/41"}], 12) is None
    assert board._board_tab([valid], 13) is None
    assert board._board_tab([{**valid, "full_url": valid["full_url"] + "?redirect=https://evil.example"}], 12) is None


@pytest.mark.parametrize("url", [
    "https://evil.example/learningx/lti/learningx_board/boards/1/posts/2",
    "http://canvas.ssu.ac.kr/file", "https://user:password@canvas.ssu.ac.kr/file",
    "https://canvas.ssu.ac.kr:444/file", "https://canvas.ssu.ac.kr:bad/file",
    "https://canvas.ssu.ac.kr.evil.example/file", "https://canvas.ssu.ac.kr\\@evil.example/",
    "https://canvas.ssu.ac.kr/\nfile", "https://[broken/file",
])
async def test_untrusted_browser_requests_aborted(url):
    assert not valid_url(url)
    route = SimpleNamespace(request=SimpleNamespace(url=url), continue_=AsyncMock(), abort=AsyncMock())
    await board._permit_board_request(route)
    route.abort.assert_awaited_once()
    route.continue_.assert_not_awaited()


async def test_missing_board_does_not_launch_browser(monkeypatch):
    client = SimpleNamespace(get_all_pages=AsyncMock(return_value=[]))
    from ssu_lms_mcp import downloads
    launch = AsyncMock(side_effect=AssertionError("must not launch"))
    monkeypatch.setattr(downloads, "_browser_context", launch)
    assert await board.list_board_posts(client, object(), 12) == []
    launch.assert_not_called()


def _fake_session(monkeypatch, get):
    @asynccontextmanager
    async def session(client, settings, course_id):
        assert course_id == 12
        yield SimpleNamespace(get=get)
    monkeypatch.setattr(board, "_board_session", session)


async def test_real_shaped_boards_pagination_and_details(monkeypatch):
    calls = []

    async def get(suffix="", params=None):
        calls.append((suffix, params))
        if not suffix:
            return [{"id": 1, "title": "Q&A"}, {"id": 2, "title": "Lecture slides"}]
        if suffix == "/1/posts":
            return {"items": [], "pagination": {"current_page": 1, "last_page": 1}}
        if suffix == "/2/posts":
            page = params["page"]
            return {"items": [{"id": page + 10, "title": "제목", "content": "<p>본문</p>", "attachment_count": 1 if page == 2 else 0}], "pagination": {"current_page": page, "last_page": 2}}
        assert suffix == "/2/posts/12"
        return {"id": 12, "title": "교안", "content": "<p>강의교안입니다</p>", "user_name": "교수", "attachments": [
            {"id": 91, "filename": "lecture.pdf", "filesize": 123, "url": "https://canvas.ssu.ac.kr/files/5/download"},
            {"id": 92, "filename": "lecture.pdf", "filesize": 456, "url": "https://canvas.ssu.ac.kr/files/6/download"},
            {"id": 93, "filename": "external.pdf", "url": "https://evil.example/file"},
        ]}

    _fake_session(monkeypatch, get)
    result = await board.list_board_posts(None, None, 12)
    assert [p.id for p in result] == [11, 12]
    assert result[1].board_id == 2 and result[1].board_name == "Lecture slides"
    assert result[1].message_text == "강의교안입니다"
    assert [a.id for a in result[1].attachments] == ["91", "92", "93"]
    assert result[1].attachments[0].size == 123 and result[1].attachments[2].url is None
    assert ("/2/posts/11", None) not in calls


async def test_selected_post_only_and_board_membership(monkeypatch):
    get = AsyncMock(side_effect=[[{"id": 2, "title": "Slides"}], {"id": 12, "title": "교안", "attachments": []}])
    _fake_session(monkeypatch, get)
    result = await board.get_board_post(None, None, 12, 12, board_id=2)
    assert result.id == 12
    assert [c.args for c in get.await_args_list] == [(), ("/2/posts/12",)]
    get.reset_mock(side_effect=True)
    get.return_value = [{"id": 2}]
    with pytest.raises(ValueError, match="찾을 수"):
        await board.get_board_post(None, None, 12, 12, board_id=99)
    assert get.await_count == 1


async def test_filename_legacy_post_lookup_scans_summaries_only(monkeypatch):
    get = AsyncMock(side_effect=[
        [{"id": 2, "title": "Slides"}],
        {"items": [{"id": 11}, {"id": 12}]},
        {"id": 12, "attachments": []},
    ])
    _fake_session(monkeypatch, get)
    assert (await board.get_board_post(None, None, 12, 12)).id == 12
    assert get.await_args_list[-1].args == ("/2/posts/12",)


async def test_repeated_pagination_fails_loudly():
    get = AsyncMock(return_value={"items": [{"id": 1}], "pagination": {"current_page": 1, "last_page": 2}})
    with pytest.raises(ValueError, match="반복"):
        _ = [row async for row in board._post_summaries(SimpleNamespace(get=get), 2)]


@pytest.mark.parametrize("status", [302, 401, 403, 419, 500])
async def test_board_api_never_follows_redirect_and_disposes_response(status):
    response = SimpleNamespace(status=status, dispose=AsyncMock())
    request = SimpleNamespace(get=AsyncMock(return_value=response))
    api = board._BoardAPI(request, 12, "Bearer test-only")
    error = SessionRequiredError if status in {401, 403, 419} else RuntimeError
    with pytest.raises(error):
        await api.get("/2/posts/12")
    assert request.get.await_args.kwargs["max_redirects"] == 0
    assert request.get.await_args.args[0] == "https://canvas.ssu.ac.kr/learningx/api/v1/learningx_board/courses/12/boards/2/posts/12"
    response.dispose.assert_awaited_once()


async def test_board_api_rejects_absolute_paths_before_request():
    request = SimpleNamespace(get=AsyncMock())
    api = board._BoardAPI(request, 12, "Bearer test-only")
    for suffix in ["https://evil.example/posts/1", "/../secret", "/2/posts/1?redirect=evil", "/2/posts/1/mark_read"]:
        with pytest.raises(ValueError):
            await api.get(suffix)
    request.get.assert_not_awaited()
