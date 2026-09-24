"""
Anteckningar exporter — journalen.1177.se

Records live in a server-rendered list whose details are injected by AJAX when
an item is expanded in place. See CLAUDE.md for the confirmed DOM facts.
"""

import re

from dataclasses import dataclass

from playwright.async_api import Page

from common import (
    CONTENT_TIMEOUT,
    NAV_TIMEOUT,
    TODAY,
    group_by_date,
    html_to_md,
    log,
    manual_login,
    safe_date,
    section_dirs,
    verify_harvest,
    write_json,
)

SECTION       = "anteckningar"
JOURNALEN_URL = "https://journalen.1177.se"
LOGGED_IN     = re.compile(r"journalen\.1177\.se/(?!LoggedOut)")

ANY_ITEM_SEL = "#nc-list-posts li:not(.nc-loading-spinner-row)"
ITEM_SEL     = "#nc-list-posts li.nc-list-post:not(.nc-loading-spinner-row)"
EXPANDER_SEL = f"{ITEM_SEL} button.nc-list-post-expander"


@dataclass
class Record:
    date:        str = ""
    record_type: str = ""
    author:      str = ""
    facility:    str = ""
    record_id:   str = ""
    content_md:  str = ""

# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------

async def login(page: Page) -> None:
    await manual_login(page, f"{JOURNALEN_URL}/", LOGGED_IN, SECTION)


async def _find_link(page: Page, pattern: re.Pattern):
    """Locate a nav link by accessible name, falling back to link text."""
    link = page.get_by_role("link", name=pattern)
    if await link.count() == 0:
        link = page.locator("a").filter(has_text=pattern)
    return link


async def navigate(page: Page) -> None:
    """Click Journalen → Anteckningar in the nav bar (link-based, not URL-hardcoded)."""
    log.info(f"[{SECTION}] Clicking 'Journalen' in the navigation bar...")
    nav_link = await _find_link(page, re.compile(r"^Journalen$", re.I))
    if await nav_link.count() == 0:
        raise RuntimeError("No 'Journalen' link found.")

    await nav_link.first.click()
    await page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT)

    if "CareDocumentation" not in page.url:
        ant_link = await _find_link(page, re.compile(r"Anteckningar", re.I))
        if await ant_link.count() == 0:
            raise RuntimeError("No 'Anteckningar' link found.")
        await ant_link.first.click()
        await page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT)

    log.info(f"[{SECTION}] Anteckningar URL: {page.url}")
    if any(x in page.url for x in ["LoggedOut", "NotFound", "login"]):
        raise RuntimeError(f"Navigation failed: {page.url}")

# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

async def load_all(page: Page) -> tuple[int, int | None]:
    """
    Wait for every record to appear in the DOM.

    The page first renders 10 records with 'Visa 10 till' / 'Visa alla'
    controls; clicking 'Visa alla' fires AJAX requests that progressively add
    <li> elements. `networkidle` fires far too early here (~49 ms) and must not
    be relied on, so we poll the DOM count instead.

    Returns (count_in_dom, declared_total).
    """
    log.info(f"[{SECTION}] Waiting for initial records to render...")
    try:
        await page.wait_for_selector(ANY_ITEM_SEL, timeout=NAV_TIMEOUT)
    except Exception:
        log.error(f"[{SECTION}] Records never appeared.")
        return 0, None

    counter = page.locator("[data-cy-id='total-number']")
    declared: int | None = None
    if await counter.count():
        try:
            declared = int(await counter.first.get_attribute("data-cy-value") or "0")
        except ValueError:
            declared = None
    log.info(f"[{SECTION}] Total records reported: {declared if declared is not None else 'unknown'}")

    if declared == 0:
        return 0, 0

    load_all_btn  = page.locator("button.load-all")
    load_more_btn = page.locator("button.load-more")

    if await load_all_btn.count() and await load_all_btn.first.is_visible() and not await load_all_btn.first.is_disabled():
        log.info(f"[{SECTION}] Clicking 'Visa alla'...")
        await load_all_btn.first.click()
    elif await load_more_btn.count():
        while await load_more_btn.first.is_visible() and not await load_more_btn.first.is_disabled():
            log.info(f"[{SECTION}] Clicking 'Visa 10 till'...")
            await load_more_btn.first.click()
            await page.wait_for_load_state("networkidle", timeout=15_000)

    if declared:
        log.info(f"[{SECTION}] Waiting for all {declared} records to appear in the DOM...")
        try:
            await page.wait_for_function(
                f"() => document.querySelectorAll('{ITEM_SEL}').length >= {declared}",
                timeout=60_000,
            )
        except Exception:
            log.warning(f"[{SECTION}] Timed out waiting for the full list.")

    count = await page.locator(ITEM_SEL).count()
    log.info(f"[{SECTION}] {count} records in DOM.")
    return count, declared

# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def parse_expander_label(label: str) -> tuple[str, str, str, str]:
    """Extract date, record_type, author, facility from the button aria-label."""
    date        = re.search(r"Datum\s+([\d-]+)", label)
    record_type = re.search(r"anteckningstyp\s+([^,]+)", label, re.I)
    author      = re.search(r"antecknad av\s+([^,]+)", label, re.I)
    facility    = re.search(r"på\s+([^,.]+)", label, re.I)
    return (
        date.group(1).strip()        if date        else "",
        record_type.group(1).strip() if record_type else "",
        author.group(1).strip()      if author      else "",
        facility.group(1).strip()    if facility    else "",
    )


async def extract_record(page: Page, index: int, total: int) -> Record:
    """
    Expand the record at `index` and read its detail.

    The detail container starts empty with class 'nu-hidden'; after the click it
    is populated by AJAX and the class is removed.
    """
    btn = page.locator(EXPANDER_SEL).nth(index)

    label     = await btn.get_attribute("aria-label") or ""
    record_id = await btn.get_attribute("data-id") or ""
    date, record_type, author, facility = parse_expander_label(label)

    log.info(f"  [{index + 1}/{total}] {date} — {record_type}")
    await btn.click()

    container = btn.locator("xpath=following-sibling::div[contains(@class,'nc-list-post-container')]")
    try:
        await page.wait_for_function(
            f"""() => {{
                const btn = document.querySelectorAll('{EXPANDER_SEL}')[{index}];
                if (!btn) return false;
                const div = btn.nextElementSibling;
                return div && !div.classList.contains('nu-hidden') && div.innerHTML.trim().length > 0;
            }}""",
            timeout=CONTENT_TIMEOUT,
        )
    except Exception:
        log.warning(f"    Timeout expanding record {index + 1}. Content may be empty.")

    html = await container.inner_html() if await container.count() else ""

    # Collapse before moving on (keeps the DOM small)
    if await btn.get_attribute("aria-expanded") == "true":
        await btn.click()

    return Record(
        date        = date,
        record_type = record_type,
        author      = author,
        facility    = facility,
        record_id   = record_id,
        content_md  = html_to_md(html),
    )


async def scrape(page: Page) -> list[Record]:
    await login(page)
    await navigate(page)
    count, declared = await load_all(page)

    records: list[Record] = []
    for i in range(count):
        try:
            records.append(await extract_record(page, i, count))
        except Exception as e:
            log.warning(f"    Skipping record {i + 1}: {e}")

    verify_harvest(SECTION, len(records), declared, strict=False)
    return records

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def save(records: list[Record]) -> None:
    if not records:
        log.warning(f"[{SECTION}] Nothing to save.")
        return

    md_dir, json_dir = section_dirs(SECTION)

    for date, day_records in sorted(group_by_date(records).items()):
        md_path = md_dir / f"{safe_date(date)}.md"
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(f"# Anteckningar — {date}\n\n")
            f.write(f"**Exporterat:** {TODAY}  \n")
            f.write(f"**Antal anteckningar denna dag:** {len(day_records)}\n\n---\n\n")

            for i, rec in enumerate(day_records, 1):
                f.write(f"## {i}. {rec.record_type}\n\n")
                f.write(f"**Datum:** {rec.date}  \n")
                f.write(f"**Antecknad av:** {rec.author}  \n")
                if rec.facility:
                    f.write(f"**Vardgivare:** {rec.facility}  \n")
                f.write("\n")
                f.write(rec.content_md or "*(Inget innehall extraherat)*")
                f.write("\n\n---\n\n")
        log.info(f"  {md_path}  ({len(day_records)} poster)")

    write_json(records, json_dir / f"anteckningar_{TODAY}.json")
