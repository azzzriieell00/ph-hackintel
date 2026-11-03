# PH HackIntel Map

A red-team themed map of hackathons and CTF / cybersecurity competitions in the Philippines,
fed by an automated scanner. Round pins are hackathons; diamond pins are CTFs; dim pins are
events held in about the last two months.

- **Site**: one static page (`index.html`). 2D dot-matrix map of the country, pins for verified
  events, intel panel with live deadline countdown and a confidence score, 3D city view on pin
  click, Signals list, and a **Brief** tab that analyses the current data.
- **Scanner**: Python pipeline (`pipeline/`) that reads Philippine university, organizer and news
  websites every hour, ranks what it finds, extracts event details (structured data, or AI reading
  with strict checks), locates them, verifies the official link and writes `data/events.json`.
- **Automation**: a GitHub Actions workflow runs the scanner hourly and publishes the site on
  GitHub Pages. The open page re-reads `data/events.json` every 10 minutes.

No sample or invented events. Everything shown is either verified (published) or clearly
marked as an unverified signal.

## Where the data comes from

There is no single feed of Philippine hackathons. Announcements are spread across Facebook
pages, event platforms, school and agency sites and news. The scanner uses only sources it is
allowed to read automatically, and routes everything else to a person.

| Layer | What it is | Published as |
|---|---|---|
| **Websites** (`site`) | 30 Philippine university, community and news sites that came up in web searches for Philippine hackathon and CTF news (2026-11-03). The scanner finds each site's feed by itself, reads it hourly, and once a day reads homepage links and the site's own search | Events after checks; headlines as Signals |
| **Event pages** | Official pages of specific events (usually linked from a Facebook post) | Events after checks |
| **CTFtime API** | Onsite CTFs whose location is in the Philippines | Events (diamond pins) |
| **Manual events** | `config/manual_events.yaml`: entries a person copied from an official page. Valid for 7 days per check | Events |
| **Signals** | Headlines that mention a competition, links to platforms the scanner must not open (Facebook, Luma, Devpost, hackathons.ph), and competitions without a confirmed venue or date | "Signals" tab, marked unverified |

The full list with reasons is in `config/sources.yaml`; the research behind it is in `docs/SOURCES.md`.

## How the scanner thinks (pipeline/run.py)

1. **Gather** links from every source.
2. **Rank** them: competition words, "registration"/"deadline", Philippine places and recent
   dates first; old posts last.
3. **Read** the best ones first (15 per source, 60 per scan, 12 minutes). Structured event data is
   used directly; other pages go to the AI, cached by page content, within a daily budget. Pages
   that could not be read yet wait in a queue for the next scan.
4. **Check AI answers**: dates and places must be written in the page, links must appear in the
   page, years must be plausible, a deadline may not fall after the event. Failing details are
   dropped and shown as "AI checks".
5. **Merge duplicates**: same official page, same name, or the same competition under another
   name (same category, start within a day, same province, same distinctive words).
6. **Locate, link-check, decide, score** (below).

## Publication rules (pipeline/store.py)

An upcoming event is shown only if all of these hold:

1. It has a deadline, or an event start date (then the page says no deadline was stated).
2. Its official/registration link answered within the last 72 hours, or a person checked it
   within the last 7 days. Three failed link checks in a row hide it.
3. It has coordinates inside the Philippines and inside the province the announcement names
   (checked against PSGC boundaries). Online events and events without a confirmed Philippine
   venue go to Signals instead.
4. Its country is the Philippines (or not stated).

Past events stay on the map for about two months (`PAST_WINDOW_D = 62`) if they have a confirmed
location and a public source link. Everything held back is written to `data/review.json` with the
reason, together with the links waiting for the AI.

## Confidence score

Each event carries a 0-100 score with its reasons, shown in the intel panel:

| Points | Reason |
|---|---|
| +30 | entered by a person, structured data on the page, or a public API |
| +15 | read by AI from the page text (and passed the checks); -5 if some details failed |
| +20 | official link answered in the last 72 h (upcoming) / documented by a published source (past) |
| +20 / +10 | checked by a person in the last 7 days (upcoming) / at any time (past) |
| +15 | pin is inside the province the source names (-20 if they disagree) |
| +5 | venue located exactly; results reported (past) |
| +10 per extra site, max +20 | corroborated by independent websites |
| -10 | no registration deadline stated; official link failing |

HIGH is 75 or more, MEDIUM 50-74, LOW below 50.

## Setup (GitHub Pages, free)

