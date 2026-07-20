import asyncio
import json
import logging
import re
import time
from datetime import datetime
from urllib.parse import urljoin, urlparse, urlunparse

from playwright.async_api import async_playwright
from playwright_stealth import Stealth
from bs4 import BeautifulSoup

# ──────────────────────────────────────────────
#  CONFIGURATION  (auto-generated — do not edit)
# ──────────────────────────────────────────────
START_URL = 'https://public.cdpehs.com/KYEnvPBL/(S(qdyrwmb2na4xj4d1s5u1cdwf))/VW_PUBLIC_EST_INSP/ShowVW_PUBLIC_EST_INSPTable.aspx?COUNTY=8'

SELECTORS = {
    "item_selectors": "#VW_PUBLIC_EST_INSPTableControlGrid tbody tr",
    "title_selectors": "td.ttc:nth-child(1)",
    "date_selectors": "td.ttc:nth-child(4)",
    "next_page_selectors": "input[name='VW_PUBLIC_EST_INSPPagination$_NextPage']"
}

# ──────────────────────────────────────────────
#  LOGGING
# ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ──────────────────────────────────────────────
#  HELPERS
# ──────────────────────────────────────────────

def resolve_url(base: str, href: str) -> str:
    """Resolve a potentially relative URL against a base URL."""
    if not href:
        return ""
    parsed_base = urlparse(base)
    parsed_href = urlparse(href)
    if parsed_href.scheme in ("http", "https"):
        return href
    # Handle relative paths
    resolved = urljoin(base, href)
    return resolved


def decode_start_url(raw: str):
    """
    Detect whether START_URL is base64-encoded HTML or an actual HTTP(S) URL.
    Returns (actual_url, html_content_or_None, form_action_or_None).
    """
    import base64 as _b64

    if not raw:
        raise ValueError("TARGET_URL is missing or invalid")

    # Quick check: is it a plain URL?
    stripped = raw.strip()
    if stripped.startswith("http://") or stripped.startswith("https://"):
        return stripped, None, None

    # Try base64 decode
    try:
        padding = 4 - len(stripped) % 4
        padded = stripped + ("=" * (padding % 4))
        decoded_bytes = _b64.b64decode(padded)
        decoded_str = decoded_bytes.decode("utf-8", errors="replace")
        if "<html" in decoded_str.lower() or "<!doctype" in decoded_str.lower():
            log.info("START_URL decoded as base64-encoded HTML.")
            # Extract form action
            form_action_match = re.search(
                r'<form[^>]+action=["\']([^"\']+)["\']',
                decoded_str,
                re.IGNORECASE,
            )
            form_action = form_action_match.group(1) if form_action_match else None
            log.info(f"Form action found: {form_action}")
            return raw, decoded_str, form_action
    except Exception as exc:
        log.warning(f"base64 decode attempt failed: {exc}")

    raise ValueError(f"TARGET_URL is missing or invalid: cannot determine URL from input")


# ──────────────────────────────────────────────
#  SCRAPER CORE
# ──────────────────────────────────────────────

seen_records = set()
all_records = []


def make_record_key(record: dict) -> str:
    return "|".join([
        str(record.get("name", "")),
        str(record.get("address", "")),
        str(record.get("lastInspectionId", "")),
    ])


