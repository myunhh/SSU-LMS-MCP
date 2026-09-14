"""Playwright SSO와 안전한 세션 파일 저장."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright


def is_usable_session_data(data: object) -> bool:
    """토큰 값을 노출하지 않고 Playwright/HTTP가 사용할 최소 구조만 확인한다."""
    if not isinstance(data, dict):
        return False
    cookies = data.get("cookies")
    storage = data.get("storage_state")
    if not isinstance(cookies, list) or not isinstance(storage, dict):
        return False
    stored_cookies = storage.get("cookies")
    origins = storage.get("origins")
    if not isinstance(stored_cookies, list) or not isinstance(origins, list):
        return False
    for cookie in [*cookies, *stored_cookies]:
        if not isinstance(cookie, dict) or not all(
            isinstance(cookie.get(key), str) for key in ("name", "value", "domain", "path")
        ):
            return False
        if not cookie["name"] or not cookie["domain"] or not cookie["path"].startswith("/"):
            return False
    for cookie in stored_cookies:
        if (
            not isinstance(cookie.get("expires"), (int, float))
            or not isinstance(cookie.get("httpOnly"), bool)
            or not isinstance(cookie.get("secure"), bool)
            or cookie.get("sameSite") not in {"Strict", "Lax", "None"}
        ):
            return False
    has_token = False
    for origin in origins:
        if not isinstance(origin, dict) or not isinstance(origin.get("origin"), str):
            return False
        entries = origin.get("localStorage")
        if not isinstance(entries, list):
            return False
        for entry in entries:
            if not isinstance(entry, dict) or not all(
                isinstance(entry.get(key), str) for key in ("name", "value")
            ):
                return False
            if origin["origin"].rstrip("/") == "https://lms.ssu.ac.kr" and entry["name"] == "xn_api_token" and entry["value"]:
                has_token = True
        parsed = urlparse(origin["origin"])
        if parsed.scheme not in {"https", "http"} or not parsed.hostname:
            return False
    has_cookie = any(
        cookie["value"] and (
            cookie["domain"].lstrip(".") == "ssu.ac.kr"
            or cookie["domain"].lstrip(".").endswith(".ssu.ac.kr")
        )
        for cookie in cookies
    )
    return bool(has_cookie or has_token)


class SSULMSAuthPlaywright:
    def __init__(self, session_file: str | Path, *, headless: bool = True):
        self.session_file = Path(session_file)
        self.headless = headless
        self.user_info: dict = {}
        self.base_url = "https://lms.ssu.ac.kr"
        self.sso_url = "https://smartid.ssu.ac.kr"
        self.canvas_url = "https://canvas.ssu.ac.kr"

    def _save_session(self, cookies: list, storage_state: dict) -> None:
        self.session_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "cookies": cookies,
            "storage_state": storage_state,
            "user_info": self.user_info,
            "saved_at": datetime.now(UTC).isoformat(),
        }
        fd, raw_tmp = tempfile.mkstemp(
            prefix=f".{self.session_file.name}.", suffix=".tmp", dir=self.session_file.parent
        )
        tmp = Path(raw_tmp)
        try:
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
            os.replace(tmp, self.session_file)
            try:
                os.chmod(self.session_file, 0o600)
            except OSError:
                pass
        except BaseException:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    async def login(self, student_id: str, password: str) -> bool:
        async with async_playwright() as play:
            browser = await play.chromium.launch(headless=self.headless)
            try:
                context = await browser.new_context()
                page = await context.new_page()
                await page.goto(
                    f"{self.sso_url}/Symtra_sso/smln.asp?apiReturnUrl=https%3A%2F%2Flms.ssu.ac.kr%2Fxn-sso%2Fgw-cb.php",
                    wait_until="networkidle",
                )
                selector = 'input[name="userid"], input[name="username"], input[name="id"]'
                await page.wait_for_selector(selector, timeout=5000)
                await page.fill(selector, student_id)
                await page.fill('input[type="password"]', password)
                login_selector = (
                    'input[type="submit"], button[type="submit"], '
                    'button:has-text("로그인"), a:has-text("로그인"), input[type="image"]'
                )
                await page.wait_for_selector(login_selector, timeout=5000)
                async with page.expect_navigation(timeout=15000, wait_until="networkidle"):
                    await page.click(login_selector)
                if self.sso_url in page.url or "login" in page.url.lower():
                    return False
                self.user_info = {"login_time": datetime.now(UTC).isoformat()}
                self._save_session(await context.cookies(), await context.storage_state())
                return True
            finally:
                await browser.close()

    async def load_session(self) -> bool:
        try:
            state = json.loads(self.session_file.read_text("utf-8"))
            storage = state["storage_state"]
            if not is_usable_session_data(state):
                return False
        except (OSError, ValueError, KeyError, TypeError):
            return False
        async with async_playwright() as play:
            browser = await play.chromium.launch(headless=self.headless)
            try:
                context = await browser.new_context(storage_state=storage)
                page = await context.new_page()
                await page.goto(
                    f"{self.base_url}/main.php", timeout=15000, wait_until="networkidle"
                )
                if self.sso_url in page.url or "login" in page.url.lower():
                    return False
                # Canvas는 LearningX와 별도 쿠키를 사용하므로 갱신을 시도한다. 이 방문의
                # 실패는 이미 확인한 LMS 세션을 무효로 만들지 않는다.
                try:
                    await page.goto(f"{self.canvas_url}/", timeout=15000, wait_until="networkidle")
                except Exception:
                    pass
                self.user_info = (
                    state.get("user_info") if isinstance(state.get("user_info"), dict) else {}
                )
                self._save_session(await context.cookies(), await context.storage_state())
                return True
            finally:
                await browser.close()
