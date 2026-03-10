"""
TikTok Cookie Authentication — Standalone

Extracts TikTok session cookies from Chrome via browser_cookie3.
Stores cookies in a local JSON file (no database dependency).
"""

import asyncio
import json
import logging
import os
import subprocess
from pathlib import Path

import browser_cookie3
from playwright.async_api import async_playwright, Browser, BrowserContext

logger = logging.getLogger(__name__)

HOME_URL = "https://www.tiktok.com"
LOGIN_URL = "https://www.tiktok.com/login"

REQUIRED_COOKIES = ["sessionid", "sid_tt", "passport_csrf_token", "sid_guard", "uid_tt"]

# Default path for standalone cookie storage
COOKIE_FILE = Path(__file__).parent / ".tiktok_cookies.json"


def extract_tiktok_cookies_from_chrome() -> dict:
    """Extract TikTok cookies from Chrome using browser_cookie3."""
    cj = browser_cookie3.chrome(domain_name=".tiktok.com")
    cookies = {c.name: c.value for c in cj}
    return cookies


class TikTokAuth:
    def __init__(self, cookie_file: str | Path | None = None):
        self._browser: Browser | None = None
        self._cookies: dict | None = None
        self._pw = None
        self._cookie_file = Path(cookie_file) if cookie_file else COOKIE_FILE

    def _load_cookies_from_file(self) -> dict | None:
        """Load cookies from local JSON file."""
        if not self._cookie_file.exists():
            return None
        try:
            data = json.loads(self._cookie_file.read_text())
            if any(k in data for k in REQUIRED_COOKIES[:2]):
                return data
        except (json.JSONDecodeError, KeyError):
            pass
        return None

    def _save_cookies_to_file(self, cookies: dict):
        """Save cookies to local JSON file."""
        self._cookie_file.write_text(json.dumps(cookies, indent=2))

    async def get_active_cookies(self) -> dict | None:
        """Load cookies from file storage."""
        cookies = self._load_cookies_from_file()
        if cookies:
            self._cookies = cookies
        return cookies

    async def import_cookies_now(self) -> dict:
        """Extract TikTok cookies from Chrome immediately."""
        cookies = await asyncio.get_event_loop().run_in_executor(
            None, extract_tiktok_cookies_from_chrome
        )
        has_session = any(c in cookies for c in REQUIRED_COOKIES[:2])
        if has_session:
            self._save_cookies_to_file(cookies)
            self._cookies = cookies
            logger.info(f"Cookies imported: {len(cookies)} cookies")
        return cookies

    async def start_browser_login(self, on_status=None) -> bool:
        """Open TikTok in Chrome, wait for login, extract cookies."""
        try:
            if on_status:
                await on_status("Opening TikTok in Chrome... Log in normally.")

            subprocess.Popen(["open", "-a", "Google Chrome", LOGIN_URL])

            if on_status:
                await on_status("Log in to TikTok in Chrome, then come back here.")

            for i in range(36):  # 36 * 5s = 180s max
                await asyncio.sleep(5)

                try:
                    cookies = await asyncio.get_event_loop().run_in_executor(
                        None, extract_tiktok_cookies_from_chrome
                    )
                except Exception as e:
                    logger.debug(f"Cookie extraction attempt {i}: {e}")
                    continue

                has_session = any(c in cookies for c in REQUIRED_COOKIES[:2])
                if has_session:
                    self._save_cookies_to_file(cookies)
                    self._cookies = cookies
                    logger.info(f"TikTok cookies captured: {len(cookies)} cookies")
                    if on_status:
                        await on_status("Login successful! Cookies captured.")
                    return True

            logger.warning("Login timed out")
            if on_status:
                await on_status("Timed out. Make sure you're logged in on tiktok.com.")
            return False

        except Exception as e:
            logger.error(f"Login error: {e}")
            if on_status:
                await on_status(f"Error: {str(e)}")
            return False

    async def get_authenticated_context(self) -> BrowserContext | None:
        """Get a Playwright browser context loaded with saved cookies."""
        cookies = self._cookies or await self.get_active_cookies()
        if not cookies:
            return None

        if self._pw is None:
            self._pw = await async_playwright().start()
        if self._browser is None or not self._browser.is_connected():
            self._browser = await self._pw.chromium.launch(headless=True)

        context = await self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
        )

        cookie_list = []
        for name, value in cookies.items():
            if not name or not value or not isinstance(value, str):
                continue
            cookie_list.append({
                "name": name,
                "value": value,
                "domain": ".tiktok.com",
                "path": "/",
            })

        if cookie_list:
            try:
                await context.add_cookies(cookie_list)
            except Exception as e:
                logger.warning(f"Bulk cookie add failed: {e}, trying one by one")
                for c in cookie_list:
                    try:
                        await context.add_cookies([c])
                    except Exception:
                        logger.debug(f"Skipped invalid cookie: {c['name']}")
        return context

    async def close(self):
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._pw:
            await self._pw.stop()
            self._pw = None
