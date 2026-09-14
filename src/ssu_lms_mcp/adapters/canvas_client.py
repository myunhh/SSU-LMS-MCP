"""LearningX와 Canvas 인증을 분리한 읽기 전용 HTTP 클라이언트."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx


class CanvasClient:
    def __init__(
        self,
        session_file: str | Path,
        *,
        lms_base_url: str = "https://lms.ssu.ac.kr",
        canvas_base_url: str | None = None,
    ):
        self.session_file = Path(session_file)
        lms_base_url = lms_base_url.rstrip("/")
        canvas_base_url = (canvas_base_url or lms_base_url.replace("//lms.", "//canvas.")).rstrip(
            "/"
        )
        self.BASE = f"{lms_base_url}/learningx/api/v1"
        self.CANVAS_BASE = f"{canvas_base_url}/api/v1"
        self._client: httpx.AsyncClient | None = None
        self._token: str | None = None
        self._csrf: str | None = None

    def _api_base(self, use_canvas: bool) -> str:
        return self.CANVAS_BASE if use_canvas else self.BASE

    def _safe_url(self, path: str, use_canvas: bool) -> str:
        base = self._api_base(use_canvas)
        url = f"{base}{path}" if path.startswith("/") else path
        parsed, expected = urlparse(url), urlparse(base)
        if (
            parsed.scheme != "https"
            or parsed.scheme != expected.scheme
            or parsed.netloc != expected.netloc
            or not parsed.path.startswith(expected.path + "/")
        ):
            raise ValueError("허용되지 않은 LMS API 주소입니다.")
        return url

    async def _request(self, url: str, *, params: dict | None, use_canvas: bool) -> httpx.Response:
        if not self._client:
            raise RuntimeError("클라이언트가 초기화되지 않았습니다.")
        for _ in range(5):
            response = await self._client.get(
                url, params=params, headers=self._headers_for(use_canvas), follow_redirects=False
            )
            if not response.is_redirect:
                return response
            location = response.headers.get("Location")
            if not location:
                return response
            target = urljoin(url, location)
            parsed = urlparse(target)
            if parsed.hostname == "smartid.ssu.ac.kr" or (
                parsed.hostname in {"lms.ssu.ac.kr", "canvas.ssu.ac.kr"}
                and "login" in parsed.path.lower()
            ):
                # 로그인 리디렉션은 따라가지 않고 인증 만료로 알려 준다. 응답에 담긴
                # URL·쿼리를 예외 문자열에 넣지 않아 토큰이 에이전트에 노출되지 않는다.
                raise httpx.HTTPStatusError(
                    "LMS 세션이 만료되었습니다.",
                    request=response.request,
                    response=httpx.Response(401, request=response.request),
                )
            url = self._safe_url(target, use_canvas)
            params = None
        raise httpx.TooManyRedirects("LMS API 리디렉션 횟수가 너무 많습니다.")

    async def init(self) -> None:
        data = json.loads(self.session_file.read_text("utf-8"))
        jar = httpx.Cookies()
        for cookie in data.get("cookies", []):
            if cookie.get("name") and cookie.get("value") is not None:
                jar.set(
                    cookie["name"],
                    cookie["value"],
                    domain=cookie.get("domain", ""),
                    path=cookie.get("path", "/"),
                )
        self._token = self._find_token(data)
        self._csrf = self._find_cookie(data, "_csrf_token") or self._find_cookie(data, "XSRF-TOKEN")
        self._client = httpx.AsyncClient(
            cookies=jar,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
            timeout=30.0,
            follow_redirects=False,
        )

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()

    def _headers_for(self, use_canvas: bool) -> dict[str, str]:
        if use_canvas:
            headers = {"Referer": "https://canvas.ssu.ac.kr/"}
            if self._csrf:
                headers["X-CSRF-Token"] = self._csrf
            return headers
        headers = {"Referer": "https://lms.ssu.ac.kr/"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    async def get(self, path: str, params: dict | None = None, use_canvas: bool = False) -> Any:
        if not self._client:
            raise RuntimeError("클라이언트가 초기화되지 않았습니다.")
        url = self._safe_url(path, use_canvas)
        response = await self._request(url, params=params, use_canvas=use_canvas)
        response.raise_for_status()
        return response.json()

    async def get_all_pages(
        self, path: str, params: dict | None = None, use_canvas: bool = False
    ) -> list:
        url = self._safe_url(path, use_canvas)
        query = dict(params or {})
        query.setdefault("per_page", 100)
        out = []
        while url:
            response = await self._request(url, params=query, use_canvas=use_canvas)
            response.raise_for_status()
            body = response.json()
            out.extend(body if isinstance(body, list) else body.get("data", []))
            next_url = self._next_link(response.headers.get("Link", ""))
            url = self._safe_url(next_url, use_canvas) if next_url else None
            query = None
        return out

    @staticmethod
    def _find_token(data: dict) -> str | None:
        for origin in data.get("storage_state", {}).get("origins", []):
            if origin.get("origin", "").rstrip("/") != "https://lms.ssu.ac.kr":
                continue
            for item in origin.get("localStorage", []):
                if item.get("name") == "xn_api_token":
                    return item.get("value")
        return next(
            (
                c.get("value") for c in data.get("cookies", [])
                if c.get("name") == "xn_api_token"
                and c.get("domain", "").lstrip(".") in {"lms.ssu.ac.kr", "ssu.ac.kr"}
            ),
            None,
        )

    @staticmethod
    def _find_cookie(data: dict, name: str) -> str | None:
        return next(
            (c.get("value") for c in data.get("cookies", []) if c.get("name") == name), None
        )

    @staticmethod
    def _next_link(link: str) -> str | None:
        return next(
            (
                part.split("<", 1)[1].split(">", 1)[0]
                for part in link.split(",")
                if 'rel="next"' in part and "<" in part
            ),
            None,
        )