async def scrape_page(page, base_url: str) -> list:
    """Extract all records from the current page state."""
    records = []

    item_sel = SELECTORS.get("item_selectors", "")
    title_sel = SELECTORS.get("title_selectors", "")
    date_sel = SELECTORS.get("date_selectors", "")

    # Wait for the item container selector
    if item_sel:
        try:
            await page.wait_for_selector(item_sel, timeout=15000)
            log.info(f"Selector matched: item_selectors = {item_sel!r}")
        except Exception as exc:
            log.warning(f"Selector FAILED to appear: item_selectors = {item_sel!r} | {exc}")

    html = await page.content()
    soup = BeautifulSoup(html, "html.parser")

    items = []
    if item_sel:
        items = soup.select(item_sel)
        if items:
            log.info(f"Found {len(items)} item(s) using item_selectors")
        else:
            log.warning(f"No items found with selector: {item_sel!r}")

    for row in items:
        record = {
            "name": None,
            "address": None,
            "city": None,
            "state": None,
            "county": None,
            "lastInspected": None,
            "lastInspectionId": None,
            "details": {},
        }

        # --- name / title ---
        if title_sel:
            el = row.select_one(title_sel)
            if el:
                record["name"] = el.get_text(strip=True) or None
                log.info(f"title_selectors matched: {record['name']}")
            else:
                # Fallback: first td
                el = row.select_one("td")
                if el:
                    record["name"] = el.get_text(strip=True) or None
                    log.info(f"title fallback matched: {record['name']}")
                else:
                    log.warning(f"title_selectors FAILED: {title_sel!r}")
        else:
            el = row.select_one("td")
            if el:
                record["name"] = el.get_text(strip=True) or None

        # --- date / lastInspected ---
        if date_sel:
            el = row.select_one(date_sel)
            if el:
                record["lastInspected"] = el.get_text(strip=True) or None
                log.info(f"date_selectors matched: {record['lastInspected']}")
            else:
                log.warning(f"date_selectors FAILED: {date_sel!r}")

        # --- Extract all other td cells as details ---
        all_tds = row.select("td")
        td_texts = [td.get_text(strip=True) for td in all_tds]

        # Try to extract common fields from column positions
        # Columns observed from the HTML structure:
        # 0: name/title, 1: address, 2: city/state, 3: date, 4+: other
        if len(td_texts) > 1:
            record["address"] = td_texts[1] if len(td_texts) > 1 else None
        if len(td_texts) > 2:
            city_state = td_texts[2] if len(td_texts) > 2 else None
            if city_state:
                # Try to split "City, ST" format
                cs_parts = city_state.split(",")
                if len(cs_parts) >= 2:
                    record["city"] = cs_parts[0].strip()
                    record["state"] = cs_parts[1].strip()
                else:
                    record["city"] = city_state.strip()

        # Additional td values beyond standard fields go into details
        for idx, td in enumerate(all_tds):
            if idx not in (0, 1, 2, 3):
                link_el = td.select_one("a")
                if link_el:
                    href = link_el.get("href", "")
                    if href:
                        resolved = resolve_url(base_url, href)
                        # Try to extract inspection ID from URL or link text
                        id_match = re.search(r'[Ii][Dd]=([\w-]+)', resolved)
                        if id_match and not record["lastInspectionId"]:
                            record["lastInspectionId"] = id_match.group(1)
                        record["details"][f"link_col_{idx}"] = resolved
                cell_text = td.get_text(strip=True)
                if cell_text:
                    record["details"][f"col_{idx}"] = cell_text

        # Try to get inspection link/ID from any anchor in the row
        if not record["lastInspectionId"]:
            for a in row.select("a"):
                href = a.get("href", "")
                if href:
                    id_match = re.search(r'[Ii][Nn][Ss][Pp][Ii][Dd]=([\w-]+)|[Ii][Dd]=([\w-]+)', href)
                    if id_match:
                        record["lastInspectionId"] = id_match.group(1) or id_match.group(2)
                        break

        # Only add non-empty rows (skip header rows / empty rows)
        if record["name"] or record["address"]:
            records.append(record)

    log.info(f"scrape_page() extracted {len(records)} record(s)")
    return records


async def advance_page(page, current_url: str, form_action: str = None) -> bool:
    """
    Attempt to navigate to the next page.
    Returns True if navigation succeeded, False if no next page found.
    """
    next_sel = SELECTORS.get("next_page_selectors", "")

    if next_sel:
        try:
            next_btn = await page.query_selector(next_sel)
            if next_btn:
                is_disabled = await next_btn.get_attribute("disabled")
                is_hidden = not await next_btn.is_visible()
                if is_disabled or is_hidden:
                    log.info("Next page button found but is disabled/hidden — last page reached.")
                    return False
                log.info(f"Clicking next page button: {next_sel!r}")
                async with page.expect_load_state("networkidle", timeout=20000):
                    await next_btn.click()
                log.info("Navigated to next page via button click.")
                return True
            else:
                log.info(f"Next page selector not found on page: {next_sel!r}")
                return False
        except Exception as exc:
            log.warning(f"advance_page error with selector {next_sel!r}: {exc}")
            return False

    log.info("No next_page_selectors defined — pagination complete.")
    return False


