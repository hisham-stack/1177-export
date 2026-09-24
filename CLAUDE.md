# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Playwright scraper that exports personal 1177 health data to Markdown and JSON. BankID authentication is manual — the user completes it in the browser window that Playwright opens.

Two sections are exported, and they are **not** variations of one scraper:

| Section        | Site                             | Stack                | Unit of data                                        |
| -------------- | -------------------------------- | -------------------- | --------------------------------------------------- |
| `anteckningar` | `journalen.1177.se`              | server-rendered      | one record, expanded in place                       |
| `inkorg`       | `e-tjanster.1177.se/messageList` | Angular + shadow DOM | a thread (ärende) of 1..n messages, on its own page |

1177 keeps inbox messages for **two years** ("Meddelanden finns tillgängliga i din inkorg i två år"), so a short Inkorg export is the source's retention limit, not a gap in the scraper.

The two sites hold **independent sessions**, so exporting both normally means two BankID prompts.

## Running

```bash
source venv/bin/activate

python scraper.py                        # every section
python scraper.py --only inkorg          # one section
python scraper.py --only anteckningar --only inkorg
python scraper.py --only inkorg --clean  # ...after deleting its previous export
```

A section that fails is logged and skipped so the others still run on their own login; the process exits non-zero afterwards.

`--clean` (`common.clean_section`) deletes the selected sections' previous exports first. It is deliberately opt-in, not something `save()` does on its own: writing is per-date, so without it a failed run leaves a silent mix of old and new files — but clearing automatically would let a run that fails halfway wipe a good earlier export and replace it with a partial one.

## Installing dependencies

`requirements.in` lists direct dependencies only (`playwright`, `markdownify`). `requirements.txt` is the full pinned lockfile including transitives.

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

On Ubuntu 26.04, Playwright refuses to install its browsers (`ERROR: Playwright does not support chromium on ubuntu26.04-x64`). Override the platform check:

```bash
PLAYWRIGHT_HOST_PLATFORM_OVERRIDE=ubuntu24.04-x64 playwright install chromium
```

To regenerate the lockfile after a version bump:

```bash
pip install pip-tools
pip-compile requirements.in
pip-sync requirements.txt
```

## Layout

```text
scraper.py        # CLI: picks sections, owns the browser, saves results
common.py         # config, logging, manual_login(), harvest verification, output helpers
anteckningar.py   # journalen.1177.se
inkorg.py         # e-tjanster.1177.se
```

Each section module exposes the same two functions — `scrape(page) -> list[...]` and `save(items)` — and `scraper.py` dispatches through the `SECTIONS` dict. Adding a section means adding a module and one dict entry.

## Guarding against silent partial exports

A scraper that quietly returns half the data is worse than one that crashes, so every failure mode that would otherwise produce plausible-looking output is made loud:

- **Counts are verified.** `common.verify_harvest` compares each section's haul against the total the page itself reports, and logs either way.
- **Empty content is an error, separately.** A count-based check cannot catch a thread that was collected but came back hollow — that bug shipped once, reporting `32/32` while 15 threads held nothing. `inkorg.extract_detail` therefore logs an `ERROR` naming the subject and URL when `content_md` is blank. **Verifying counts is not verifying content; both are needed.**
- **An empty tab and an unreachable one are distinguished** by the site's own `"Du har inga … meddelanden"` line. Both yield zero rows, so without that signal missing data reads as an empty inbox.
- **Nothing is parsed destructively.** Every thread's whole detail page is stored in `content_md` **before** any attempt to split it into messages, so a missed card costs structure, never content.
- **An incomplete run still saves.** `common.PartialExport` carries the collected items with the exception, so `scraper.py` writes them and still exits non-zero — reporting a failure should not mean discarding good records.

When adding scraping code, keep these properties: parse defensively, never let the parsed view be the only copy, and make sure each guard checks something the others cannot see.

## Site DOM facts

### Anteckningar (`journalen.1177.se`, verified 2026-05)

