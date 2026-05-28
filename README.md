# tiktok-scraper

> **Pure-HTTP TikTok scraper with reverse-engineered X-Bogus signing — no browser required.**
> Optional Playwright fallback for when TikTok's HTTP endpoints go silent.

[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

A Python library that talks to TikTok's internal web API the same way `tiktok.com` does — by signing every request with a reverse-engineered **X-Bogus** token. No browser, no Selenium, no Playwright (unless you opt in to the fallback). Just `httpx` and math.

---

## Why this exists

TikTok doesn't expose a public scraping API, and most third-party libraries rely on browser automation that's slow, fragile, and trivial to detect. This repo gives you:

- **Direct HTTP** access to TikTok's web endpoints (`/api/search/general/full/`, `/api/repost/item_list/`, SSR profile pages…)
- **X-Bogus signing** implemented from scratch (RC4 + custom Base64 + magic constants), so you can authenticate any request without spawning a browser
- **A Playwright fallback** (`TikTokClient`) for the rare cases where pure HTTP can't reach a video (heavy account pages, post-redesign endpoints)
- **A multi-source full-account scraping strategy** that works around the dead `/api/post/item_list/` endpoint by combining SSR + search + hashtags + reposts

## What you can do with it

| Use case | How |
|---|---|
| Scrape every video from a single account | `direct.get_user_videos(username, count=200)` |
| Search videos by keyword | `direct.search_videos("matcha latte", count=30)` |
| Pull a hashtag feed | `direct.get_hashtag_videos("paris", count=50)` |
| Fetch a user's reposts | `direct.get_user_reposts(sec_uid, count=30)` |
| Get profile metadata (followers, bio…) | `direct.get_user_detail(username)` |
| Get full detail for one video | `direct.get_video_detail(video_id, author)` |
| Download a video file | `direct.download_video(video)` → `bytes` |
| Download all images of a carousel | `direct.download_carousel_images(video)` |
| Sign your own API URL | `sign_url(url, user_agent)` |
| Generate a raw X-Bogus token | `generate_xbogus(query_string, user_agent)` |
| Parse a TikTok JSON item yourself | `parse_video_from_item(item)` |
| Extract SSR rehydration data from HTML | `parse_rehydration_data(html)` |
| Browser-based scrape when HTTP fails | `TikTokClient.get_user_videos(...)` |

Common downstream applications:
- Influencer analytics & audit tools
- Content trend mining and competitive research
- Brand monitoring (mentions, hashtags)
- Dataset building for ML / academic research
- Personal archival of your own content

---

## Install

```bash
pip install -r requirements.txt
playwright install chromium   # only if you plan to use TikTokClient (the browser fallback)
```

**Dependencies** (`requirements.txt`):
- `httpx[http2]` — async HTTP with HTTP/2
- `pydantic` — typed data models
- `browser_cookie3` — read TikTok cookies from your local Chrome
- `playwright` — optional fallback

**Python 3.10+** required (uses union-type syntax `str | None`).

---

## Quick start

### 1. Pure-HTTP scrape (no browser)

```python
import asyncio
from tiktok_scraper import TikTokDirectClient

async def main():
    client = TikTokDirectClient()
    await client.initialize(auto_import_cookies=True)   # pulls TikTok cookies from Chrome

    # Search
    result = await client.search_videos("cooking tips", count=10)
    for v in result.videos:
        print(f"@{v.author} — {v.views:,} views — {v.engagement_rate}% ER")

    # Full account scrape
    videos = await client.get_user_videos("khaby.lame", count=200)
    print(f"Got {len(videos)} videos from @khaby.lame")

    await client.close()

asyncio.run(main())
```

### 2. Browser fallback (when HTTP isn't enough)

```python
import asyncio
from tiktok_scraper import TikTokClient, TikTokAuth

async def main():
    auth = TikTokAuth()
    await auth.import_cookies_now()              # or auth.start_browser_login()

    client = TikTokClient(auth=auth)
    await client.initialize()

    videos = await client.get_user_videos("nasa", count=500, max_scrolls=80)
    print(f"Got {len(videos)} videos")

    await client.close()
    await auth.close()

asyncio.run(main())
```

### 3. Just sign a URL (no scraping)

```python
from tiktok_scraper import sign_url, generate_xbogus

ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 ..."
signed = sign_url("https://www.tiktok.com/api/search/general/full/?aid=1988&keyword=paris", ua)
print(signed)   # → ...&X-Bogus=DFSzswVOq...

# Or just the token
token = generate_xbogus("aid=1988&keyword=paris", ua)
```

See [`example.py`](example.py) for the full runnable demo.

---

## Two clients, two strategies

| | `TikTokDirectClient` | `TikTokClient` |
|---|---|---|
| **Transport** | Pure `httpx` + X-Bogus | Headless Chromium (Playwright) |
| **Speed** | Fast (no browser startup) | Slow (browser scroll loop) |
| **Detection footprint** | Low (looks like a normal browser request) | Higher (real browser, but with auth cookies) |
| **Best for** | Search, hashtags, reposts, individual lookups, account scraping (default) | Stubborn accounts where HTTP misses videos |
| **Requires Playwright?** | No | Yes |

The default workflow is: **try `TikTokDirectClient` first** — its `get_user_videos` already combines 8 sources (SSR, multi-keyword search, hashtag + bio expansion, reposts). If you still come up short, fall back to `TikTokClient`.

---

## Authentication

TikTok requires session cookies for most endpoints. `TikTokAuth` gives you three ways to get them:

```python
from tiktok_scraper import TikTokAuth

auth = TikTokAuth(cookie_file=".tiktok_cookies.json")  # optional persistence

# Option A — import from your local Chrome (fastest, requires being logged in there)
await auth.import_cookies_now()

# Option B — open a real browser, you log in manually, cookies get saved
await auth.start_browser_login(on_status=lambda msg: print(msg))

# Option C — load previously saved cookies
cookies = await auth.get_active_cookies()
```

Cookies persist to a JSON file so you only authenticate once per session.

---

## Full API reference

### `TikTokDirectClient` — pure-HTTP scraper

```python
client = TikTokDirectClient(auth: TikTokAuth | None = None)
```

| Method | Returns | What it does |
|---|---|---|
| `initialize(auto_import_cookies=True)` | `None` | Open the `httpx.AsyncClient`, import cookies if requested |
| `close()` | `None` | Close the underlying HTTP client |
| `search_videos(keyword, count=30, offset=0)` | `SearchResult` | Paginated keyword search via `/api/search/general/full/` |
| `get_hashtag_videos(hashtag, count=50)` | `list[TikTokVideo]` | Hashtag feed (tries `#tag` then bare `tag`) |
| `get_challenge_detail(challenge_name)` | `dict` | Raw challenge/hashtag metadata |
| `get_user_detail(username)` | `dict \| None` | SSR-extracted profile (followers, secUid, bio, …) |
| `get_user_reposts(sec_uid, count=30)` | `list[TikTokVideo]` | Self-reposts via `/api/repost/item_list/` |
| `get_user_videos(username, count=200)` | `list[TikTokVideo]` | **Full account scrape** — combines SSR, multi-keyword search, hashtag/bio expansion, reposts; deduped by `video_id` and filtered to the target author |
| `get_user_with_reposts(username, count=30)` | `dict` | Profile + recent uploads + reposts in one call |
| `get_video_detail(video_id, author)` | `dict \| None` | Single-video metadata |
| `enrich_single_video(video)` | `None` | Refresh `play_url` / `download_url` on a `TikTokVideo` in place |
| `download_video(video)` | `bytes \| None` | Download the MP4 (auto-refreshes URLs on 403/410) |
| `download_carousel_images(video)` | `list[bytes]` | Download all images of a carousel post |

### `TikTokClient` — Playwright fallback

```python
client = TikTokClient(auth: TikTokAuth | None = None, cookie_file: str | None = None)
```

| Method | Returns | What it does |
|---|---|---|
| `initialize()` | `None` | Start Playwright + open an authenticated Chromium context |
| `get_user_videos(username, count=500, max_scrolls=60, max_idle_scrolls=5)` | `list[TikTokVideo]` | Open `/@user`, scroll to the end, harvest videos from intercepted JSON responses + periodic HTML rehydration parses |
| `close()` | `None` | Close browser + Playwright runtime |

### `TikTokAuth` — cookie & session management

| Method | Returns | What it does |
|---|---|---|
| `import_cookies_now()` | `dict` | One-shot import from your local Chrome via `browser_cookie3` |
| `start_browser_login(on_status=None)` | `bool` | Launch a real browser for manual login, save cookies on success |
| `get_active_cookies()` | `dict \| None` | Load cookies from file (or return cached) |
| `get_authenticated_context()` | `BrowserContext \| None` | Build a Playwright context pre-loaded with auth cookies |
| `close()` | `None` | Shut down any browser resources |
| `extract_tiktok_cookies_from_chrome()` *(module-level)* | `dict` | Raw helper around `browser_cookie3.chrome(domain=".tiktok.com")` |

### `xbogus` — signing primitives

| Function | Returns | What it does |
|---|---|---|
| `generate_xbogus(query, user_agent, timestamp=None)` | `str` | Compute the X-Bogus token for a query string |
| `sign_url(url, user_agent, body="")` | `str` | Append a fresh `&X-Bogus=...` to any TikTok API URL |

### `parser` — pure parsing helpers (no I/O)

| Function | Returns | What it does |
|---|---|---|
| `parse_video_from_item(item)` | `TikTokVideo \| None` | Convert a raw TikTok JSON item into a typed `TikTokVideo` |
| `parse_rehydration_data(html)` | `list[dict]` | Extract `__UNIVERSAL_DATA_FOR_REHYDRATION__` items from an SSR HTML page |
| `parse_search_profiles(entries)` | `list[TikTokProfile]` | Pull profile objects out of search results |
| `detect_content_format(item)` | `tuple[str, int, list[str]]` | Return `(content_format, image_count, image_urls)` — distinguishes `video` / `carousel` / `duet` |

### Data models (`pydantic.BaseModel`)

**`TikTokVideo`**
```
video_id, author, author_sec_uid, description, hashtags,
views, likes, shares, comments, saves, duration,
play_url, download_url, cover_url, sound_name, created_at,
content_format ("video"|"carousel"|"duet"),
main_format ("ugc"|"carousel"),
image_count, image_urls,
engagement_rate  # computed: (likes+comments+shares+saves) / views * 100
```

**`TikTokProfile`**
```
sec_uid, unique_id, nickname, followers, following,
total_likes, video_count, avatar_url
```

**`SearchResult`**
```
videos: list[TikTokVideo]
profiles: list[TikTokProfile]
has_more: bool
cursor: str
```

---

## How the full-account scrape works

TikTok's `/api/post/item_list/` endpoint is dead for unauthenticated web traffic. `get_user_videos()` works around it by tapping **eight** parallel sources and deduping by `video_id`:

1. **SSR profile page** (`/@user`) — `itemList` often embedded
2. **SSR user detail** — `secUid`, follower count, bio (used as further search seeds)
3. **Search `@username`** — exact-author match
4. **Search `username`** — exact-author match (no `@`)
5. **Search `username + <top hashtag>`** ×N — expand via the user's own hashtags
6. **Search `username + <bio keyword>`** ×N — expand via bio text
7. **Hashtag feeds** ×N — pull author-matched items from each top hashtag
8. **Reposts** via `/api/repost/item_list/` — catch self-reposts

Every item is filtered with `author.lower() == username.lower()` so the result is clean.

---

## Project layout

```
tiktok-scraper/
├── __init__.py           # public exports
├── direct_client.py      # TikTokDirectClient — pure HTTP + X-Bogus
├── tiktok_client.py      # TikTokClient — Playwright fallback
├── auth.py               # TikTokAuth — cookie/session management
├── xbogus.py             # X-Bogus signing (RC4 + custom Base64)
├── parser.py             # JSON / SSR parsing helpers
├── models.py             # Pydantic models
├── intercept_signing.py  # dev tool — capture real X-Bogus from a browser to validate the RE
├── example.py            # runnable demo
└── requirements.txt
```

---

## Limitations & caveats

- **TikTok updates their signing scheme.** X-Bogus has been stable for a while, but if requests start returning empty `data`, the magic constants in `xbogus.py` may need refreshing. Use `intercept_signing.py` to capture fresh signatures from a real browser and compare.
- **Cookies expire.** If a long-running scrape suddenly returns nothing, re-run `auth.import_cookies_now()`.
- **Rate limits are real.** The clients add randomized delays (`0.2–0.5s` between paginated calls), but if you hammer the API you'll get throttled. Scale your `count` and the number of concurrent users you scrape.
- **HTTP/2 + http2 extras required.** Make sure you installed `httpx[http2]`, not bare `httpx`.

---

## Legal & ethical use

This library scrapes a **public web interface**. Use it responsibly:

- Don't scrape private/unlisted content.
- Don't redistribute downloaded videos without the creator's permission.
- Respect TikTok's Terms of Service in your jurisdiction.
- Heavy automated traffic against TikTok may violate their ToS and risks getting your account/IP blocked.

This project is for research, personal archival, and authorized analytics work. The author assumes no liability for misuse.

---

## License

MIT — see [LICENSE](LICENSE).

## Contributing

Issues and PRs welcome. If you're submitting a signing fix, please include a captured request from `intercept_signing.py` as evidence.
