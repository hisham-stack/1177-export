"""
Inkorg exporter — e-tjanster.1177.se

A different site from journalen.1177.se, with an independent session: an Angular
app whose list markup is confirmed, but whose items are conversations (arenden)
holding one or more messages, opened on their own page.

Two traps this module is built around:

1. Angular keeps all four tab panels ('Inkomna', 'Olasta', 'Favoriter',
   'Arkiverade') in the DOM at once, so an unscoped '.message-list__item' query
   returns items from every tab mixed together. Everything here is scoped to
   visible items and then verified against the page's own 'Visar X-Y av Z'
   counter.
2. The thread links are Angular router links with no href, so a thread can only
   be reached by clicking, and the list's tab and page must be restored
   afterwards.
"""

import re

from dataclasses import dataclass, field

from playwright.async_api import Page

from common import (
    CONTENT_TIMEOUT,
    NAV_TIMEOUT,
    TODAY,
    PartialExport,
    dump_debug_html,
    group_by_date,
    html_to_md,
    log,
    manual_login,
    safe_date,
    section_dirs,
    verify_harvest,
    write_json,
)

SECTION    = "inkorg"
INKORG_URL = "https://e-tjanster.1177.se/messageList"
LOGGED_IN  = re.compile(r"e-tjanster\.1177\.se/")

# A thread opens on one of two detail layouts: e-tjanster's own
# /messageDetail/<id>?page=&tab=, or a handoff to the third host
# arende.1177.se/inkorg/<id>. They do not share markup.
DETAIL_URL_RE = re.compile(r"/(?:inkorg|messageDetail)/\d+")

# Confirmed against the live DOM
ITEM_SEL    = ".message-list__item"
LINK_SEL    = "a.message-list__header-link"
DATE_SEL    = "span.message-list__date"
META_SEL    = ".message-list__content p"
THREAD_SEL  = "p.ids-disabled"
COUNTER_SEL = "p.filter-message-info"
PAGE_BTN    = "button.ids-list-pagination__button"

# Detail page: one <id-card class="ids-card"> per message, each stamped 'Skickades:'
CARD_SEL     = ".ids-card"
CARD_SEQ_SEL = "div.italic"      # "Meddelande 2 av 2"
CARD_HEAD_SEL = "h2"             # "Information" / "Svar" / "Skickat"

# Tabs worth exporting: 'Olasta' and 'Favoriter' are subsets of 'Inkomna'.
TABS = ["Inkomna", "Arkiverade"]

# "<subject> Mottaget <date> <status> <facility> klickbar"
LABEL_RE = re.compile(
    r"^(?P<subject>.+?)\s+Mottaget\s+(?P<date>\d{4}-\d{2}-\d{2})\s+(?P<rest>.+?)\s*klickbar\s*$"
)
COUNTER_RE = re.compile(r"Visar\s+\d+\s*[-–]\s*\d+\s+av\s+(\d+)")
# The colon renders on screen but does not survive the Markdown conversion,
# so it is optional here — requiring it silently matched nothing.
SENT_RE    = re.compile(r"Skickades:?\s*(\d{4}-\d{2}-\d{2}\s+[\d:]+)")
SEQ_RE     = re.compile(r"Meddelande\s+(\d+)\s+av\s+(\d+)")


@dataclass
class ThreadMessage:
    sequence: str = ""   # "Meddelande 1 av 2"
    heading:  str = ""   # "Information" / "Svar" / "Skickat"
    sent_at:  str = ""   # "2026-04-27 09:11"
    body_md:  str = ""


@dataclass
class Thread:
    date:          str = ""
    subject:       str = ""
    status:        str = ""   # "Information" / "Arendet avslutat"
    facility:      str = ""
    tab:           str = ""
    url:           str = ""
    message_count: int = 1
    content_md:    str = ""   # the whole detail page, so nothing is ever lost
    messages:      list[ThreadMessage] = field(default_factory=list)

# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------

async def login(page: Page) -> None:
    await manual_login(page, INKORG_URL, LOGGED_IN, SECTION)


def _visible(page: Page, selector: str):
    """Scope a selector to the active tab panel — see trap 1 in the module docstring."""
    return page.locator(f"{selector}:visible")


async def read_declared_total(page: Page) -> int | None:
    """Parse 'Visar 1-10 av 32' from the visible counter."""
    counter = _visible(page, COUNTER_SEL)
    if await counter.count() == 0:
        return None
    match = COUNTER_RE.search(await counter.first.inner_text())
    return int(match.group(1)) if match else None


async def select_tab(page: Page, name: str) -> str:
    """
    Switch to a tab by accessible name. Returns 'ok', 'empty', or 'unreachable'.

    The click is verified rather than assumed: the tab markup sits behind the
    design system's shadow roots, so a loose text match can land on something
    that is not the tab and silently leave the previous panel showing.

    An empty tab and an unreachable one both yield zero rows, so they are told
    apart by the site's own empty-state line ('Du har inga ... meddelanden') —
    otherwise a tab the scraper failed to open would look like one with nothing
    in it, and missing data would read as an empty inbox.
    """
    pattern = re.compile(rf"^\s*{re.escape(name)}\b", re.I)
    for candidate in (
        page.get_by_role("tab", name=pattern),
        page.get_by_role("button", name=pattern),
        page.get_by_role("link", name=pattern),
        page.get_by_text(pattern),
    ):
        for i in range(min(await candidate.count(), 3)):
            element = candidate.nth(i)
            if not await element.is_visible():
                continue
            await element.click()
            await page.wait_for_timeout(800)

            if await read_declared_total(page) is not None or await _visible(page, ITEM_SEL).count():
                log.info(f"[{SECTION}] Selected tab '{name}'.")
                return "ok"
            if await page.locator("text=Du har inga >> visible=true").count():
                log.info(f"[{SECTION}] Tab '{name}' is empty — the site says so explicitly.")
                return "empty"

    log.warning(
        f"[{SECTION}] Tab '{name}' could not be opened: no rows, no counter, and no "
        f"empty-state message. Its contents are missing from this export."
    )
    return "unreachable"


async def page_count(page: Page) -> int:
    """How many pagination pages the visible tab has (1 when unpaginated)."""
    buttons = _visible(page, f"{PAGE_BTN}[aria-label^='Gå till sida']")
    numbers = [1]
    for i in range(await buttons.count()):
        label = await buttons.nth(i).get_attribute("aria-label") or ""
        if match := re.search(r"sida\s+(\d+)", label):
            numbers.append(int(match.group(1)))
    return max(numbers)


async def goto_page(page: Page, number: int) -> None:
    """Jump to a pagination page. Page 1 is already current on a fresh load."""
    if number == 1:
        current = _visible(page, f"{PAGE_BTN}--current")
        if await current.count() and (await current.first.inner_text()).strip() == "1":
            return
    button = _visible(page, f"{PAGE_BTN}[aria-label='Gå till sida {number}']")
    if await button.count() == 0:
        raise RuntimeError(f"Pagination button for page {number} not found.")
    await button.first.click()
    await page.wait_for_timeout(700)


