"""TikTok Direct Client — pure httpx + X-Bogus + SSR.

Strategy for scraping an account (`get_user_videos`):
  /api/post/item_list/ is dead for web. So we tap every other source:
    1. SSR profile page       (/@user)        → itemList often embedded
    2. SSR user detail        → secUid, followers, bio (for more search seeds)
    3. Search "@username"                     → exact-author match
    4. Search "username"                      → exact-author match
    5. Search "username + <top hashtag>" ×N   → expand via own hashtags
    6. Search "username + <bio keyword>" ×N   → expand via bio
    7. Hashtag feeds via search ×N            → pull author-matched items
    8. Reposts /api/repost/item_list/         → self-reposts if any

Everything is deduped by video_id and filtered by author.lower() == username.lower().
"""

import asyncio
import json
import logging
import random
import re
from datetime import datetime
from urllib.parse import urlencode

import httpx

from .xbogus import generate_xbogus, sign_url
from .models import TikTokVideo, TikTokProfile, SearchResult
from .auth import TikTokAuth, extract_tiktok_cookies_from_chrome
from .parser import (
    parse_video_from_item,
    parse_rehydration_data,
    parse_search_profiles,
    detect_content_format,
)

logger = logging.getLogger(__name__)

BASE_URL = "https://www.tiktok.com"
API_BASE = f"{BASE_URL}/api"

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

COMMON_PARAMS = {
    "aid": "1988",
    "app_language": "en",
    "app_name": "tiktok_web",
    "browser_language": "en-US",
    "browser_name": "Mozilla",
    "browser_online": "true",
    "browser_platform": "MacIntel",
    "channel": "tiktok_web",
    "cookie_enabled": "true",
    "device_platform": "web_pc",
    "focus_state": "true",
    "from_page": "search",
    "history_len": "3",
    "is_fullscreen": "false",
    "is_page_visible": "true",
    "language": "en",
    "os": "mac",
    "priority_region": "",
    "region": "US",
    "screen_height": "1080",
    "screen_width": "1920",
    "tz_name": "Europe/Paris",
    "webcast_language": "en",
}

CHROME_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"macOS"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "Origin": "https://www.tiktok.com",
    "Referer": "https://www.tiktok.com/",
}

SSR_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"macOS"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}

STOPWORDS_FR = {
    "le", "la", "les", "un", "une", "des", "de", "du", "et", "ou", "mais", "pour",
    "avec", "sur", "pas", "ne", "que", "qui", "dans", "par", "mon", "ma", "mes",
    "je", "tu", "il", "elle", "nous", "vous", "ils", "elles", "est", "sont",
    "ce", "se", "ça", "ceci", "cela", "tout", "tous", "toute", "toutes",
}
STOPWORDS_EN = {
    "the", "a", "an", "and", "or", "but", "for", "with", "on", "not", "is", "are",
    "i", "you", "he", "she", "we", "they", "this", "that", "my", "your", "in",
    "of", "to", "by", "at", "as", "be", "it", "its", "all", "any", "from",
}
STOPWORDS = STOPWORDS_FR | STOPWORDS_EN


def _gen_device_id() -> str:
    return str(random.randint(7_000_000_000_000_000_000, 7_999_999_999_999_999_999))


def _build_ms_token(length: int = 126) -> str:
    chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    return "".join(random.choice(chars) for _ in range(length))


def _extract_bio_keywords(bio: str, limit: int = 5) -> list[str]:
    """Keep meaningful words from a bio: length >= 4, not stopwords, alpha-heavy."""
    if not bio:
        return []
    # Drop emojis and URLs
    text = re.sub(r"https?://\S+", " ", bio)
    text = re.sub(r"[^\w\s#@-]", " ", text, flags=re.UNICODE)
    words = re.findall(r"[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ-]{3,}", text.lower())
    out: list[str] = []
    seen = set()
    for w in words:
        if w in STOPWORDS or w in seen:
            continue
        seen.add(w)
        out.append(w)
        if len(out) >= limit:
            break
    return out


