"""
Entry point for the 1177 exporters.

Each section is a module of its own because they target different sites with
independent sessions; this file only decides what to run and keeps the browser
alive across them.
"""

import argparse
import asyncio

from playwright.async_api import async_playwright

import anteckningar
import inkorg
from common import PartialExport, clean_section, log, setup_logging

SECTIONS = {
    "anteckningar": anteckningar,
    "inkorg":       inkorg,
}


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Export personal 1177 data (Anteckningar, Inkorg) to Markdown and JSON.",
    )
    ap.add_argument(
        "--only",
        choices=sorted(SECTIONS),
        action="append",
        help="Export only this section; repeatable. Defaults to all sections.",
    )
    ap.add_argument(
        "--clean",
        action="store_true",
        help="Delete each selected section's previous export before scraping, "
             "so the output reflects exactly one run.",
    )
    return ap.parse_args()


async def main(args: argparse.Namespace) -> None:
    setup_logging()
    selected = args.only or sorted(SECTIONS)

    if args.clean:
        for name in selected:
            clean_section(name)

    if len(selected) > 1:
        log.info(
            "Selected sections live on different 1177 sites with independent "
            "sessions — expect a BankID prompt for each."
        )

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--no-sandbox", "--disable-setuid-sandbox"],
        )
        page = await (await browser.new_context()).new_page()
        results: dict[str, list] = {}
        failed:  list[str]       = []

        try:
            # One section failing must not waste the manual BankID login the
            # others still need, so each is isolated and saved on its own.
            for name in selected:
                module = SECTIONS[name]
                try:
                    results[name] = await module.scrape(page)
                except PartialExport as e:
                    # Incomplete, but what it did collect is still worth saving.
                    results[name] = e.items
                    failed.append(name)
                    log.error(f"Section '{name}' incomplete: {e}")
                except Exception as e:
                    failed.append(name)
                    log.error(f"Section '{name}' failed: {e}", exc_info=True)
        finally:
            await browser.close()

        for name, items in results.items():
            SECTIONS[name].save(items)
            log.info(f"{name}: {len(items)} items exported.")

        if failed:
            raise SystemExit(f"Section(s) failed: {', '.join(failed)} — see scraper.log")


if __name__ == "__main__":
    asyncio.run(main(parse_args()))
