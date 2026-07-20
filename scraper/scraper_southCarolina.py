import asyncio
import json
import re
import logging
from datetime import datetime
from urllib.parse import urljoin, urlparse

from playwright.async_api import async_playwright
from playwright_stealth import Stealth
from bs4 import BeautifulSoup

# ──────────────────────────────────────────────────────────
# CONFIGURATION — injected from prompt inputs
# ──────────────────────────────────────────────────────────
START_URL = 'https://apps.dhec.sc.gov/Environment/FoodGrades/'

SELECTORS = {
    "item_selectors": "#restaurants_dataTable tbody tr, table.dataTable tbody tr",
    "title_selectors": "td.doclink.sorting_1, td[width='30%']",
    "date_selectors": "td[width='10%']",
    "search_button_selectors": "#btnSearch, input[value='Search']",
    "next_page_selectors": "#restaurants_next, a.paginate_button.next"
}

# ──────────────────────────────────────────────────────────
# LOGGING SETUP
# ──────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────
# VALIDATION
# ──────────────────────────────────────────────────────────
def _validate_start_url(url: str) -> str:
    """Raise ValueError if START_URL is missing or malformed."""
    if not url or not url.strip():
        raise ValueError("TARGET_URL is missing or invalid")
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"TARGET_URL is missing or invalid: {url!r}")
    return url.strip()


def _resolve_url(base: str, href: str) -> str:
    """Resolve relative href against base URL."""
    if not href:
        return ""
    return urljoin(base, href)


# ──────────────────────────────────────────────────────────
# PAGE SCRAPER
# ──────────────────────────────────────────────────────────
async def scrape_page(page) -> list:
    """
    Extract structured records from the current page state.

    Extraction order:
    1. Wait for key selector (item_selectors) to appear.
    2. Grab full page HTML.
    3. Parse with BeautifulSoup.
    4. Iterate over item containers using SELECTORS.
    5. Extract each field using child selectors.
    """
    records = []

    item_selector   = SELECTORS.get("item_selectors", "")
    title_selector  = SELECTORS.get("title_selectors", "")
    date_selector   = SELECTORS.get("date_selectors", "")

    # Wait for item container to appear (up to 15 s)
    if item_selector:
        try:
            await page.wait_for_selector(item_selector, timeout=15_000)
            logger.info("Item selector matched: %r", item_selector)
        except Exception as e:
            logger.warning("Item selector timed out or failed (%r): %s", item_selector, e)

    html = await page.content()
    soup = BeautifulSoup(html, "html.parser")

    # ── Locate item rows ──────────────────────────────────
    rows = []
    if item_selector:
        # Try each comma-separated candidate in order
        for sel in [s.strip() for s in item_selector.split(",")]:
            rows = soup.select(sel)
            if rows:
                logger.info("Item rows found with selector %r: %d rows", sel, len(rows))
                break
            else:
                logger.warning("Item selector yielded no rows: %r", sel)

    if not rows:
        logger.warning("No item rows found on page; returning empty list.")
        return records

    for row in rows:
        # Skip header rows (th elements only)
        if row.find("th") and not row.find("td"):
            continue

        record = {
            "name":            None,
            "address":         None,
            "city":            None,
            "state":           None,
            "county":          None,
            "lastInspected":   None,
            "lastInspectionId": None,
            "details":         {}
        }

        # ── Name / title field ────────────────────────────
        name_val = None
        if title_selector:
            for sel in [s.strip() for s in title_selector.split(",")]:
                el = row.select_one(sel)
                if el:
                    name_val = el.get_text(separator=" ", strip=True)
                    logger.info("Title matched with %r: %r", sel, name_val[:80] if name_val else "")
                    # Check for an inspection-detail link
                    link_el = el.find("a", href=True)
                    if link_el:
                        record["lastInspectionId"] = link_el["href"].strip()
                    break
            if not name_val:
                logger.warning("Title selector yielded no text for a row; title_selector=%r", title_selector)
        record["name"] = name_val

        # ── Date field ────────────────────────────────────
        date_val = None
        if date_selector:
            for sel in [s.strip() for s in date_selector.split(",")]:
                el = row.select_one(sel)
                if el:
                    date_val = el.get_text(strip=True)
                    logger.info("Date matched with %r: %r", sel, date_val)
                    break
            if not date_val:
                logger.warning("Date selector yielded no text; date_selector=%r", date_selector)
        record["lastInspected"] = date_val

        # ── Remaining <td> cells → details ───────────────
        cells = row.find_all("td")
        details = {}
        for idx, cell in enumerate(cells):
            cell_text = cell.get_text(separator=" ", strip=True)
            if cell_text:
                details[f"col_{idx}"] = cell_text
        if details:
            record["details"] = details
            # Heuristic: try to infer address/city/state/county from columns
            # Common table layout for SC Food Grades: name | address | city | grade | date
            col_keys = list(details.keys())
            if len(col_keys) >= 2 and record["address"] is None:
                record["address"] = details.get("col_1")
            if len(col_keys) >= 3 and record["city"] is None:
                record["city"] = details.get("col_2")
            if len(col_keys) >= 4 and record["state"] is None:
                record["state"] = "SC"
            if len(col_keys) >= 5 and record["county"] is None:
                record["county"] = details.get("col_4")

        # Only add rows that have at least one meaningful field
        if any(v for v in [record["name"], record["lastInspected"]] if v):
            records.append(record)

    logger.info("scrape_page() extracted %d records.", len(records))
    return records


