"""
TikTok Scraper — Usage Examples

Two modes available:
  1. TikTokDirectClient — Pure HTTP, no browser (uses X-Bogus RE)
  2. TikTokClient — Browser-based with API interception (fallback)
"""

import asyncio
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")


async def demo_direct_client():
    """Mode 1: Direct HTTP — No browser, pure reverse engineering."""
    from tiktok_scraper_standalone import TikTokDirectClient

    print("\n=== DIRECT CLIENT (No Browser) ===\n")

    client = TikTokDirectClient()
    await client.initialize(auto_import_cookies=True)

    # Search videos
    results = await client.search_videos("cooking tips", count=5)
    print(f"Found {len(results.videos)} videos:")
    for v in results.videos:
        print(f"  @{v.author}: {v.description[:50]}... | {v.views:,} views | {v.engagement_rate}% ER")

    # Get video details
    if results.videos:
        detail = await client.get_video_detail(results.videos[0].video_id)
        if detail:
            print(f"\nDetail: {detail.description[:80]}...")

    await client.close()


async def demo_browser_client():
    """Mode 2: Browser-based — API interception (fallback if direct fails)."""
    from tiktok_scraper_standalone import TikTokClient, TikTokAuth

    print("\n=== BROWSER CLIENT (Fallback) ===\n")

    auth = TikTokAuth()
    await auth.import_cookies_now()

    client = TikTokClient(auth=auth)
    await client.initialize()

    results = await client.search_videos("cooking tips", count=5)
    print(f"Found {len(results.videos)} videos:")
    for v in results.videos:
        print(f"  @{v.author}: {v.description[:50]}... | {v.views:,} views | {v.engagement_rate}% ER")

    await client.close()
    await auth.close()


async def demo_xbogus():
    """Demo: Generate X-Bogus signatures without making requests."""
    from tiktok_scraper_standalone import generate_xbogus, sign_url

    print("\n=== X-BOGUS GENERATION ===\n")

    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    query = "aid=1988&count=10&keyword=cooking&offset=0"

    xbogus = generate_xbogus(query, ua)
    print(f"Query:   {query}")
    print(f"X-Bogus: {xbogus}")
    print(f"Length:  {len(xbogus)} chars")

    signed = sign_url(f"https://www.tiktok.com/api/search/general/full/?{query}", ua)
    print(f"\nSigned URL: {signed}")


if __name__ == "__main__":
    # Run X-Bogus demo first (no network needed)
    asyncio.run(demo_xbogus())

    # Then try direct client
    asyncio.run(demo_direct_client())
