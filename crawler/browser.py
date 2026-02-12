from __future__ import annotations


def render_page_html(url: str, timeout_ms: int = 10_000) -> str:
    """Render a page using Playwright if available.

    Raises RuntimeError if Playwright is not installed.
    """

    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("render-js requested but playwright is not installed") from exc

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(url, timeout=timeout_ms, wait_until="networkidle")
            return page.content()
        finally:
            browser.close()
