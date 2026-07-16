import asyncio
import json
import logging
from datetime import datetime
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

# Configure logging for debugging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

# --- Configuration ---
# Update START_URL if the resolved URL below is incomplete or relative.
START_URL = "https://public.cdpehs.com/KYEnvPBL/(S(ffon2tol2zzd4utfzqil5ybu))/VW_PUBLIC_EST_INSP/ShowVW_PUBLIC_EST_INSPTable.aspx?COUNTY=8"

ITEM_SELECTOR = "#VW_PUBLIC_EST_INSPTableControlGrid tbody tr"
TITLE_SELECTOR = "td.ttc:nth-child(1)"
DATE_SELECTOR = "td.ttc:nth-child(4)"
NEXT_PAGE_SELECTOR = "input[name='VW_PUBLIC_EST_INSPPagination$_NextPage']"

# ----------------------


def scrape_page(html_content: str) -> list:
    """
    Parse a single page of HTML content and extract items.

    Args:
        html_content: Full HTML source of the current page.

    Returns:
        List of dicts with extracted fields.
    """
    logger.info("Parsing page HTML with BeautifulSoup...")
    soup = BeautifulSoup(html_content, "html.parser")
    rows = soup.select(ITEM_SELECTOR)
    logger.info("Found %d row(s) on current page.", len(rows))

    items = []
    for idx, row in enumerate(rows):
        try:
            title_el = row.select_one(TITLE_SELECTOR)
            date_el = row.select_one(DATE_SELECTOR)

            title = title_el.get_text(strip=True) if title_el else ""
            date_val = date_el.get_text(strip=True) if date_el else ""

            # Collect all cell text for a full-row record
            all_cells = [td.get_text(strip=True) for td in row.find_all("td")]

            if title or date_val or any(all_cells):
                item = {
                    "title": title,
                    "date": date_val,
                    "all_cells": all_cells,
                }
                items.append(item)
                logger.debug("Row %d extracted: title=%r, date=%r", idx, title, date_val)
            else:
                logger.debug("Row %d appears empty, skipping.", idx)
        except Exception as row_err:
            logger.warning("Error extracting row %d: %s", idx, str(row_err))

    logger.info("Extracted %d item(s) from this page.", len(items))
    return items


async def advance_page(page) -> bool:
    """
    Attempt to navigate to the next page by clicking the pagination button.

    This handles ASP.NET WebForms postback pagination where the next-page
    control is a submit input that triggers a __doPostBack or form submit.

    Args:
        page: The Playwright page object at the current page state.

    Returns:
        True if navigation to the next page succeeded, False if no next page.
    """
    logger.info("Checking for next page button with selector: %s", NEXT_PAGE_SELECTOR)
    try:
        next_btn = page.locator(NEXT_PAGE_SELECTOR)
        count = await next_btn.count()
        logger.info("Next page button count: %d", count)

        if count == 0:
            logger.info("No next page button found. Reached last page.")
            return False

        # Check if the button is disabled (common pattern in ASP.NET grids)
        is_disabled = await next_btn.get_attribute("disabled")
        if is_disabled is not None:
            logger.info("Next page button is disabled. Reached last page.")
            return False

        # Capture current URL and a snapshot of content for change detection
        current_url = page.url
        current_html_snippet = await page.inner_html("body")
        pre_nav_checksum = hash(current_html_snippet[:2000])
        logger.info("Pre-click page checksum (first 2000 chars): %d", pre_nav_checksum)

        logger.info("Clicking next page button...")
        # For ASP.NET postback: click triggers form submission; wait for navigation/load
        async with page.expect_response(lambda r: r.status == 200, timeout=30000):
            await next_btn.click()

        # Wait for the network to settle
        await page.wait_for_load_state("networkidle", timeout=30000)
        logger.info("Page loaded after pagination click.")

        # Verify that content actually changed (postback may render same page on last page)
        new_html_snippet = await page.inner_html("body")
        post_nav_checksum = hash(new_html_snippet[:2000])
        logger.info("Post-click page checksum (first 2000 chars): %d", post_nav_checksum)

        if pre_nav_checksum == post_nav_checksum:
            logger.info("Page content unchanged after next-page click. Assuming last page.")
            return False

        return True

    except Exception as nav_err:
        logger.warning("advance_page error: %s. Attempting infinite scroll fallback...", str(nav_err))
        return await _infinite_scroll_fallback(page)


async def _infinite_scroll_fallback(page) -> bool:
    """
    Fallback: attempt to load more content via infinite scroll.

    Scrolls to the bottom of the page and waits for new content to load.
    Returns True if new content appeared, False otherwise.
    """
    logger.info("Trying infinite scroll fallback...")
    try:
        pre_height = await page.evaluate("document.body.scrollHeight")
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(2000)
        await page.wait_for_load_state("networkidle", timeout=15000)
        post_height = await page.evaluate("document.body.scrollHeight")

        if post_height > pre_height:
            logger.info("Infinite scroll loaded more content (height: %d -> %d).", pre_height, post_height)
            return True
        else:
            logger.info("No additional content after scroll. End of data.")
            return False
    except Exception as scroll_err:
        logger.warning("Infinite scroll fallback failed: %s", str(scroll_err))
        return False


async def run_scraper():
    """
    Main scraper entry point. Launches Playwright, applies stealth,
    iterates through all pages, and saves results to JSON.
    """
    all_results = []
    page_number = 1
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = f"results_{timestamp}.json"

    logger.info("Starting scraper. Target URL: %s", START_URL)
    logger.info("Output will be saved to: %s", output_file)

    async with async_playwright() as pw:
        logger.info("Launching Chromium browser (headless)...")
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
            ]
        )

        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            java_script_enabled=True,
        )

        page = await context.new_page()

        # Apply Playwright Stealth (v2 API)
        logger.info("Applying Playwright Stealth (v2 API)...")
        await Stealth().apply_stealth_async(page)
        logger.info("Stealth applied successfully.")

        # Navigate to the start URL
        logger.info("Navigating to: %s", START_URL)
        try:
            await page.goto(START_URL, wait_until="networkidle", timeout=60000)
            logger.info("Initial page loaded successfully.")
        except Exception as nav_err:
            logger.error("Failed to load start URL: %s", str(nav_err))
            await browser.close()
            return

        # Pagination loop
        while True:
            logger.info("--- Scraping page %d ---", page_number)
            try:
                html_content = await page.content()
                page_items = scrape_page(html_content)
                all_results.extend(page_items)
                logger.info(
                    "Page %d: %d item(s) scraped. Running total: %d",
                    page_number, len(page_items), len(all_results)
                )
            except Exception as scrape_err:
                logger.error("Error scraping page %d: %s", page_number, str(scrape_err))
                break

            # Advance to next page
            has_next = await advance_page(page)
            if not has_next:
                logger.info("No more pages. Stopping pagination loop.")
                break

            page_number += 1
            logger.info("Advanced to page %d.", page_number)

            # Safety limit to prevent runaway loops
            if page_number > 10000:
                logger.warning("Reached page limit safety cap (10000). Stopping.")
                break

        logger.info("Scraping complete. Total items collected: %d", len(all_results))
        await browser.close()
        logger.info("Browser closed.")

    # Save results to JSON
    try:
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(all_results, f, ensure_ascii=False, indent=2)
        logger.info("Results saved to %s", output_file)
    except Exception as save_err:
        logger.error("Failed to save results: %s", str(save_err))


if __name__ == "__main__":
    asyncio.run(run_scraper())