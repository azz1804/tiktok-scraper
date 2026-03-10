"""
TikTok Scraper — Standalone

Browser-based API interception + SSR data extraction.
No external TikTok libraries. No database dependency.

Approach:
  1. Playwright drives a real Chromium browser (bypasses TLS fingerprinting)
  2. Route interception captures internal API responses as they happen
  3. SSR rehydration data is parsed from __UNIVERSAL_DATA_FOR_REHYDRATION__
  4. Cookies are extracted from Chrome's encrypted SQLite DB (macOS Keychain + AES-CBC)

Endpoints intercepted:
  - /api/search/general/full/   (keyword search)
  - /api/challenge/item_list/   (hashtag videos)
  - /api/post/item_list/        (user videos)
  - /api/comment/list/          (video comments)
"""

import asyncio
import json
import logging
import random
import re
from datetime import datetime
from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Route

from .auth import TikTokAuth
from .models import TikTokVideo, TikTokProfile, SearchResult

logger = logging.getLogger(__name__)

TIKTOK_BASE = "https://www.tiktok.com"


class TikTokClient:
    def __init__(self, auth: TikTokAuth | None = None, cookie_file: str | None = None):
        """
        Initialize the TikTok client.

        Args:
            auth: An existing TikTokAuth instance. If None, one is created.
            cookie_file: Path to cookie JSON file. Only used if auth is None.
        """
        self.auth = auth or TikTokAuth(cookie_file=cookie_file)
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    async def initialize(self):
        """Initialize browser and load authenticated context."""
        pw = await async_playwright().start()
        self._browser = await pw.chromium.launch(headless=True)
        self._context = await self.auth.get_authenticated_context()
        if self._context is None:
            self._context = await self._browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1920, "height": 1080},
            )
        self._page = await self._context.new_page()

        # Warmup: visit homepage to build device trust
        await self._page.goto(TIKTOK_BASE, wait_until="domcontentloaded", timeout=30000)
        await self._random_delay(2, 4)

    async def _random_delay(self, min_sec: float = 0.5, max_sec: float = 1.5):
        await asyncio.sleep(random.uniform(min_sec, max_sec))

    async def _intercept_api(self, page: Page, url_pattern: str) -> list[dict]:
        """Set up XHR interception for a given API pattern."""
        captured = []

        async def handle_route(route: Route):
            response = await route.fetch()
            try:
                body = await response.text()
                data = json.loads(body)
                captured.append(data)
            except Exception:
                pass
            await route.fulfill(response=response)

        await page.route(f"**{url_pattern}**", handle_route)
        return captured

    def _parse_video_from_item(self, item: dict) -> TikTokVideo | None:
        """Parse a video item from TikTok API response."""
        try:
            stats = item.get("stats", {})
            video_info = item.get("video", {})
            author_info = item.get("author", {})
            music_info = item.get("music", {})

            hashtags = []
            for challenge in item.get("challenges", []):
                title = challenge.get("title", "")
                if title:
                    hashtags.append(title)
            desc = item.get("desc", "")
            hashtags.extend(re.findall(r"#(\w+)", desc))
            hashtags = list(set(hashtags))

            created_ts = item.get("createTime", 0)
            created_at = datetime.fromtimestamp(int(created_ts)) if created_ts else None

            return TikTokVideo(
                video_id=str(item.get("id", "")),
                author=author_info.get("uniqueId", ""),
                author_sec_uid=author_info.get("secUid", ""),
                description=desc,
                hashtags=hashtags,
                views=int(stats.get("playCount", 0) or 0),
                likes=int(stats.get("diggCount", 0) or 0),
                shares=int(stats.get("shareCount", 0) or 0),
                comments=int(stats.get("commentCount", 0) or 0),
                duration=video_info.get("duration", 0),
                play_url=video_info.get("playAddr", ""),
                download_url=video_info.get("downloadAddr", ""),
                sound_name=music_info.get("title", ""),
                created_at=created_at,
            )
        except Exception as e:
            logger.error(f"Error parsing video item: {e}")
            return None

    def _parse_rehydration_data(self, html: str) -> list[dict]:
        """Extract video data from __UNIVERSAL_DATA_FOR_REHYDRATION__ script tag."""
        pattern = r'<script[^>]*id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>'
        match = re.search(pattern, html, re.DOTALL)
        if not match:
            return []

        try:
            data = json.loads(match.group(1))
            default_scope = data.get("__DEFAULT_SCOPE__", {})
            items = []

            for key in default_scope:
                scope_data = default_scope[key]
                if isinstance(scope_data, dict):
                    item_list = scope_data.get("itemList", [])
                    if item_list:
                        items.extend(item_list)
                    item_info = scope_data.get("itemInfo", {}).get("itemStruct")
                    if item_info:
                        items.append(item_info)

            return items
        except (json.JSONDecodeError, KeyError) as e:
            logger.error(f"Error parsing rehydration data: {e}")
            return []

    async def search_videos(self, keyword: str, count: int = 30) -> SearchResult:
        """Search TikTok for videos matching a keyword."""
        page = await self._context.new_page()
        videos = []
        profiles = []

        try:
            captured = await self._intercept_api(page, "/api/search/general/full/")

            search_url = f"{TIKTOK_BASE}/search?q={keyword}"
            await page.goto(search_url, wait_until="networkidle", timeout=30000)
            await self._random_delay(2, 4)

            # Extract from SSR rehydration data
            html = await page.content()
            rehydration_items = self._parse_rehydration_data(html)
            for item in rehydration_items:
                video = self._parse_video_from_item(item)
                if video:
                    videos.append(video)

            # Scroll to load more results
            loaded = len(videos)
            max_scrolls = min((count - loaded) // 10 + 1, 10)
            for _ in range(max_scrolls):
                if len(videos) >= count:
                    break
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await self._random_delay(1, 2.5)

            # Parse intercepted API responses
            for data in captured:
                item_list = data.get("data", [])
                if isinstance(item_list, list):
                    for entry in item_list:
                        item = entry.get("item", entry)
                        video = self._parse_video_from_item(item)
                        if video:
                            videos.append(video)

                        user_info = entry.get("user_list", [])
                        for user_entry in (user_info if isinstance(user_info, list) else []):
                            user = user_entry.get("user_info", {})
                            if user.get("sec_uid"):
                                profiles.append(TikTokProfile(
                                    sec_uid=user.get("sec_uid", ""),
                                    unique_id=user.get("unique_id", ""),
                                    nickname=user.get("nickname", ""),
                                    followers=user.get("follower_count", 0),
                                    total_likes=user.get("total_favorited", 0),
                                ))

            # Deduplicate
            seen_ids = set()
            unique_videos = []
            for v in videos:
                if v.video_id not in seen_ids:
                    seen_ids.add(v.video_id)
                    unique_videos.append(v)

            return SearchResult(videos=unique_videos[:count], profiles=profiles)

        finally:
            await page.close()

    async def get_hashtag_videos(self, hashtag: str, count: int = 50) -> list[TikTokVideo]:
        """Get videos for a specific hashtag."""
        page = await self._context.new_page()
        videos = []

        try:
            captured = await self._intercept_api(page, "/api/challenge/item_list/")

            tag_url = f"{TIKTOK_BASE}/tag/{hashtag.lstrip('#')}"
            await page.goto(tag_url, wait_until="networkidle", timeout=30000)
            await self._random_delay(2, 4)

            html = await page.content()
            for item in self._parse_rehydration_data(html):
                video = self._parse_video_from_item(item)
                if video:
                    videos.append(video)

            max_scrolls = min((count - len(videos)) // 12 + 1, 15)
            for _ in range(max_scrolls):
                if len(videos) >= count:
                    break
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await self._random_delay(1, 2.5)

            for data in captured:
                item_list = data.get("itemList", data.get("items", []))
                if isinstance(item_list, list):
                    for item in item_list:
                        video = self._parse_video_from_item(item)
                        if video:
                            videos.append(video)

            seen_ids = set()
            unique = []
            for v in videos:
                if v.video_id not in seen_ids:
                    seen_ids.add(v.video_id)
                    unique.append(v)

            return unique[:count]

        finally:
            await page.close()

    async def get_user_videos(self, sec_uid: str, count: int = 30) -> list[TikTokVideo]:
        """Get videos from a specific user profile."""
        page = await self._context.new_page()
        videos = []

        try:
            captured = await self._intercept_api(page, "/api/post/item_list/")

            await page.goto(f"{TIKTOK_BASE}/@{sec_uid}", wait_until="networkidle", timeout=30000)
            await self._random_delay(2, 4)

            html = await page.content()
            for item in self._parse_rehydration_data(html):
                video = self._parse_video_from_item(item)
                if video:
                    videos.append(video)

            max_scrolls = min((count - len(videos)) // 12 + 1, 10)
            for _ in range(max_scrolls):
                if len(videos) >= count:
                    break
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await self._random_delay(1, 2)

            for data in captured:
                item_list = data.get("itemList", [])
                if isinstance(item_list, list):
                    for item in item_list:
                        video = self._parse_video_from_item(item)
                        if video:
                            videos.append(video)

            seen_ids = set()
            unique = []
            for v in videos:
                if v.video_id not in seen_ids:
                    seen_ids.add(v.video_id)
                    unique.append(v)

            return unique[:count]

        finally:
            await page.close()

    async def get_video_comments(self, video_id: str, count: int = 50) -> list[dict]:
        """Get comments for a specific video."""
        page = await self._context.new_page()
        comments = []

        try:
            captured = await self._intercept_api(page, "/api/comment/list/")

            await page.goto(f"{TIKTOK_BASE}/video/{video_id}", wait_until="networkidle", timeout=30000)
            await self._random_delay(2, 4)

            try:
                comment_btn = await page.query_selector('[data-e2e="comment-icon"]')
                if comment_btn:
                    await comment_btn.click()
                    await self._random_delay(2, 3)
            except Exception:
                pass

            for _ in range(min(count // 20 + 1, 5)):
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await self._random_delay(1, 2)

            for data in captured:
                comment_list = data.get("comments", [])
                if isinstance(comment_list, list):
                    for c in comment_list:
                        comments.append({
                            "user": c.get("user", {}).get("unique_id", ""),
                            "text": c.get("text", ""),
                            "likes": c.get("digg_count", 0),
                            "created_at": c.get("create_time", 0),
                        })

            return comments[:count]

        finally:
            await page.close()

    async def close(self):
        if self._page:
            await self._page.close()
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
