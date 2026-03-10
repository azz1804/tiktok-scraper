"""
TikTok Direct API Client — No Browser Required

Uses the reverse-engineered X-Bogus signing to make direct HTTP requests
to TikTok's internal API endpoints via httpx.

This is the real reverse engineering: no Playwright, no headless browser.
Just pure HTTP requests with proper signatures.
"""

import asyncio
import hashlib
import json
import logging
import random
import re
import time
from datetime import datetime
from urllib.parse import urlencode, quote

import httpx

from .xbogus import generate_xbogus, sign_url
from .models import TikTokVideo, TikTokProfile, SearchResult
from .auth import TikTokAuth, extract_tiktok_cookies_from_chrome

logger = logging.getLogger(__name__)

# TikTok API base
API_BASE = "https://www.tiktok.com/api"

# Default User-Agent — MUST match what's used for X-Bogus generation
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# Common API parameters that TikTok expects
COMMON_PARAMS = {
    "aid": "1988",
    "app_language": "en",
    "app_name": "tiktok_web",
    "browser_language": "en-US",
    "browser_name": "Mozilla",
    "browser_online": "true",
    "browser_platform": "Win32",
    "browser_version": "5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "channel": "tiktok_web",
    "cookie_enabled": "true",
    "device_id": "",
    "device_platform": "web_pc",
    "focus_state": "true",
    "from_page": "search",
    "history_len": "3",
    "is_fullscreen": "false",
    "is_page_visible": "true",
    "language": "en",
    "os": "windows",
    "priority_region": "",
    "referer": "",
    "region": "US",
    "screen_height": "1080",
    "screen_width": "1920",
    "tz_name": "Europe/Paris",
    "webcast_language": "en",
}


def _generate_device_id() -> str:
    """Generate a plausible TikTok device ID (19-digit number)."""
    return str(random.randint(10**18, 10**19 - 1))


def _build_ms_token(length: int = 126) -> str:
    """
    Generate a plausible msToken.
    The real msToken comes from TikTok's web-mssdk but a random
    Base64-like string of the right length works for many endpoints.
    """
    chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    return "".join(random.choice(chars) for _ in range(length))


