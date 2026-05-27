"""Pure functions to parse TikTok API / SSR responses into models.

No network calls, no state — just data transformation.
"""

import json
import logging
import re
from datetime import datetime

from .models import TikTokVideo, TikTokProfile

logger = logging.getLogger(__name__)


def detect_content_format(item: dict) -> tuple[str, int, list[str]]:
    """Returns (format_string, image_count, image_urls)."""
    image_post = item.get("imagePost")
    if image_post:
        images = image_post.get("images", [])
        if images:
            urls = _extract_image_urls(images)
            return "carousel", len(images), urls

    if item.get("type") == 150 or item.get("photoMode"):
        images = (item.get("imagePost") or {}).get("images", [])
        urls = _extract_image_urls(images) if images else []
        return "carousel", len(images) if images else 1, urls

    duet_info = item.get("duetInfo")
    if duet_info and duet_info.get("duetFromId", "0") != "0":
        return "duet", 0, []

    return "video", 0, []


def _extract_image_urls(images: list[dict]) -> list[str]:
    urls = []
    for img in images:
        url = ""
        image_url = img.get("imageURL", {})
        if isinstance(image_url, dict):
            url_list = image_url.get("urlList", [])
            url = url_list[-1] if url_list else ""
        elif isinstance(image_url, str):
            url = image_url
        if not url and img.get("displayImage"):
            display = img["displayImage"]
            if isinstance(display, dict):
                ul = display.get("urlList", [])
                url = ul[-1] if ul else ""
        if not url and img.get("ownerWatermarkImage"):
            owner = img["ownerWatermarkImage"]
            if isinstance(owner, dict):
                ul = owner.get("urlList", [])
                url = ul[-1] if ul else ""
        if url:
            urls.append(url)
    return urls


def parse_video_from_item(item: dict) -> TikTokVideo | None:
    """Parse a TikTok API item dict into a TikTokVideo."""
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
        hashtags = list({h.lower(): h for h in hashtags}.values())

        created_ts = item.get("createTime", 0)
        created_at = datetime.fromtimestamp(int(created_ts)) if created_ts else None

        content_format, image_count, image_urls = detect_content_format(item)
        main_format = "carousel" if content_format == "carousel" else "ugc"

        saves = int(stats.get("collectCount", 0) or 0) or int(stats.get("saveCount", 0) or 0)

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
            saves=saves,
            duration=video_info.get("duration", 0),
            play_url=video_info.get("playAddr", ""),
            download_url=video_info.get("downloadAddr", ""),
            cover_url=(
                video_info.get("dynamicCover")
                or video_info.get("cover")
                or video_info.get("originCover")
                or (image_urls[0] if image_urls else "")
            ),
            sound_name=music_info.get("title", ""),
            created_at=created_at,
            content_format=content_format,
            main_format=main_format,
            image_count=image_count,
            image_urls=image_urls,
        )
    except Exception as e:
        logger.error(f"Error parsing video item: {e}")
        return None


def parse_rehydration_data(html: str) -> list[dict]:
    """Extract every video item buried in SSR rehydration script tags."""
    pattern = r'<script[^>]*id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>'
    match = re.search(pattern, html, re.DOTALL)
    if not match:
        pattern2 = r'<script[^>]*id="SIGI_STATE"[^>]*>(.*?)</script>'
        match = re.search(pattern2, html, re.DOTALL)
        if not match:
            return []

    try:
        data = json.loads(match.group(1))
        default_scope = data.get("__DEFAULT_SCOPE__", {})
        items = []
        for scope_data in default_scope.values():
            if not isinstance(scope_data, dict):
                continue
            item_list = scope_data.get("itemList", [])
            if item_list:
                items.extend(item_list)
            item_info = scope_data.get("itemInfo", {}).get("itemStruct")
            if item_info:
                items.append(item_info)
            # Some scopes nest the user's posts under userInfo/posts
            for k, v in scope_data.items():
                if isinstance(v, dict) and "itemList" in v and isinstance(v["itemList"], list):
                    items.extend(v["itemList"])

        if not items:
            item_module = data.get("ItemModule", {})
            for item_data in item_module.values():
                items.append(item_data)

        return items
    except (json.JSONDecodeError, KeyError) as e:
        logger.error(f"Error parsing rehydration data: {e}")
        return []


def parse_search_profiles(entries: list[dict]) -> list[TikTokProfile]:
    profiles = []
    for entry in entries:
        for user_entry in entry.get("user_list", []):
            user = user_entry.get("user_info", {})
            if user.get("sec_uid"):
                profiles.append(TikTokProfile(
                    sec_uid=user.get("sec_uid", ""),
                    unique_id=user.get("unique_id", ""),
                    nickname=user.get("nickname", ""),
                    followers=int(user.get("follower_count", 0) or 0),
                    total_likes=int(user.get("total_favorited", 0) or 0),
                ))
    return profiles
