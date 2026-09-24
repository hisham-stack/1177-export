# journal-export-1177

A Playwright scraper that exports your own 1177 health data — journal records (_Anteckningar_) and inbox conversations (_Inkorg_) — into structured Markdown and JSON. Authentication is manual: you complete BankID in the browser window the script opens.

## What it exports

| Section        | Source                           | Unit of data                                      |
| -------------- | -------------------------------- | ------------------------------------------------- |
| `anteckningar` | `journalen.1177.se`              | one journal record per entry                      |
| `inkorg`       | `e-tjanster.1177.se/messageList` | a conversation (_ärende_) of one or more messages |

The two live on different 1177 sites with **independent sessions**, so exporting both normally means two BankID prompts.

> 1177 keeps inbox messages for **two years** only. A short Inkorg export reflects that retention limit, not a failure to find older messages.

## Prerequisites

- Python 3.10+
- A Swedish BankID (Mobile BankID or BankID on card)

## Installation

```bash
git clone <repo-url>
cd journal-export-1177

python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate

pip install -r requirements.txt
playwright install chromium
```

On Ubuntu 26.04 the last step fails with `ERROR: Playwright does not support chromium on ubuntu26.04-x64`, because the release is newer than Playwright's support list. Override the platform check:

```bash
PLAYWRIGHT_HOST_PLATFORM_OVERRIDE=ubuntu24.04-x64 playwright install chromium
```

## Running

```bash
source venv/bin/activate

python scraper.py                        # every section
python scraper.py --only inkorg          # just one
python scraper.py --only inkorg --clean  # ...after deleting its previous export
```

A Chromium window opens on 1177. **You have 2 minutes to complete BankID.** The script then works through each selected section on its own, so one section failing does not waste the login the other still needs. Progress goes to the terminal and to `scraper.log`.

`--clean` deletes the selected sections' previous exports first, so the output reflects exactly one run. It is opt-in rather than automatic: a run that fails halfway would otherwise wipe a good earlier export and replace it with a partial one.

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

Markdown files group entries by date with their type, author and care provider; the JSON holds the same data as an array for further processing. Anything with no detectable date lands in `utan-datum.md`.

> **Note:** output files contain sensitive personal medical data. The `output/` directory is git-ignored and should never be committed or shared.

## How it works

```text
scraper.py        # CLI: picks sections, owns the browser, saves results
common.py         # config, logging, login, harvest verification, output helpers
anteckningar.py   # journalen.1177.se
inkorg.py         # e-tjanster.1177.se
```

Each section is its own module exposing `scrape(page)` and `save(items)`; `scraper.py` dispatches through a `SECTIONS` dict, so adding a section means adding a module and one entry. They target different sites with unrelated markup, which is why they are separate — but they share one login, browser and output layer so the two cannot drift apart.

| Step | Function                                | Description                                                |
| ---- | --------------------------------------- | ---------------------------------------------------------- |
| 1    | `manual_login()`                        | Opens the site and waits up to 2 min for BankID            |
| 2    | `navigate()` / `select_tab()`           | Reaches the section, by link or tab                        |
| 3    | `load_all()` / `page_count()`           | Expands or pages through the full list                     |
| 4    | `extract_record()` / `extract_detail()` | Opens each entry, waits for its content, converts the HTML |
| 5    | `verify_harvest()`                      | Checks the haul against the count the page itself reports  |
| 6    | `save()`                                | Groups by date and writes the Markdown and JSON files      |

## Guarding against silent partial exports

A scraper that quietly returns half the data is worse than one that crashes, so the failure modes that produce plausible-looking output are made loud:

- **Counts are verified.** Each section compares what it collected against the total the page reports, and says so either way.
- **Empty content is an error.** A thread that yields no text is reported by name and URL — a count-based check cannot see this, because the item was collected, just hollow.
- **An empty tab and an unreachable tab are distinguished** by the site's own "you have no messages" line, so missing data never reads as an empty inbox.
- **Nothing is parsed destructively.** Each conversation's whole detail page is stored as Markdown _before_ being split into messages, so a missed card costs structure, never content.
- **An incomplete run still saves.** It reports the gap and exits non-zero, but keeps what it collected.

## Dependency management

Direct dependencies are listed in `requirements.in`. `requirements.txt` is the full pinned lockfile (including transitive dependencies) generated by `pip-tools`.

```bash
pip install pip-tools
pip-compile requirements.in
pip-sync requirements.txt
```

## Logs

`scraper.log` is written to the project root on every run and contains timestamped info and error messages. It is git-ignored.
