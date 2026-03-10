"""
Intercept TikTok's signing mechanisms (X-Bogus, msToken, _signature).

This script launches a browser, navigates TikTok, and captures:
1. The JS code that generates X-Bogus
2. The msToken generation flow
3. All signed API requests with their parameters

Run this to analyze the signing algorithms before reimplementing in Python.
"""

import asyncio
import json
import re
import logging
from pathlib import Path
from playwright.async_api import async_playwright, Page, Request, Response

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
logger = logging.getLogger(__name__)

OUTPUT_DIR = Path(__file__).parent / "re_captures"
OUTPUT_DIR.mkdir(exist_ok=True)


async def capture_signing_data():
    """Intercept all API calls and extract signing parameters."""

    captured_requests = []
    captured_scripts = []

    async def on_request(request: Request):
        url = request.url
        if "/api/" in url or "bogus" in url.lower() or "msToken" in url:
            params = dict(request.url.split("?")[1].split("&")) if "?" in url else {}
            # Parse params properly
            if "?" in url:
                param_str = url.split("?", 1)[1]
                params = {}
                for p in param_str.split("&"):
                    if "=" in p:
                        k, v = p.split("=", 1)
                        params[k] = v

            entry = {
                "url": url.split("?")[0],
                "method": request.method,
                "params": params,
                "headers": dict(request.headers),
            }

            # Highlight signing params
            signing_params = {}
            for key in ["X-Bogus", "x-bogus", "msToken", "ms_token", "_signature", "x-tt-params"]:
                if key in params:
                    signing_params[key] = params[key]
                # Also check headers
                for hk, hv in request.headers.items():
                    if key.lower() == hk.lower():
                        signing_params[f"header_{key}"] = hv

            if signing_params:
                entry["signing_params"] = signing_params
                logger.info(f"SIGNED REQUEST: {entry['url']}")
                for k, v in signing_params.items():
                    logger.info(f"  {k} = {v[:80]}...")

            captured_requests.append(entry)

    async def on_response(response: Response):
        url = response.url
        content_type = response.headers.get("content-type", "")

        # Capture JavaScript files that might contain signing logic
        if "javascript" in content_type or url.endswith(".js"):
            try:
                body = await response.text()
                # Look for X-Bogus generation code
                xbogus_indicators = [
                    "X-Bogus", "x-bogus", "XBogus",
                    "msToken", "ms_token",
                    "_signature",
                    "sign_v", "get_sign",
                    "webmssdk", "byted_acrawler",
                    "signer",
                ]
                matches = [ind for ind in xbogus_indicators if ind in body]
                if matches:
                    filename = url.split("/")[-1].split("?")[0][:50]
                    script_path = OUTPUT_DIR / f"js_{filename}"
                    script_path.write_text(body)
                    logger.info(f"CAPTURED JS with signing code: {filename} (matches: {matches})")
                    captured_scripts.append({
                        "url": url,
                        "filename": str(script_path),
                        "matches": matches,
                        "size": len(body),
                    })
            except Exception:
                pass

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=False)  # headless=False to see what's happening
    context = await browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1920, "height": 1080},
    )

    page = await context.new_page()

    # Hook into console to catch any signing-related logs
    page.on("request", on_request)
    page.on("response", on_response)

    # Also inject JS to intercept XMLHttpRequest and fetch
    await page.add_init_script("""
    // Intercept XMLHttpRequest to capture params before sending
    const origOpen = XMLHttpRequest.prototype.open;
    const origSend = XMLHttpRequest.prototype.send;

    XMLHttpRequest.prototype.open = function(method, url, ...args) {
        this._capturedUrl = url;
        this._capturedMethod = method;
        return origOpen.call(this, method, url, ...args);
    };

    XMLHttpRequest.prototype.send = function(body) {
        if (this._capturedUrl && this._capturedUrl.includes('/api/')) {
            console.log('__SIGNING_INTERCEPT_XHR__:' + JSON.stringify({
                url: this._capturedUrl,
                method: this._capturedMethod,
                body: body ? body.substring(0, 500) : null
            }));
        }
        return origSend.call(this, body);
    };

    // Intercept fetch
    const origFetch = window.fetch;
    window.fetch = function(url, options) {
        if (typeof url === 'string' && url.includes('/api/')) {
            console.log('__SIGNING_INTERCEPT_FETCH__:' + JSON.stringify({
                url: url,
                method: options?.method || 'GET',
                headers: options?.headers || {}
            }));
        }
        return origFetch.call(this, url, options);
    };
    """)

    console_logs = []
    page.on("console", lambda msg: console_logs.append(msg.text) if "__SIGNING_INTERCEPT" in msg.text else None)

    # Navigate and trigger API calls
    logger.info("Navigating to TikTok homepage...")
    await page.goto("https://www.tiktok.com", wait_until="networkidle", timeout=60000)
    await asyncio.sleep(3)

    logger.info("Navigating to search...")
    await page.goto("https://www.tiktok.com/search?q=cooking", wait_until="networkidle", timeout=60000)
    await asyncio.sleep(3)

    # Scroll to trigger more API calls
    for i in range(3):
        logger.info(f"Scrolling ({i+1}/3)...")
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(2)

    # Try to extract the signing function directly from the page context
    logger.info("Attempting to extract signing functions from page context...")
    signing_analysis = await page.evaluate("""
    () => {
        const result = {
            hasWebmssdk: typeof window.webmssdk !== 'undefined',
            hasByteAcrawler: typeof window.byted_acrawler !== 'undefined',
            hasSigner: typeof window._signer !== 'undefined',
            windowKeys: Object.keys(window).filter(k =>
                k.toLowerCase().includes('sign') ||
                k.toLowerCase().includes('bogus') ||
                k.toLowerCase().includes('token') ||
                k.toLowerCase().includes('encrypt') ||
                k.toLowerCase().includes('mssdk') ||
                k.toLowerCase().includes('acrawler') ||
                k.toLowerCase().includes('byted')
            ),
        };

        // Try to find the signing module in webpack chunks
        if (window.webpackChunk_N_E) {
            result.webpackChunks = window.webpackChunk_N_E.length;
        }

        return result;
    }
    """)
    logger.info(f"Signing analysis from page context: {json.dumps(signing_analysis, indent=2)}")

    # Save all captured data
    output = {
        "requests": captured_requests,
        "scripts": captured_scripts,
        "console_intercepts": console_logs,
        "signing_analysis": signing_analysis,
    }

    output_file = OUTPUT_DIR / "signing_capture.json"
    output_file.write_text(json.dumps(output, indent=2, default=str))
    logger.info(f"Saved capture data to {output_file}")
    logger.info(f"Captured {len(captured_requests)} API requests, {len(captured_scripts)} JS files")

    # Summary of signing params found
    all_signing = {}
    for req in captured_requests:
        if "signing_params" in req:
            for k, v in req["signing_params"].items():
                if k not in all_signing:
                    all_signing[k] = []
                all_signing[k].append(v)

    logger.info("\n=== SIGNING PARAMETERS SUMMARY ===")
    for param, values in all_signing.items():
        logger.info(f"  {param}: {len(values)} occurrences, sample length: {len(values[0])}")
        logger.info(f"    First value: {values[0][:100]}")

    await browser.close()
    await pw.stop()

    return output


if __name__ == "__main__":
    asyncio.run(capture_signing_data())