async def run_search(page) -> None:
    """Click the search button so the result table is rendered before scraping."""
    search_selector = SELECTORS.get("search_button_selectors", "")
    if not search_selector:
        logger.info("No search_button_selectors configured; skipping search click.")
        return

    for sel in [s.strip() for s in search_selector.split(",")]:
        try:
            button = page.locator(sel).first
            if not await button.count():
                logger.info("Search button selector not found: %r", sel)
                continue

            logger.info("Clicking search button: %r", sel)
            try:
                async with page.expect_navigation(wait_until="networkidle", timeout=15_000):
                    await button.click()
            except Exception:
                await button.click()
                await page.wait_for_load_state("networkidle", timeout=15_000)

            item_selector = SELECTORS.get("item_selectors", "")
            if item_selector:
                try:
                    await page.wait_for_selector(item_selector, timeout=15_000)
                except Exception as e:
                    logger.warning("Search completed but item selector did not appear yet (%r): %s", item_selector, e)

            logger.info("Search submitted; current URL: %s", page.url)
            return
        except Exception as e:
            logger.warning("run_search() error with selector %r: %s", sel, e)
            continue

    logger.info("No operable search button found; continuing without click.")


# ──────────────────────────────────────────────────────────
# PAGINATION HANDLER
# ──────────────────────────────────────────────────────────
async def advance_page(page, current_url: str):
    """
    Attempt to navigate to the next page.

    Strategy (in order):
    1. Click the next-page button identified by SELECTORS[next_page_selectors].
    2. If clicking fails or button is disabled, return None to signal end-of-pagination.

    Returns the new URL string on success, or None when no further pages exist.
    """
    next_sel = SELECTORS.get("next_page_selectors", "")
    if not next_sel:
        logger.info("No next_page_selectors configured; single-page scrape.")
        return None

    for sel in [s.strip() for s in next_sel.split(",")]:
        try:
            btn = page.locator(sel).first
            if not await btn.count():
                logger.info("Next-page selector not found on page: %r", sel)
                continue

            # Check for disabled state (DataTables adds 'disabled' class)
            classes = await btn.get_attribute("class") or ""
            if "disabled" in classes:
                logger.info("Next-page button is disabled (%r); end of pagination.", sel)
                return None

            logger.info("Clicking next-page button: %r", sel)
            await btn.click()
            # Wait for the table to re-render
            await page.wait_for_load_state("networkidle", timeout=15_000)
            new_url = page.url
            logger.info("Advanced to next page: %s", new_url)
            return new_url

        except Exception as e:
            logger.warning("advance_page() error with selector %r: %s", sel, e)
            continue

    logger.info("No operable next-page button found; end of pagination.")
    return None


# ──────────────────────────────────────────────────────────
# MAIN ORCHESTRATOR
# ──────────────────────────────────────────────────────────
async def main():
    validated_url = _validate_start_url(START_URL)
    logger.info("Starting scrape from: %s", validated_url)

    all_records = []
    seen_keys   = set()  # for deduplication

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()

        # Apply stealth patches
        await Stealth().apply_stealth_async(page)
        logger.info("Stealth applied.")

        # Navigate to START_URL
        try:
            logger.info("Loading START_URL: %s", validated_url)
            await page.goto(validated_url, wait_until="networkidle", timeout=60_000)
            logger.info("Page loaded: %s", page.url)
        except Exception as e:
            logger.error("Failed to load START_URL: %s", e)
            await browser.close()
            return

        try:
            await run_search(page)
        except Exception as e:
            logger.warning("Search button click step failed; continuing to scrape current page state: %s", e)

        current_url = page.url
        page_num    = 1

        while True:
            logger.info("Scraping page %d at URL: %s", page_num, current_url)
            try:
                records = await scrape_page(page)
            except Exception as e:
                logger.error("scrape_page() raised an exception on page %d: %s", page_num, e)
                break

            for rec in records:
                # Deduplicate by (name, lastInspected)
                key = (rec.get("name") or "", rec.get("lastInspected") or "")
                if key not in seen_keys:
                    seen_keys.add(key)
                    all_records.append(rec)

            logger.info("Total unique records so far: %d", len(all_records))

            # Attempt pagination
            try:
                next_url = await advance_page(page, current_url)
            except Exception as e:
                logger.warning("advance_page() raised an exception: %s", e)
                next_url = None

            if next_url is None:
                logger.info("Pagination complete after %d pages.", page_num)
                break

            # Guard: stop if URL did not change (infinite-loop protection)
            if next_url == current_url:
                logger.warning("URL did not change after pagination; stopping.")
                break

            current_url = next_url
            page_num   += 1

        await browser.close()

    # ── Save results ──────────────────────────────────────
    timestamp   = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = f"results_{timestamp}.json"
    with open(output_file, "w", encoding="utf-8") as fh:
        json.dump(all_records, fh, indent=2, ensure_ascii=False)

    logger.info("Saved %d records to %s", len(all_records), output_file)
    print(f"Done. {len(all_records)} records written to {output_file}")


if __name__ == "__main__":
    asyncio.run(main())