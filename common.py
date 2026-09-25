"""
Shared infrastructure for the 1177 exporters.

Anteckningar and Inkorg live on different sites with independent sessions and
unrelated markup, so each has its own module. Everything they genuinely share —
config, logging, the browser lifecycle, manual BankID login, and output
writing — lives here so the two cannot drift apart.
"""

import json
import logging
import re
import sys
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from markdownify import markdownify as md_convert
from playwright.async_api import Page

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

OUTPUT_DIR = Path("output")
TODAY      = datetime.today().strftime("%Y-%m-%d")

LOGIN_TIMEOUT   = 120_000   # 2 min for manual BankID
NAV_TIMEOUT     = 30_000
CONTENT_TIMEOUT = 15_000

log = logging.getLogger("1177-export")


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("scraper.log"),
        ],
    )

# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

async def manual_login(page: Page, start_url: str, logged_in: re.Pattern, service: str) -> None:
    """
    Open `start_url` and wait for the user to finish BankID in the browser.

    Returns immediately when the session is already authenticated, so selecting
    both sections does not always mean two logins — that depends on whether the
    two sites share a session, which they historically do not.
    """
    log.info(f"[{service}] Opening {start_url} ...")
    await page.goto(start_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
    log.info(f"[{service}] Landed on: {page.url}")

    if not logged_in.search(page.url):
        log.info(f"[{service}] Complete BankID login in the browser window (2 min timeout)...")
    await page.wait_for_url(logged_in, timeout=LOGIN_TIMEOUT)
    log.info(f"[{service}] Authenticated: {page.url}")

# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

class HarvestMismatch(RuntimeError):
    """Raised when the number of items collected disagrees with the site's own count."""


class PartialExport(RuntimeError):
    """
    Raised when a section knows its export is incomplete but still has data worth
    keeping. Carries that data so the caller can save it and still exit non-zero:
    discarding good records to report a failure would be its own kind of loss.
    """

    def __init__(self, message: str, items: list):
        super().__init__(message)
        self.items = items


def verify_harvest(section: str, collected: int, declared: int | None, strict: bool = True) -> None:
    """
    Compare what we collected against the total the page itself reports.

    A scraper that quietly returns half the data is worse than one that fails,
    so a mismatch is loud by default.
    """
    if declared is None:
        log.warning(f"[{section}] No declared total to verify against — collected {collected}.")
        return
    if collected == declared:
        log.info(f"[{section}] Verified: {collected}/{declared} items collected.")
        return
    msg = f"[{section}] Count mismatch: collected {collected} but the page reports {declared}."
    if strict:
        raise HarvestMismatch(msg)
    log.error(msg)

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def html_to_md(html: str) -> str:
    return md_convert(html).strip() if html and html.strip() else ""


def group_by_date(items: list) -> dict[str, list]:
    """Group dataclass items by their `.date`, bucketing dateless ones together."""
    grouped: dict[str, list] = defaultdict(list)
    for item in items:
        grouped[item.date or "utan-datum"].append(item)
    return grouped


def section_dirs(section: str) -> tuple[Path, Path]:
    """Return (md_dir, json_dir) for a section, created if missing."""
    md_dir   = OUTPUT_DIR / section / "md"
    json_dir = OUTPUT_DIR / section / "json"
    md_dir.mkdir(parents=True, exist_ok=True)
    json_dir.mkdir(parents=True, exist_ok=True)
    return md_dir, json_dir


def clean_section(section: str) -> None:
    """
    Delete a section's previous export.

    Kept as an explicit opt-in rather than something `save()` does on its own: a
    run that fails halfway would otherwise wipe a good earlier export and
    replace it with a partial one. Writing is per-date, so without this a failed
    run leaves a silent mix of old and new files.
    """
    md_dir, json_dir = section_dirs(section)
    removed = 0
    for path in list(md_dir.glob("*.md")) + list(json_dir.glob("*.json")):
        path.unlink()
        removed += 1
    log.info(f"[{section}] Cleaned {removed} file(s) from the previous export.")


def write_json(items: list, path: Path) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump([asdict(i) for i in items], f, ensure_ascii=False, indent=2)
    log.info(f"  {path}")


def safe_date(date: str) -> str:
    return (date or "utan-datum").replace("/", "-")


async def dump_debug_html(page: Page, path: Path) -> None:
    """Save the current page so unexpected markup can be inspected offline."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(await page.content(), encoding="utf-8")
    log.warning(f"Saved page HTML for inspection: {path} (contains personal data)")