async def restore_list(page: Page, tab: str, page_number: int) -> None:
    """Return to the list at the given tab and page after visiting a thread."""
    if "messageList" not in page.url:
        await page.goto(INKORG_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
    await page.wait_for_selector(ITEM_SEL, timeout=NAV_TIMEOUT)
    if tab != TABS[0]:
        await select_tab(page, tab)
    if page_number > 1:
        await goto_page(page, page_number)

# ---------------------------------------------------------------------------
# List parsing
# ---------------------------------------------------------------------------

def parse_label(label: str) -> tuple[str, str, str]:
    """Split the link aria-label into (subject, date, trailing status+facility)."""
    match = LABEL_RE.match(label.strip())
    if not match:
        return "", "", ""
    return match.group("subject").strip(), match.group("date"), match.group("rest").strip()


def split_meta(meta_text: str, fallback: str) -> tuple[str, str]:
    """
    Split '<status> | <facility>' (e.g. 'Information | Vardcentralen X') into its parts.

    The aria-label runs the two together with no separator, so the visible row
    text — which does carry a '|' — is the reliable source; the label is only a
    fallback, and then the facility cannot be told apart from the status.
    """
    if "|" in meta_text:
        status, _, facility = meta_text.partition("|")
        return status.strip(), facility.strip()
    return (meta_text or fallback).strip(), ""


async def read_row(page: Page, index: int, tab: str) -> Thread:
    """Read one list row without opening it."""
    item = _visible(page, ITEM_SEL).nth(index)

    label = await item.locator(LINK_SEL).first.get_attribute("aria-label") or ""
    subject, date, rest = parse_label(label)

    meta_text = ""
    meta = item.locator(META_SEL)
    if await meta.count():
        meta_text = (await meta.first.inner_text()).strip()
    status, facility = split_meta(meta_text, rest)

    if not date:
        date_el = item.locator(DATE_SEL)
        if await date_el.count():
            date = (await date_el.first.inner_text()).strip()

    count = 1
    thread_note = item.locator(THREAD_SEL)
    if await thread_note.count():
        if match := re.search(r"(\d+)\s+meddelanden", await thread_note.first.inner_text()):
            count = int(match.group(1))

    return Thread(
        date          = date,
        subject       = subject,
        status        = status,
        facility      = facility,
        tab           = tab,
        message_count = count,
    )

# ---------------------------------------------------------------------------
# Detail parsing
# ---------------------------------------------------------------------------

async def _main_region(page: Page):
    """The detail page's content area, narrowest container that still holds the h1."""
    for selector in ("main", "[role='main']", "#main-content", ".ids-content"):
        region = page.locator(selector)
        if await region.count():
            return region.first
    return page.locator("body")


def split_markdown_messages(content_md: str) -> list[ThreadMessage]:
    """
    Last-resort split of an already-captured page on its 'Meddelande N av M'
    markers, for layouts that offer no card element to split on.

    Operating on the saved Markdown rather than the DOM means this can never
    lose content: worst case it returns nothing and `content_md` still stands.
    """
    parts = [p.strip() for p in re.split(r"(?=Meddelande\s+\d+\s+av\s+\d+)", content_md)]
    return [
        ThreadMessage(
            sequence = SEQ_RE.search(part).group(0),
            sent_at  = match.group(1) if (match := SENT_RE.search(part)) else "",
            body_md  = part,
        )
        for part in parts
        if SEQ_RE.search(part)
    ]


async def dismiss_session_warning(page: Page) -> None:
    """
    Close the 'Du haller pa att bli utloggad' inactivity dialog if it is showing.

    It sits in the DOM on every page, permanently hidden until it fires, and a
    long export is exactly the workload that wakes it.
    """
    dialog = page.locator("h1:visible").filter(has_text=re.compile(r"bli utloggad", re.I))
    if await dialog.count() == 0:
        return
    log.warning("    Inactivity dialog appeared — dismissing.")
    for name in (r"Fortsätt", r"Stanna", r"Ja"):
        button = page.get_by_role("button", name=re.compile(name, re.I))
        if await button.count() and await button.first.is_visible():
            await button.first.click()
            await page.wait_for_timeout(500)
            return


async def extract_detail(page: Page, thread: Thread) -> None:
    """
    Read an opened thread into `thread`.

    The whole content region is stored as Markdown first, so the export is
    complete even if the per-message split below misses a card. The structured
    breakdown is a convenience on top of that, never the only copy.
    """
    # Two traps in one wait:
    #  - Never wait on a bare 'h1'. The page carries four and the first is the
    #    permanently hidden inactivity dialog, so wait_for_selector('h1') waits
    #    on an element that never becomes visible and times out every time.
    #  - Never treat the URL, or any visible heading, as "the page is ready".
    #    Angular swaps the route while the previous view is still mounted, so
    #    both match early and the content region is read back empty.
    # Every detail layout stamps its messages 'Skickades:', and the list page
    # has none, so that text is the one honest signal that the thread rendered.
    try:
        await page.wait_for_url(DETAIL_URL_RE, timeout=CONTENT_TIMEOUT)
    except Exception:
        pass  # Some thread types render without a recognised detail route.

    # '>> visible=true' filters the match set. Taking '.first' instead waits on
    # whichever element comes first in the DOM — which on these pages is hidden,
    # so the wait burns its whole timeout before falling through.
    try:
        await page.wait_for_selector("text=Skickades >> visible=true", timeout=CONTENT_TIMEOUT)
    except Exception:
        heading = page.locator("h1:visible").filter(has_text=thread.subject[:40])
        await heading.first.wait_for(state="visible", timeout=CONTENT_TIMEOUT)

    await dismiss_session_warning(page)

    thread.url = page.url

    region = await _main_region(page)
    thread.content_md = html_to_md(await region.inner_html())

    # An empty content region means the view had not rendered when it was read.
    # This is the failure that verify_harvest cannot see — the thread is counted
    # as collected while carrying nothing — so it has to be loud on its own.
    if not thread.content_md.strip():
        log.error(
            f"    EMPTY content for '{thread.subject}' ({page.url}) — "
            f"the detail view had not rendered when it was read."
        )

    # On arende.1177.se a message card is an .ids-card carrying a 'Skickades:'
    # stamp. e-tjanster's own detail page uses different markup, so fall back to
    # a structural definition: the innermost element holding both a heading and
    # a 'Skickades:' stamp is one message, whatever it is called.
    cards = page.locator(f"{CARD_SEL}:visible").filter(has_text=re.compile(r"Skickades"))
    found = await cards.count()
    if found == 0:
        cards = page.locator(
            "xpath=//*[.//h2][contains(., 'Skickades')]"
            "[not(.//*[.//h2][contains(., 'Skickades')])]"
        )
        found = await cards.count()

    if found == 0:
        # e-tjanster's own single-message layout has no headings to split on.
        # The content is already captured, so represent it as the one message it
        # is rather than leaving `messages` inconsistently empty.
        if thread.message_count == 1:
            log.info("    Single-message layout — using the page as one message.")
            thread.messages.append(ThreadMessage(
                heading = thread.subject,
                sent_at = match.group(1) if (match := SENT_RE.search(thread.content_md)) else "",
                body_md = thread.content_md,
            ))
        elif split := split_markdown_messages(thread.content_md):
            log.info(f"    Split {len(split)} messages from the page text.")
            thread.messages.extend(split)
        else:
            log.warning(
                f"    No message cards matched for a {thread.message_count}-message "
                f"thread — full page kept in content_md."
            )
        return

    for i in range(found):
        card = cards.nth(i)
        text = (await card.inner_text()).strip()
        seq  = card.locator(CARD_SEQ_SEL)
        head = card.locator(CARD_HEAD_SEL)
        thread.messages.append(ThreadMessage(
            sequence = (await seq.first.inner_text()).strip() if await seq.count()
                       else (match.group(0) if (match := SEQ_RE.search(text)) else ""),
            heading  = (await head.first.inner_text()).strip() if await head.count() else "",
            sent_at  = match.group(1) if (match := SENT_RE.search(text)) else "",
            body_md  = html_to_md(await card.inner_html()),
        ))

    if thread.message_count and found != thread.message_count:
        log.warning(
            f"    Thread '{thread.subject}': list said {thread.message_count} messages, "
            f"detail page yielded {found}."
        )

# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------

async def scrape_tab(page: Page, tab: str) -> list[Thread]:
    declared = await read_declared_total(page)
    pages    = await page_count(page)
    log.info(f"[{SECTION}] Tab '{tab}': {declared if declared is not None else '?'} threads over {pages} page(s).")

    threads: list[Thread] = []
    for page_number in range(1, pages + 1):
        if page_number > 1:
            await goto_page(page, page_number)

        on_page = await _visible(page, ITEM_SEL).count()
        log.info(f"[{SECTION}] Tab '{tab}', page {page_number}/{pages}: {on_page} rows.")

        for index in range(on_page):
            try:
                thread = await read_row(page, index, tab)
                log.info(f"  [{len(threads) + 1}] {thread.date} — {thread.subject}")

                await _visible(page, ITEM_SEL).nth(index).locator(LINK_SEL).first.click()
                await extract_detail(page, thread)
                threads.append(thread)
            except Exception as e:
                log.warning(f"    Skipping row {index + 1} on page {page_number}: {e}")
            finally:
                await restore_list(page, tab, page_number)

    verify_harvest(f"{SECTION}/{tab}", len(threads), declared, strict=False)
    return threads


async def scrape(page: Page) -> list[Thread]:
    await login(page)
    if "messageList" not in page.url:
        await page.goto(INKORG_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)

    try:
        await page.wait_for_selector(ITEM_SEL, timeout=NAV_TIMEOUT)
    except Exception:
        log.error(f"[{SECTION}] No message rows found.")
        await dump_debug_html(page, section_dirs(SECTION)[0].parent / "_debug_inkorg.html")
        return []

    threads: list[Thread] = []
    unreachable: list[str] = []

    for tab in TABS:
        if tab != TABS[0]:
            status = await select_tab(page, tab)
            if status == "unreachable":
                unreachable.append(tab)
            if status != "ok":
                continue
        threads.extend(await scrape_tab(page, tab))

    # A tab that could not be opened is missing data, not an empty tab, and the
    # export must not end on a quiet note as though everything was collected.
    if unreachable:
        raise PartialExport(
            f"Tab(s) {', '.join(unreachable)} could not be opened — this export is incomplete.",
            threads,
        )
    return threads

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def save(threads: list[Thread]) -> None:
    if not threads:
        log.warning(f"[{SECTION}] Nothing to save.")
        return

    md_dir, json_dir = section_dirs(SECTION)

    for date, day_threads in sorted(group_by_date(threads).items()):
        md_path = md_dir / f"{safe_date(date)}.md"
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(f"# Inkorg — {date}\n\n")
            f.write(f"**Exporterat:** {TODAY}  \n")
            f.write(f"**Antal arenden denna dag:** {len(day_threads)}\n\n---\n\n")

            for i, thread in enumerate(day_threads, 1):
                f.write(f"## {i}. {thread.subject or '(utan rubrik)'}\n\n")
                f.write(f"**Datum:** {thread.date}  \n")
                if thread.status:
                    f.write(f"**Status:** {thread.status}  \n")
                if thread.facility:
                    f.write(f"**Vardgivare:** {thread.facility}  \n")
                f.write(f"**Flik:** {thread.tab}  \n")
                f.write(f"**Antal meddelanden:** {thread.message_count}\n\n")

                if thread.messages:
                    for msg in thread.messages:
                        title = " — ".join(x for x in (msg.sequence, msg.heading) if x)
                        f.write(f"### {title or 'Meddelande'}\n\n")
                        if msg.sent_at:
                            f.write(f"**Skickades:** {msg.sent_at}\n\n")
                        f.write(msg.body_md or "*(Inget innehall extraherat)*")
                        f.write("\n\n")
                else:
                    f.write(thread.content_md or "*(Inget innehall extraherat)*")
                    f.write("\n\n")
                f.write("---\n\n")
        log.info(f"  {md_path}  ({len(day_threads)} arenden)")

    write_json(threads, json_dir / f"inkorg_{TODAY}.json")