- Records live in `<ul id="nc-list-posts"><li class="nc-list-post">` — **not** a `<table>`
- Each `<li>` holds a `<button class="nc-list-post-expander" data-id="..." data-date="...">` whose `aria-label` carries date, type, author, and facility as plain text
- Detail content is the sibling `<div class="nc-list-post-container nu-hidden">`, injected by AJAX on click; it starts empty
- `wait_for_function` polls the `<li>` count against `data-cy-value` on the total-number element; `networkidle` fires far too early here (~49 ms) and must not be relied on

### Inkorg (`e-tjanster.1177.se/messageList`, verified 2026-09)

- Angular (`_ngcontent-ng-*`) with the Inera design system (`ids-*`); ~132 shadow roots. Playwright's CSS engine pierces them, but raw `document.querySelectorAll` inside `wait_for_function`/`evaluate` does **not**
- **All four tab panels stay in the DOM at once** (`Inkomna`, `Olästa`, `Favoriter`, `Arkiverade`), so an unscoped `.message-list__item` query returns ~40 mixed rows for a 32-item inbox. Every query is scoped with `:visible` and verified against the counter
- Row: `.message-list__item` inside `div.message-list`
  - `a.message-list__header-link` — the title. **No `href`** (Angular router link), so a thread is only reachable by clicking, and the tab and page must be restored afterwards
  - its `aria-label` is `"<subject> Mottaget <date> <status> <facility> klickbar"` — status and facility run together with no separator
  - `.message-list__content p` — the visible `"<status> | <facility>"` line, which *does* separate them; this is the reliable source
  - `p.ids-disabled` — `"N meddelanden"`, present only on multi-message threads
- Counter: `p.filter-message-info` → `"Visar 1-10 av 32"`
- Pagination: `button.ids-list-pagination__button[aria-label="Gå till sida N"]`, plus `[aria-label="Nästa sida"]`. Rendered twice (above and below the list) per tab, so always take `.first` of a `:visible` match
- Tabs: only `Inkomna` holds anything; `Olästa` and `Favoriter` are subsets of it and `Arkiverade` was empty when verified. An empty tab states so (`"Du har inga … meddelanden"`), which is how `select_tab` tells an empty tab from one it failed to open — without that line the two are indistinguishable and missing data reads as an empty inbox
- **Two detail layouts, no shared markup.** `Beställ tid`, `Meddelande till invånare`, `Förnya recept` and `Kontakta mig` hand off to `arende.1177.se/inkorg/<id>` and use `<id-card class="ids-card">` per message, with `div.italic` (`"Meddelande N av M"`) and `h2`. `Kallelse till besök` and `Tidbokning` stay on `e-tjanster.1177.se/messageDetail/<id>?page=&tab=` and offer no card element at all — those fall back to synthesising one message, or to splitting the captured Markdown on its `"Meddelande N av M"` markers
- Every message carries `Skickades: YYYY-MM-DD HH:MM` on screen, but **the colon does not survive the Markdown conversion** — `SENT_RE` makes it optional, and requiring it matched nothing at all

### Waiting on this Angular app

Three separate bugs here came from the same mistake — assuming the first DOM match is the intended one:

- `wait_for_selector("h1")` waits on the *first* `h1`, which is the permanently hidden inactivity dialog, so it burns its full timeout every time. The same applies to `get_by_text(...).first`. Filter the match set instead: `"text=… >> visible=true"`, or a `:visible` suffix
- A matching URL does not mean the view rendered. Angular swaps the route while the previous page is still mounted, so both `wait_for_url` and any visible heading match early and the content region reads back **empty**. Wait for content that only the destination has — here, the `Skickades` stamp

## Output

```text
output/
├── anteckningar/
│   ├── md/YYYY-MM-DD.md                 # one file per date
│   └── json/anteckningar_YYYY-MM-DD.json
└── inkorg/
    ├── md/YYYY-MM-DD.md                 # one file per date, threads with nested messages
    └── json/inkorg_YYYY-MM-DD.json
```

Items with no detectable date land in `utan-datum.md`. `scraper.log` is written to the project root. The `output/` directory is git-ignored.

Output files contain sensitive medical data — handle accordingly. `common.dump_debug_html()` writes a full page dump on unexpected markup; that dump is personal data too.
