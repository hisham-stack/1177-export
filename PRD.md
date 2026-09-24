# PRD: Personal Health Data Exporter — 1177.se

**Author:** Hisham Ali
**Last updated:** 2026-09-24
**Status:** Complete

---

## Problem

1177.se (Sweden's national healthcare portal) provides access to personal health data through a web interface. There is no official export feature that produces a portable, structured file. Reading or archiving records requires manual browsing, session by session.

The goal is to automate the extraction of the authenticated user's own data and save it locally as human-readable Markdown and machine-readable JSON.

---

## Scope

**In scope**

- Anteckningar (visit notes) — full note content, not just headers
- Inkorg (inbox conversations) — every message in every thread
- The authenticated user's own data only

**Out of scope**

- Other journal sections (Diagnoser, Läkemedel, Remisser, etc.)
- Any other user's records
- Automating the BankID authentication step
- Sending, replying to or archiving messages — this is an exporter, it never writes to 1177

---

## Technical Context

The two sections are not variations of one scraper. They differ in host, session, framework and unit of data:

| Property        | Anteckningar                             | Inkorg                                                              |
| --------------- | ---------------------------------------- | ------------------------------------------------------------------- |
| Host            | `journalen.1177.se`                      | `e-tjanster.1177.se/messageList`                                    |
| Section URL     | `/JournalCategories/CareDocumentation`   | `/messageList`                                                      |
| Rendering       | Server-rendered; detail injected by AJAX | Angular SPA, ~132 shadow roots (Inera design system)                |
| Unit of data    | One record, expanded in place            | A thread (ärende) of 1..n messages, on its own page                 |
| Detail location | Sibling container in the same page       | `/messageDetail/<id>`, or a handoff to `arende.1177.se/inkorg/<id>` |

Both use Swedish BankID, which is manual and cannot be automated. **The two hosts maintain independent sessions**, so exporting both normally means two BankID prompts.

1177 retains inbox messages for **two years** only; a short Inkorg export reflects that retention limit, not a failure to find older messages.

---

## Solution

A Python package driving a real Chromium browser via Playwright. The user completes BankID manually in the opened window; the script automates all subsequent navigation and extraction.

### Layout

```text
scraper.py        # CLI: picks sections, owns the browser, saves results
common.py         # config, logging, login, verification, output helpers
anteckningar.py   # journalen.1177.se
inkorg.py         # e-tjanster.1177.se
```

Each section module exposes `scrape(page)` and `save(items)` and is dispatched from a `SECTIONS` dict. They share one login, browser and output layer so the two cannot drift apart, while their site-specific code stays separate. Adding a section means adding a module and one dict entry.

### Stack

| Package       | Role                        |
| ------------- | --------------------------- |
| `playwright`  | Browser automation          |
| `markdownify` | HTML-to-Markdown conversion |

### Flow

1. Open the section's host — triggers the BankID redirect
2. User completes BankID in the browser window (2-minute window)
3. Navigate to the section by nav-bar link or tab, never a hardcoded URL
4. Load the full list: "Visa alla" plus DOM polling (Anteckningar), or tab plus pagination (Inkorg)
5. Open each entry, wait for its content to render, convert the HTML to Markdown
6. Verify the haul against the count the page itself reports
7. Group by date and write the output files

### Key implementation decisions

| Decision                                            | Reason                                                                                           |
| --------------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| Separate modules per section, shared infrastructure | Two different sites; duplicating login and output into standalone scripts would let them diverge |
| Poll DOM count instead of `networkidle`             | `networkidle` fires ~49 ms after "Visa alla", with only the first 10 of N records loaded         |
| Navigate via nav links and tabs, not URLs           | The site's URL structure changed during development; link-based navigation survived it           |
| Expand records sequentially, not in parallel        | AJAX per record; parallel expansion caused DOM state conflicts                                   |
| `aria-label` as metadata source                     | Date, type, author and facility are all in the label without opening the entry                   |
| Store the whole detail page before parsing it       | A missed card then costs structure, never content                                                |
| `--clean` is opt-in, not automatic                  | Clearing on every run would let a half-failed run overwrite a complete export                    |

---

## Key Findings During Development

### Anteckningar

| Issue                                     | Root Cause                                                             | Resolution                                         |
| ----------------------------------------- | ---------------------------------------------------------------------- | -------------------------------------------------- |
| SSO landed on `LoggedOut/TimedOutResult`  | `e-tjanster.1177.se` and `journalen.1177.se` keep independent sessions | Log in directly on `journalen.1177.se`             |
| `/Anteckningar` returned `Error/NotFound` | No valid session for the journalen subdomain                           | Corrected the login entry point                    |
| Only 10 of N records extracted            | `networkidle` fires before progressive AJAX loading completes          | Poll DOM `<li>` count against `data-cy-value`      |
| Record detail content empty               | `nc-list-post-container` starts `nu-hidden` and is AJAX-filled         | Wait for the class to drop and `innerHTML` to fill |

### Inkorg

Every one of these produced plausible-looking output rather than an error, which is why the guards below exist.

| Issue                                | Root Cause                                                                                     | Resolution                                                        |
| ------------------------------------ | ---------------------------------------------------------------------------------------------- | ----------------------------------------------------------------- |
| 40 rows found for a 32-item inbox    | Angular keeps all four tab panels mounted at once                                              | Scope queries with `:visible`, verify against the counter         |
| 17 of 32 threads lost                | `wait_for_selector("h1")` waits on the *first* `h1` — the permanently hidden inactivity dialog | Filter the match set: `text=… >> visible=true`                    |
| 15 threads exported empty at `32/32` | `wait_for_url` matched while Angular still had the previous view mounted                       | Wait for content only the destination has — the `Skickades` stamp |
| 15 s wasted per thread               | `get_by_text(...).first` picks the first DOM match, which is hidden                            | Same visible-filter fix                                           |
| `sent_at` empty on every message     | The colon in `Skickades:` does not survive the Markdown conversion                             | Make the colon optional in the pattern                            |
| `Arkiverade` indistinguishable       | An empty tab and an unreachable tab both yield zero rows                                       | Read the site's own "Du har inga … meddelanden" line              |

**The lesson that shaped the design:** the count guard reported a confident `32/32` while 15 of those threads held nothing at all. Verifying counts is not verifying content — a scraper needs guards at both levels, each checking what the other cannot see.

---

## Guarantees against silent partial exports

- Counts are checked against the total the page itself reports
- Empty content is an error in its own right, reported by subject and URL
- An empty tab is told from an unreachable one by the site's own empty-state line
- Each entry's whole detail page is stored before any attempt to split it
- An incomplete run saves what it collected and exits non-zero

---

## Output

```text
output/
├── anteckningar/
│   ├── md/YYYY-MM-DD.md                 # one file per date
│   └── json/anteckningar_YYYY-MM-DD.json
└── inkorg/
    ├── md/YYYY-MM-DD.md                 # one file per date, threads with nested messages
    └── json/inkorg_YYYY-MM-DD.json

scraper.log                              # full run log (project root)
```

---

## Constraints

- BankID authentication is manual — each run requires a fresh login, per host
- Sessions expire and are not reusable across runs
- The script accesses only the authenticated user's own data (GDPR Art. 15, 20)
- Inbox history is limited to two years by 1177's own retention policy
- Output files contain sensitive medical data and must be handled accordingly; `output/`, logs and any Playwright trace or HAR are git-ignored for that reason