class TikTokDirectClient:
    """
    Direct HTTP client for TikTok's internal API.
    No browser. Pure HTTP + X-Bogus signing.
    """

    def __init__(
        self,
        cookies: dict | None = None,
        user_agent: str = DEFAULT_UA,
        proxy: str | None = None,
    ):
        self._user_agent = user_agent
        self._device_id = _generate_device_id()
        self._cookies = cookies or {}
        self._ms_token = self._cookies.get("msToken", _build_ms_token())
        self._proxy = proxy

        self._client: httpx.AsyncClient | None = None

    async def initialize(self, auto_import_cookies: bool = True):
        """
        Initialize the HTTP client.

        Args:
            auto_import_cookies: If True and no cookies provided,
                                 automatically extract from Chrome.
        """
        if not self._cookies and auto_import_cookies:
            try:
                self._cookies = await asyncio.get_event_loop().run_in_executor(
                    None, extract_tiktok_cookies_from_chrome
                )
                self._ms_token = self._cookies.get("msToken", self._ms_token)
                logger.info(f"Auto-imported {len(self._cookies)} cookies from Chrome")
            except Exception as e:
                logger.warning(f"Could not auto-import cookies: {e}")

        transport = None
        if self._proxy:
            transport = httpx.AsyncHTTPTransport(proxy=self._proxy)

        self._client = httpx.AsyncClient(
            headers={
                "User-Agent": self._user_agent,
                "Referer": "https://www.tiktok.com/",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Sec-Ch-Ua": '"Chromium";v="131", "Not_A Brand";v="24"',
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"Windows"',
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
            },
            cookies=self._cookies,
            timeout=30.0,
            follow_redirects=True,
            transport=transport,
            http2=True,
        )

    async def _signed_request(
        self,
        endpoint: str,
        params: dict,
        method: str = "GET",
        body: str = "",
    ) -> dict | None:
        """
        Make a signed API request with X-Bogus.

        Args:
            endpoint: API path (e.g. "/api/search/general/full/")
            params: Query parameters dict.
            method: HTTP method.
            body: POST body.

        Returns:
            Parsed JSON response or None on error.
        """
        if not self._client:
            raise RuntimeError("Client not initialized. Call initialize() first.")

        # Merge common params
        full_params = {**COMMON_PARAMS}
        full_params["device_id"] = self._device_id
        full_params["msToken"] = self._ms_token
        full_params.update(params)

        # Build query string
        query_string = urlencode(full_params, safe="-_.")

        # Generate X-Bogus signature
        xbogus = generate_xbogus(query_string, self._user_agent, body)
        query_string += f"&X-Bogus={xbogus}"

        url = f"https://www.tiktok.com{endpoint}?{query_string}"

        try:
            if method == "GET":
                response = await self._client.get(url)
            else:
                response = await self._client.post(url, content=body)

            if response.status_code == 200:
                data = response.json()

                # Update msToken from response cookies if present
                if "msToken" in response.cookies:
                    self._ms_token = response.cookies["msToken"]

                return data
            else:
                logger.error(f"API returned {response.status_code} for {endpoint}")
                logger.debug(f"Response: {response.text[:500]}")
                return None

        except Exception as e:
            logger.error(f"Request failed for {endpoint}: {e}")
            return None

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

    async def search_videos(self, keyword: str, count: int = 30, offset: int = 0) -> SearchResult:
        """
        Search TikTok for videos matching a keyword.
        Direct API call — no browser.
        """
        videos = []
        profiles = []
        cursor = offset

        while len(videos) < count:
            data = await self._signed_request(
                "/api/search/general/full/",
                {
                    "keyword": keyword,
                    "offset": str(cursor),
                    "count": str(min(20, count - len(videos))),
                    "search_source": "normal_search",
                    "query_source": "",
                    "web_search_code": '{"tiktok":{"client_params_x":{"search_engine":{"ies_mt_user_live_video_card_use_498":1,"mt_search_general_user_live_card":1}},"search_server":{}}}',
                    "search_id": "",
                },
            )

            if not data:
                break

            item_list = data.get("data", [])
            if not isinstance(item_list, list) or not item_list:
                break

            for entry in item_list:
                item = entry.get("item", entry)
                if "id" in item or "video_id" in item:
                    video = self._parse_video_from_item(item)
                    if video:
                        videos.append(video)

                # Extract profile info
                user_list = entry.get("user_list", [])
                if isinstance(user_list, list):
                    for user_entry in user_list:
                        user = user_entry.get("user_info", {})
                        if user.get("sec_uid"):
                            profiles.append(TikTokProfile(
                                sec_uid=user.get("sec_uid", ""),
                                unique_id=user.get("unique_id", ""),
                                nickname=user.get("nickname", ""),
                                followers=int(user.get("follower_count", 0) or 0),
                                total_likes=int(user.get("total_favorited", 0) or 0),
                            ))

            has_more = data.get("has_more", 0)
            cursor = data.get("cursor", cursor + 20)

            if not has_more:
                break

            # Small delay between pagination requests
            await asyncio.sleep(random.uniform(0.5, 1.5))

        # Deduplicate
        seen = set()
        unique = []
        for v in videos:
            if v.video_id not in seen:
                seen.add(v.video_id)
                unique.append(v)

        return SearchResult(
            videos=unique[:count],
            profiles=profiles,
            has_more=bool(len(videos) >= count),
            cursor=str(cursor),
        )

    async def get_hashtag_videos(self, hashtag: str, count: int = 50) -> list[TikTokVideo]:
        """Get videos for a specific hashtag via direct API."""
        tag = hashtag.lstrip("#")
        videos = []

        # First, resolve the hashtag to get challenge_id
        challenge_data = await self._signed_request(
            "/api/search/general/full/",
            {
                "keyword": tag,
                "offset": "0",
                "count": "1",
                "search_source": "normal_search",
            },
        )

        # Try the challenge item_list endpoint
        cursor = 0
        while len(videos) < count:
            data = await self._signed_request(
                "/api/challenge/item_list/",
                {
                    "challengeName": tag,
                    "count": str(min(30, count - len(videos))),
                    "cursor": str(cursor),
                    "coverFormat": "2",
                },
            )

            if not data:
                break

            item_list = data.get("itemList", data.get("items", []))
            if not isinstance(item_list, list) or not item_list:
                break

            for item in item_list:
                video = self._parse_video_from_item(item)
                if video:
                    videos.append(video)

            if not data.get("hasMore", False):
                break

            cursor = data.get("cursor", cursor + 30)
            await asyncio.sleep(random.uniform(0.5, 1.5))

        # Deduplicate
        seen = set()
        unique = []
        for v in videos:
            if v.video_id not in seen:
                seen.add(v.video_id)
                unique.append(v)

        return unique[:count]

    async def get_user_videos(self, username: str, count: int = 30) -> list[TikTokVideo]:
        """Get videos from a specific user via direct API."""
        videos = []

        # First get user's secUid by visiting their profile page
        # We need the secUid for the post/item_list API
        user_data = await self._signed_request(
            "/api/search/general/full/",
            {
                "keyword": username,
                "offset": "0",
                "count": "1",
                "search_source": "normal_search",
            },
        )

        sec_uid = ""
        if user_data:
            for entry in user_data.get("data", []):
                user_list = entry.get("user_list", [])
                if isinstance(user_list, list):
                    for u in user_list:
                        info = u.get("user_info", {})
                        if info.get("unique_id", "").lower() == username.lower():
                            sec_uid = info.get("sec_uid", "")
                            break
                if sec_uid:
                    break

        if not sec_uid:
            logger.warning(f"Could not find secUid for user {username}")
            return []

        cursor = 0
        while len(videos) < count:
            data = await self._signed_request(
                "/api/post/item_list/",
                {
                    "secUid": sec_uid,
                    "count": str(min(30, count - len(videos))),
                    "cursor": str(cursor),
                    "coverFormat": "2",
                },
            )

            if not data:
                break

            item_list = data.get("itemList", [])
            if not isinstance(item_list, list) or not item_list:
                break

            for item in item_list:
                video = self._parse_video_from_item(item)
                if video:
                    videos.append(video)

            if not data.get("hasMore", False):
                break

            cursor = data.get("cursor", cursor + 30)
            await asyncio.sleep(random.uniform(0.5, 1.5))

        seen = set()
        unique = []
        for v in videos:
            if v.video_id not in seen:
                seen.add(v.video_id)
                unique.append(v)

        return unique[:count]

    async def get_video_comments(self, video_id: str, count: int = 50) -> list[dict]:
        """Get comments for a specific video via direct API."""
        comments = []
        cursor = 0

        while len(comments) < count:
            data = await self._signed_request(
                "/api/comment/list/",
                {
                    "aweme_id": video_id,
                    "count": str(min(50, count - len(comments))),
                    "cursor": str(cursor),
                    "current_region": "US",
                },
            )

            if not data:
                break

            comment_list = data.get("comments", [])
            if not isinstance(comment_list, list) or not comment_list:
                break

            for c in comment_list:
                comments.append({
                    "user": c.get("user", {}).get("unique_id", ""),
                    "text": c.get("text", ""),
                    "likes": int(c.get("digg_count", 0) or 0),
                    "created_at": c.get("create_time", 0),
                })

            if not data.get("has_more", False):
                break

            cursor = data.get("cursor", cursor + 50)
            await asyncio.sleep(random.uniform(0.5, 1.5))

        return comments[:count]

    async def get_video_detail(self, video_id: str) -> TikTokVideo | None:
        """Get detailed info for a single video."""
        data = await self._signed_request(
            "/api/item/detail/",
            {"itemId": video_id},
        )

        if not data:
            return None

        item = data.get("itemInfo", {}).get("itemStruct")
        if item:
            return self._parse_video_from_item(item)
        return None

    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None