async def main():
    raw_start = START_URL
    if not raw_start or not raw_start.strip():
        raise ValueError("TARGET_URL is missing or invalid")

    actual_url, html_content, form_action = decode_start_url(raw_start)

    # If START_URL was base64 HTML, we need to determine the real navigation URL
    nav_url = None
    if html_content and form_action:
        # Resolve form action — but we need a base to resolve against
        # Since START_URL is encoded HTML (no real base URL), log warning
        log.warning(
            "START_URL is base64-encoded HTML, not a navigable URL. "
            "Cannot perform browser navigation. "
            "Extracting data from decoded HTML content directly using BeautifulSoup."
        )
        # Parse the embedded HTML directly without Playwright navigation
        await scrape_from_html(html_content)
        return

    if not nav_url:
        nav_url = actual_url

    # Validate URL scheme
    parsed = urlparse(nav_url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"TARGET_URL is missing or invalid: {nav_url!r}")

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
        await Stealth().apply_stealth_async(page)

        log.info(f"Navigating to START_URL: {nav_url}")
        await page.goto(nav_url, wait_until="networkidle", timeout=60000)
        log.info("Page loaded.")

        page_number = 1
        while True:
            log.info(f"--- Scraping page {page_number} ---")
            records = await scrape_page(page, nav_url)

            for rec in records:
                key = make_record_key(rec)
                if key not in seen_records:
                    seen_records.add(key)
                    all_records.append(rec)

            log.info(f"Total unique records so far: {len(all_records)}")

            has_next = await advance_page(page, page.url, form_action)
            if not has_next:
                log.info("No more pages — scraping complete.")
                break

            page_number += 1
            await asyncio.sleep(1)

        await browser.close()

    save_results()


async def scrape_from_html(html_content: str):
    """
    Fallback: parse static HTML content directly with BeautifulSoup
    when the START_URL is not a navigable HTTP URL.
    """
    log.info("Parsing static HTML content with BeautifulSoup (no browser navigation).")
    soup = BeautifulSoup(html_content, "html.parser")

    item_sel = SELECTORS.get("item_selectors", "")
    title_sel = SELECTORS.get("title_selectors", "")
    date_sel = SELECTORS.get("date_selectors", "")

    items = []
    if item_sel:
        items = soup.select(item_sel)
        if items:
            log.info(f"Found {len(items)} item(s) using item_selectors in static HTML")
        else:
            log.warning(f"No items found in static HTML with selector: {item_sel!r}")

    for row in items:
        record = {
            "name": None,
            "address": None,
            "city": None,
            "state": None,
            "county": None,
            "lastInspected": None,
            "lastInspectionId": None,
            "details": {},
        }

        if title_sel:
            el = row.select_one(title_sel)
            if el:
                record["name"] = el.get_text(strip=True) or None
                log.info(f"title_selectors matched: {record['name']}")
            else:
                el = row.select_one("td")
                if el:
                    record["name"] = el.get_text(strip=True) or None

        if date_sel:
            el = row.select_one(date_sel)
            if el:
                record["lastInspected"] = el.get_text(strip=True) or None

        all_tds = row.select("td")
        td_texts = [td.get_text(strip=True) for td in all_tds]

        if len(td_texts) > 1:
            record["address"] = td_texts[1]
        if len(td_texts) > 2:
            city_state = td_texts[2]
            if city_state:
                cs_parts = city_state.split(",")
                if len(cs_parts) >= 2:
                    record["city"] = cs_parts[0].strip()
                    record["state"] = cs_parts[1].strip()
                else:
                    record["city"] = city_state.strip()

        for idx, td in enumerate(all_tds):
            if idx not in (0, 1, 2, 3):
                link_el = td.select_one("a")
                if link_el:
                    href = link_el.get("href", "")
                    if href:
                        id_match = re.search(r'[Ii][Dd]=([\w-]+)', href)
                        if id_match and not record["lastInspectionId"]:
                            record["lastInspectionId"] = id_match.group(1)
                        record["details"][f"link_col_{idx}"] = href
                cell_text = td.get_text(strip=True)
                if cell_text:
                    record["details"][f"col_{idx}"] = cell_text

        if not record["lastInspectionId"]:
            for a in row.select("a"):
                href = a.get("href", "")
                if href:
                    id_match = re.search(r'[Ii][Nn][Ss][Pp][Ii][Dd]=([\w-]+)|[Ii][Dd]=([\w-]+)', href)
                    if id_match:
                        record["lastInspectionId"] = id_match.group(1) or id_match.group(2)
                        break

        if record["name"] or record["address"]:
            key = make_record_key(record)
            if key not in seen_records:
                seen_records.add(key)
                all_records.append(record)

    log.info(f"Static HTML scrape: {len(all_records)} unique record(s) found.")
    save_results()


def save_results():
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"results_{timestamp}.json"
    with open(filename, "w", encoding="utf-8") as fh:
        json.dump(all_records, fh, indent=2, ensure_ascii=False)
    log.info(f"Saved {len(all_records)} record(s) to {filename}")


if __name__ == "__main__":
    asyncio.run(main())