1. Create a GitHub repository and push this folder to the `main` branch.
2. Settings > Pages > Build and deployment > Source: **GitHub Actions**.
3. Settings > Secrets and variables > Actions:
   - Variable `HACKINTEL_CONTACT`: an email or URL. It goes in the scanner's User-Agent;
     OpenStreetMap's geocoder requires a contact.
   - Secret `GEMINI_API_KEY` (recommended) or `ANTHROPIC_API_KEY`: turns on AI reading for pages
     without structured event data. Without a key those pages are listed in `data/review.json`
     as "pages_needing_llm". With Gemini the scanner picks the newest Flash-Lite model
     automatically (override with the variable `HACKINTEL_GEMINI_MODEL`) and stays under a request
     budget: 12 per scan, 120 per day, one every 6 s (`HACKINTEL_LLM_RUN_CAP`,
     `HACKINTEL_LLM_DAILY_CAP`, `HACKINTEL_LLM_MIN_INTERVAL`). After a quota answer (HTTP 429)
     the AI pauses until the quota resets. If both keys are set, Gemini is used unless the variable
     `HACKINTEL_LLM_PROVIDER` is `anthropic`.
   - Optional variables: `HACKINTEL_SCAN_BUDGET_S` (default 720 s), `HACKINTEL_MAX_PAGES` (60).
4. Actions tab > "Scan and deploy" > **Run workflow** for the first run. After that it runs
   every hour at :17.

## Run locally

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows      |  source .venv/bin/activate  (Linux)
pip install -r requirements.txt
python -m pytest -q               # 59 tests, no internet needed
python -m pipeline.run            # one scan (needs internet)
python tools/build_page.py        # re-embed the snapshot into index.html
python -m http.server 8000        # open http://localhost:8000
```

| Command / Option | Meaning | Purpose |
|---|---|---|
| `python -m pipeline.run` | Run one scan | Updates `data/events.json` and `data/review.json` |
| `--offline` | No network | Re-evaluates manual entries and existing data only |
| `--only SOURCE_ID` | One source | Debug a single entry from `config/sources.yaml` |
| `python tools/build_page.py` | Build the page | Embeds the boundary data and a snapshot of the events |
| `python -m http.server 8000` | Static server | Lets the page load the live `data/events.json` |

## Adding sources

Edit `config/sources.yaml`. The easiest is a whole website; only its address is needed:

```yaml
  - id: my-school
    name: My School news
    type: site
    url: https://www.myschool.edu.ph/
```

Other types: `event_page` (one official event page, read every scan), `listing` (follows matching
links on a news page), `rss` (`leads_only: true` keeps headlines without opening them), `ctftime`.
The scanner obeys robots.txt (including wildcard rules and Crawl-delay) and waits at least 2 s
between requests to one host. Each source's result is in `data/events.json` > `sources`.

## Adding an event by hand (Facebook and other closed platforms)

The scanner does not read Facebook (Meta's terms prohibit automated collection). To add an event
you saw there, run the helper and answer the questions with what the post states:

```bash
python tools/add_event.py
```

It checks the city against the Philippine province boundaries (or asks for coordinates from
Google Maps), builds the entry and appends it to `config/manual_events.yaml`. Then:

```bash
git add config/manual_events.yaml
git commit -m "Add <event name>"
git push
```

The push triggers a scan, and the event appears on the map within a few minutes. If the post
links to an official page, also add that page as an `event_page` source: the scanner then reads it
every hour, picks up changes such as a moved deadline, and merges it with your hand entry (your
entry wins).

## Files

```
index.html                    built page (generated)
src/index.template.html       page source: HTML, CSS, JS
pipeline/                     scanner: fetch.py, extract.py, geo.py, store.py, run.py, config.py
config/sources.yaml           where to look, and blocked hosts
config/manual_events.yaml     hand-checked events
data/events.json              scanner output the page reads (events, signals, sources, scan stats)
data/review.json              held-back items with reasons, links waiting for the AI
data/venues.json              gazetteer of city centres (each tested against PSGC boundaries)
data/geo.json                 map dots and province outlines (generated by tools/build_geo.py)
data/state/                   geocode cache, page and site state, AI cache and budget
tests/                        pytest suite with a local fixture website
.github/workflows/            hourly scan and Pages deploy
vendor/maplibre-gl.js         MapLibre GL JS 4.7.1 for the 3D view
docs/SOURCES.md               source research and the reasons behind each decision
```

## What is verified and what is not

- Tested here: 59 tests (extraction, feed discovery, link ranking, robots.txt wildcards and redirects, AI
  answer checks, duplicate merging, confidence, CTFtime, location checks, publication rules, the AI
  cache and waiting queue, and end-to-end scans of a local fixture website); the page in headless
  Chrome at desktop and phone width with new data and with the older data format.
- **Not tested here**: scans against the real websites and the 3D view with real map tiles. The
  build machine had no general internet access. The first GitHub Actions run after installing is
  the real test: check `data/events.json` > `sources` for per-source errors, and the Brief tab.

## Credits

Boundaries: PSGC-based maps from `faeldon/philippines-json-maps` (MIT). 3D tiles: OpenFreeMap,
OpenMapTiles, OpenStreetMap contributors. Geocoding: Nominatim / OpenStreetMap. CTF calendar:
CTFtime. Map engine: MapLibre GL JS (BSD-3-Clause).