class TikTokDirectClient:
    """Hybrid TikTok client. Direct HTTP for hot path."""

    def __init__(
        self,
        cookies: dict | None = None,
        user_agent: str = DEFAULT_UA,
        proxy: str | None = None,
    ):
        self._user_agent = user_agent
        self._device_id = _gen_device_id()
        self._cookies = cookies or {}
        self._ms_token = self._cookies.get("msToken", _build_ms_token())
        self._proxy = proxy
        self._client: httpx.AsyncClient | None = None

    # ── Lifecycle ────────────────────────────────────────────────

    async def initialize(self, auto_import_cookies: bool = True):
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
            headers={"User-Agent": self._user_agent, **CHROME_HEADERS},
            cookies=self._cookies,
            timeout=30.0,
            follow_redirects=True,
            transport=transport,
            http2=True,
        )
        logger.info(
            f"TikTokDirectClient ready: {len(self._cookies)} cookies, "
            f"msToken={'yes' if self._cookies.get('msToken') else 'generated'}"
        )

    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None

    # ── Signed API ───────────────────────────────────────────────

    async def _signed_get(
        self, endpoint: str, params: dict, referer: str | None = None
    ) -> dict:
        if not self._client:
            raise RuntimeError("Call initialize() first")

        full = {**COMMON_PARAMS, "device_id": self._device_id}
        if self._ms_token:
            full["msToken"] = self._ms_token
        full.update(params)

        qs = urlencode(full, safe="-_.")
        xb = generate_xbogus(qs, self._user_agent, "")
        url = f"{API_BASE}{endpoint}?{qs}&X-Bogus={xb}"

        headers = dict(self._client.headers)
        if referer:
            headers["Referer"] = referer

        try:
            resp = await self._client.get(url, headers=headers)
            if resp.status_code != 200:
                logger.warning(f"API {endpoint} → {resp.status_code}")
                return {}
            if "msToken" in resp.cookies:
                self._ms_token = resp.cookies["msToken"]
            if not resp.text:
                return {}
            try:
                return resp.json()
            except Exception:
                logger.warning(f"API {endpoint}: non-JSON")
                return {}
        except Exception as e:
            logger.error(f"API {endpoint} failed: {e}")
            return {}

    async def _random_delay(self, min_s: float = 0.3, max_s: float = 0.8):
        await asyncio.sleep(random.uniform(min_s, max_s))

    # ── Search ───────────────────────────────────────────────────

    async def search_videos(
        self, keyword: str, count: int = 30, offset: int = 0
    ) -> SearchResult:
        videos: list[TikTokVideo] = []
        profiles: list[TikTokProfile] = []
        cursor = offset

        while len(videos) < count:
            data = await self._signed_get(
                "/search/general/full/",
                {
                    "keyword": keyword,
                    "offset": str(cursor),
                    "count": str(min(20, count - len(videos))),
                    "search_source": "normal_search",
                    "query_source": "",
                    "search_id": "",
                    "from_page": "search",
                },
            )
            if not data:
                break
            items = data.get("data", [])
            if not isinstance(items, list) or not items:
                break

            for entry in items:
                item = entry.get("item", entry)
                if "id" in item or "video_id" in item:
                    v = parse_video_from_item(item)
                    if v:
                        videos.append(v)

            profiles.extend(parse_search_profiles(items))
            if not data.get("has_more", 0):
                break
            cursor = data.get("cursor", cursor + 20)
            await self._random_delay(0.2, 0.5)

        seen = set()
        unique = []
        for v in videos:
            if v.video_id not in seen:
                seen.add(v.video_id)
                unique.append(v)

        return SearchResult(videos=unique[:count], profiles=profiles, cursor=str(cursor))

    async def get_hashtag_videos(self, hashtag: str, count: int = 50) -> list[TikTokVideo]:
        hashtag = hashtag.lstrip("#")
        result = await self.search_videos(f"#{hashtag}", count=count)
        videos = result.videos
        if len(videos) < count // 2:
            await self._random_delay(0.2, 0.5)
            result2 = await self.search_videos(hashtag, count=count - len(videos))
            seen = {v.video_id for v in videos}
            for v in result2.videos:
                if v.video_id not in seen:
                    videos.append(v)
                    seen.add(v.video_id)
        return videos[:count]

    async def get_challenge_detail(self, challenge_name: str) -> dict:
        data = await self._signed_get(
            "/challenge/detail/",
            {"challengeName": challenge_name.lstrip("#")},
        )
        return data.get("challengeInfo", {}) or {}

    async def get_user_reposts(self, sec_uid: str, count: int = 30) -> list[TikTokVideo]:
        videos: list[TikTokVideo] = []
        cursor = "0"
        while len(videos) < count:
            data = await self._signed_get(
                "/repost/item_list/",
                {
                    "secUid": sec_uid,
                    "count": str(min(30, count - len(videos))),
                    "cursor": cursor,
                    "from_page": "user",
                },
                referer=f"{BASE_URL}/",
            )
            if not data:
                break
            items = data.get("itemList", []) or []
            for item in items:
                v = parse_video_from_item(item)
                if v:
                    videos.append(v)
            if not data.get("hasMore") or not items:
                break
            cursor = str(data.get("cursor", "0"))
            await self._random_delay(0.2, 0.4)
        return videos[:count]

    # ── SSR (rehydration via httpx) ──────────────────────────────

    async def _ssr_fetch(self, url: str, referer: str | None = None) -> str:
        if not self._client:
            raise RuntimeError("Call initialize() first")
        headers = dict(SSR_HEADERS)
        headers["User-Agent"] = self._user_agent
        if referer:
            headers["Referer"] = referer
        try:
            resp = await self._client.get(url, headers=headers)
            if resp.status_code != 200:
                logger.warning(f"SSR {url}: {resp.status_code}")
                return ""
            return resp.text or ""
        except Exception as e:
            logger.warning(f"SSR {url}: {e}")
            return ""

    async def _ssr_rehydration(self, url: str, referer: str | None = None) -> dict:
        html = await self._ssr_fetch(url, referer=referer)
        if not html:
            return {}
        m = re.search(
            r'<script[^>]*id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>([^<]+)</script>',
            html,
        )
        if not m:
            return {}
        try:
            return json.loads(m.group(1))
        except Exception:
            return {}

    async def get_user_detail(self, username: str) -> dict | None:
        url = f"{BASE_URL}/@{username.lstrip('@')}"
        data = await self._ssr_rehydration(url)
        if not data:
            return None
        scope = data.get("__DEFAULT_SCOPE__", {})
        user_detail = scope.get("webapp.user-detail", {})
        user_info = user_detail.get("userInfo", {})
        if not user_info:
            return None
        user = user_info.get("user", {})
        stats = user_info.get("stats", {})
        return {
            "unique_id": user.get("uniqueId", ""),
            "sec_uid": user.get("secUid", ""),
            "nickname": user.get("nickname", ""),
            "signature": user.get("signature", ""),
            "verified": user.get("verified", False),
            "avatar": user.get("avatarLarger", ""),
            "followers": stats.get("followerCount", 0),
            "following": stats.get("followingCount", 0),
            "total_likes": stats.get("heart", 0),
            "video_count": stats.get("videoCount", 0),
            "bio_link": user.get("bioLink", {}).get("link", "") if isinstance(user.get("bioLink"), dict) else "",
        }

    async def get_video_detail(self, video_id: str, author: str) -> dict | None:
        url = f"{BASE_URL}/@{author.lstrip('@')}/video/{video_id}"
        data = await self._ssr_rehydration(url)
        if not data:
            return None
        scope = data.get("__DEFAULT_SCOPE__", {})
        for v in scope.values():
            if isinstance(v, dict):
                item = v.get("itemInfo", {}).get("itemStruct")
                if item:
                    return item
        return None

    async def enrich_single_video(self, video: TikTokVideo) -> None:
        item = await self.get_video_detail(video.video_id, video.author)
        if not item:
            return
        fmt, img_count, img_urls = detect_content_format(item)
        if fmt != "video":
            video.content_format = fmt
            if fmt == "carousel":
                video.main_format = "carousel"
            video.image_count = img_count
            if img_urls:
                video.image_urls = img_urls
        real_saves = int(item.get("stats", {}).get("collectCount", 0) or 0)
        if real_saves > video.saves:
            video.saves = real_saves

    # ── Aggressive user-video scraping (multi-source) ────────────

    async def get_user_videos(self, username: str, count: int = 200) -> list[TikTokVideo]:
        """Multi-source aggressive scrape. Only counts videos with author == username."""
        username_norm = username.lower().lstrip("@")
        seen_ids: set[str] = set()
        videos: list[TikTokVideo] = []
        phase_counts: dict[str, int] = {}

        def _add(items: list[TikTokVideo], phase: str) -> int:
            added = 0
            for v in items:
                if not v or not v.video_id:
                    continue
                if v.video_id in seen_ids:
                    continue
                if (v.author or "").lower() != username_norm:
                    continue
                seen_ids.add(v.video_id)
                videos.append(v)
                added += 1
            phase_counts[phase] = phase_counts.get(phase, 0) + added
            return added

        # ── Phase 0: SSR profile → get secUid, real stats, bio ──
        user_detail = await self.get_user_detail(username_norm)
        sec_uid = (user_detail or {}).get("sec_uid", "")
        bio = (user_detail or {}).get("signature", "")
        logger.info(f"[{username_norm}] profile: {user_detail and user_detail.get('video_count', 0)} videos declared, secUid={'ok' if sec_uid else 'none'}")

        # ── Phase 1: SSR user page HTML → rehydration itemList ──
        user_url = f"{BASE_URL}/@{username_norm}"
        html = await self._ssr_fetch(user_url)
        if html:
            items = parse_rehydration_data(html)
            parsed = [parse_video_from_item(it) for it in items]
            n = _add([p for p in parsed if p], "ssr_profile")
            logger.info(f"[{username_norm}] Phase 1 — SSR profile itemList: +{n} (total: {len(videos)})")

        # ── Phase 2: search '@username' ──
        if len(videos) < count:
            r = await self.search_videos(f"@{username_norm}", count=100)
            n = _add(r.videos, "search_at")
            logger.info(f"[{username_norm}] Phase 2 — search @user: +{n} (total: {len(videos)})")
            await self._random_delay()

        # ── Phase 3: search 'username' ──
        if len(videos) < count:
            r = await self.search_videos(username_norm, count=100)
            n = _add(r.videos, "search_plain")
            logger.info(f"[{username_norm}] Phase 3 — search user: +{n} (total: {len(videos)})")
            await self._random_delay()

        # Harvest seeds from what we have so far
        hashtag_freq: dict[str, int] = {}
        for v in videos:
            for tag in v.hashtags:
                tl = tag.lower()
                if len(tl) > 2 and tl != username_norm:
                    hashtag_freq[tl] = hashtag_freq.get(tl, 0) + 1
        top_hashtags = sorted(hashtag_freq, key=hashtag_freq.get, reverse=True)[:8]

        # ── Phase 4: 'username + top hashtag' ──
        for tag in top_hashtags:
            if len(videos) >= count:
                break
            r = await self.search_videos(f"{username_norm} {tag}", count=60)
            n = _add(r.videos, "search_user_tag")
            logger.info(f"[{username_norm}] Phase 4 — '{tag}': +{n} (total: {len(videos)})")
            await self._random_delay()

        # ── Phase 5: 'username + bio keyword' ──
        bio_keywords = _extract_bio_keywords(bio, limit=5)
        for kw in bio_keywords:
            if len(videos) >= count:
                break
            r = await self.search_videos(f"{username_norm} {kw}", count=40)
            n = _add(r.videos, "search_bio")
            logger.info(f"[{username_norm}] Phase 5 — bio '{kw}': +{n} (total: {len(videos)})")
            await self._random_delay()

        # ── Phase 6: hashtag feed (top 3) ──
        for tag in top_hashtags[:3]:
            if len(videos) >= count:
                break
            items = await self.get_hashtag_videos(tag, count=100)
            n = _add(items, "hashtag_feed")
            logger.info(f"[{username_norm}] Phase 6 — #{tag} feed: +{n} (total: {len(videos)})")
            await self._random_delay()

        # ── Phase 7: reposts (catches self-reposts) ──
        if sec_uid and len(videos) < count:
            try:
                reposts = await self.get_user_reposts(sec_uid, count=50)
                n = _add(reposts, "reposts")
                logger.info(f"[{username_norm}] Phase 7 — reposts: +{n} (total: {len(videos)})")
            except Exception as e:
                logger.warning(f"[{username_norm}] Phase 7 reposts failed: {e}")

        logger.info(
            f"[{username_norm}] DONE — {len(videos)} unique own-author videos. "
            f"Breakdown: {phase_counts}"
        )
        return videos[:count]

    async def get_user_with_reposts(self, username: str, count: int = 30) -> dict:
        user = await self.get_user_detail(username)
        if not user:
            return {"user": None, "reposts": []}
        reposts: list[TikTokVideo] = []
        if user.get("sec_uid"):
            reposts = await self.get_user_reposts(user["sec_uid"], count=count)
        return {"user": user, "reposts": reposts}

    # ── Downloads ────────────────────────────────────────────────

    async def _refresh_video_urls(self, video: TikTokVideo) -> bool:
        item = await self.get_video_detail(video.video_id, video.author)
        if not item:
            return False
        video_info = item.get("video", {}) or {}
        new_play = video_info.get("playAddr") or ""
        new_dl = video_info.get("downloadAddr") or ""
        if not (new_play or new_dl):
            return False
        video.play_url = new_play or video.play_url
        video.download_url = new_dl or video.download_url
        return True

    async def download_video(self, video: TikTokVideo) -> bytes | None:
        url = video.download_url or video.play_url
        if not url or not self._client:
            return None

        async def _try(u: str) -> httpx.Response | None:
            try:
                return await self._client.get(
                    u,
                    headers={"Referer": f"{BASE_URL}/@{video.author}/video/{video.video_id}"},
                )
            except Exception as e:
                logger.warning(f"Download attempt failed: {e}")
                return None

        resp = await _try(url)
        if resp is None or resp.status_code in (403, 410, 404):
            if await self._refresh_video_urls(video):
                fresh = video.download_url or video.play_url
                if fresh and fresh != url:
                    resp = await _try(fresh)
        if resp is None:
            return None
        try:
            if resp.status_code == 403 or len(resp.content) < 10_000:
                return None
            resp.raise_for_status()
            return resp.content
        except Exception as e:
            logger.error(f"Download {video.video_id}: {e}")
            return None

    async def download_carousel_images(self, video: TikTokVideo) -> list[bytes]:
        if not video.image_urls or not self._client:
            return []
        images = []
        for url in video.image_urls[:10]:
            try:
                resp = await self._client.get(
                    url, headers={"Referer": f"{BASE_URL}/@{video.author}"}
                )
                if resp.status_code == 200 and len(resp.content) > 1000:
                    images.append(resp.content)
            except Exception as e:
                logger.warning(f"Carousel download: {e}")
        return images
