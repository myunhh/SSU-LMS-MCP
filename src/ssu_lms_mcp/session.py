"""세션 수명 관리. 자격증명은 파일에 보관하지 않는다."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
from filelock import AsyncFileLock

from .adapters.canvas_client import CanvasClient
from .auth import SSULMSAuthPlaywright, is_usable_session_data
from .config import Settings


class SessionRequiredError(RuntimeError):
    """재로그인이 필요한 경우의 안전한 공개 오류."""

    def __init__(self, message: str | None = None):
        super().__init__(
            message
            or "LMS 세션이 없거나 만료되었습니다. `ssu-lms-mcp login`으로 다시 로그인하세요."
        )


class SessionManager:
    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def _lock(self) -> AsyncFileLock:
        return AsyncFileLock(str(self.settings.session_file) + ".lock")

    def _auth(self) -> SSULMSAuthPlaywright:
        return SSULMSAuthPlaywright(self.settings.session_file, headless=self.settings.headless)

    def _ensure_data_dir(self) -> None:
        self.settings.session_file.parent.mkdir(parents=True, exist_ok=True)

    async def login(self, student_id: str, password: str) -> dict:
        self._ensure_data_dir()
        async with self._lock:
            ok = await self._auth().login(student_id, password)
        if not ok:
            raise SessionRequiredError()
        return self.status()

    async def refresh(self) -> dict:
        self._ensure_data_dir()
        async with self._lock:
            ok = await self._auth().load_session()
        if not ok:
            raise SessionRequiredError()
        return self.status()

    def status(self) -> dict:
        result = {"authenticated": False, "saved_at": None, "needs_refresh": False}
        try:
            raw = json.loads(self.settings.session_file.read_text("utf-8"))
            if not is_usable_session_data(raw):
                return result
            saved = raw.get("saved_at")
            if not isinstance(saved, str):
                return result
            timestamp = datetime.fromisoformat(saved.replace("Z", "+00:00"))
            timestamp = timestamp.replace(tzinfo=UTC) if timestamp.tzinfo is None else timestamp
            result.update(
                authenticated=True,
                saved_at=timestamp.isoformat(),
                needs_refresh=(datetime.now(UTC) - timestamp).total_seconds()
                >= self.settings.session_refresh_seconds,
            )
            return result
        except (OSError, ValueError, TypeError):
            return result

    async def _refresh_if_stale(self) -> None:
        self._ensure_data_dir()
        async with self._lock:
            state = self.status()
            if not state["authenticated"]:
                raise SessionRequiredError()
            if not state["needs_refresh"]:
                return
            if not await self._auth().load_session():
                raise SessionRequiredError()

    @asynccontextmanager
    async def client(self):
        state = self.status()
        if not state["authenticated"]:
            raise SessionRequiredError()
        if state["needs_refresh"]:
            await self._refresh_if_stale()
        client = CanvasClient(self.settings.session_file)
        try:
            try:
                await client.init()
            except (FileNotFoundError, TypeError, ValueError):
                raise SessionRequiredError() from None
            try:
                yield client
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in {401, 403, 419}:
                    raise SessionRequiredError() from None
                raise
        finally:
            await client.close()
