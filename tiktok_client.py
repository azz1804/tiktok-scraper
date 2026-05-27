"""TikTok Playwright browser scraper — aggressive fallback.

Used when pure-HTTP scraping misses videos. Opens the /@user page in headless
Chrome with auth cookies, scrolls to the end, and harvests videos from every
JSON response + periodic HTML rehydration re-parses.
"""

import asyncio
import json
import logging
import random
from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Response

from .auth import TikTokAuth
from .models import TikTokVideo
from .parser import parse_video_from_item, parse_rehydration_data

logger = logging.getLogger(__name__)

TIKTOK_BASE = "https://www.tiktok.com"


def _iter_video_items(data) -> list[dict]:
    """Walk any JSON blob and yield dicts that look like video items."""
    found: list[dict] = []

    def walk(obj):
        if isinstance(obj, dict):
            if ("id" in obj or "aweme_id" in obj) and (
                "stats" in obj or "video" in obj or "author" in obj
            ):
                found.append(obj)
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(data)
    return found


class TikTokClient:
    def __init__(self, auth: TikTokAuth | None = None, cookie_file: str | None = None):
        self.auth = auth or TikTokAuth(cookie_file=cookie_file)
        self._pw = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None

    async def initialize(self):
        if self._pw is None:
            self._pw = await async_playwright().start()
        if self._browser is None or not self._browser.is_connected():
            self._browser = await self._pw.chromium.launch(headless=True)

        self._context = await self.auth.get_authenticated_context()
        if self._context is None:
            self._context = await self._browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 900},
                locale="en-US",
            )

    async def _random_delay(self, min_sec: float = 0.8, max_sec: float = 1.8):
        await asyncio.sleep(random.uniform(min_sec, max_sec))

    async def get_user_videos(
        self,
        username: str,
        count: int = 500,
        max_scrolls: int = 60,
        max_idle_scrolls: int = 5,
    ) -> list[TikTokVideo]:
        """Scroll /@user, capture every video item from JSON responses + HTML rehydration."""
        if self._context is None:
            await self.initialize()

        username_norm = username.lower().lstrip("@")
        seen_ids: set[str] = set()
        videos: list[TikTokVideo] = []
        captured_items: list[dict] = []

        page = await self._context.new_page()

        async def on_response(resp: Response):
            url = resp.url
            if "/api/" not in url and "/tiktok-web-api/" not in url:
                return
            ct = resp.headers.get("content-type", "")
            if "json" not in ct.lower():
                return
            try:
                data = await resp.json()
            except Exception:
                return
            items = _iter_video_items(data)
            if items:
                captured_items.extend(items)

        page.on("response", on_response)

        def _merge(items: list[dict], source: str) -> int:
            added = 0
            for it in items:
                v = parse_video_from_item(it)
                if not v or not v.video_id:
                    continue
                if v.video_id in seen_ids:
                    continue
                if (v.author or "").lower() != username_norm:
                    continue
                seen_ids.add(v.video_id)
                videos.append(v)
                added += 1
            if added:
                logger.info(f"[browser {username_norm}] +{added} via {source} (total: {len(videos)})")
            return added

        try:
            url = f"{TIKTOK_BASE}/@{username_norm}"
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await self._random_delay(2.0, 3.5)

            # Initial HTML rehydration
            html = await page.content()
            _merge(parse_rehydration_data(html), "rehydration_initial")

            # Drain any captures that arrived during load
            _merge(captured_items, "responses_initial")
            captured_items.clear()

            last_count = len(videos)
            idle = 0
            for i in range(max_scrolls):
                if len(videos) >= count:
                    break
                await page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
                await self._random_delay(1.0, 1.8)

                # Harvest newly captured responses
                if captured_items:
                    _merge(captured_items, f"scroll_{i}_responses")
                    captured_items.clear()

                # Re-parse rehydration every 4 scrolls (TikTok mutates it)
                if i % 4 == 3:
                    try:
                        html = await page.content()
                        _merge(parse_rehydration_data(html), f"scroll_{i}_rehydration")
                    except Exception:
                        pass

                if len(videos) == last_count:
                    idle += 1
                    if idle >= max_idle_scrolls:
                        logger.info(f"[browser {username_norm}] no new videos after {idle} scrolls, stopping")
                        break
                else:
                    idle = 0
                last_count = len(videos)

            # Final sweep: one more rehydration + captured buffer
            try:
                html = await page.content()
                _merge(parse_rehydration_data(html), "final_rehydration")
            except Exception:
                pass
            if captured_items:
                _merge(captured_items, "final_responses")

            logger.info(f"[browser {username_norm}] DONE — {len(videos)} unique videos")
            return videos[:count]

        finally:
            try:
                await page.close()
            except Exception:
                pass

    async def close(self):
        try:
            if self._context:
                await self._context.close()
        except Exception:
            pass
        self._context = None
        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass
        self._browser = None
        try:
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass
        self._pw = None
