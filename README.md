# PH HackIntel Map

A red-team themed map of hackathons in the Philippines, fed by an automated scanner.

- **Site**: one static page (`index.html`). 2D dot-matrix map of the country, pins for verified
  events, intel panel with live deadline countdown, 3D city view on pin click, Signals list.
- **Scanner**: Python pipeline (`pipeline/`) that checks public sources every hour, extracts
  event details, locates them, verifies the official link, and writes `data/events.json`.
- **Automation**: a GitHub Actions workflow runs the scanner hourly and publishes the site on
  GitHub Pages. The open page re-reads `data/events.json` every 10 minutes.

No sample or invented events. Everything shown is either verified (published) or clearly
marked as an unverified signal.

## Where the data comes from

There is no single feed of Philippine hackathons. Announcements are spread across Facebook
pages, event platforms, agency and university sites and news. The scanner uses only sources it
is allowed to read automatically, and routes everything else to a person.

| Layer | What it is | Published as |
|---|---|---|
| **Verified events** | Announcements read from public pages (schema.org event data, or AI reading of the page text when an API key is set), located, and whose official link answered in the last 72 h | Pins and list entries |
| **Manual events** | `config/manual_events.yaml`: entries a person copied from an official page. Valid for 7 days per check | Pins and list entries |
| **Signals** | News headlines that mention hackathons, and links to platforms the scanner must not open (Facebook, Luma, Devpost, hackathons.ph) | "Signals" tab, marked unverified |

Platforms excluded from automated access, and why, are listed in `config/sources.yaml`
(`blocked_hosts`) and explained in `docs/SOURCES.md`.

## Publication rules (pipeline/store.py)

An event is shown only if all of these hold:

1. It has a deadline, or an event start date (then the page says no deadline was stated).
2. Its official/registration link answered within the last 72 hours, or a person checked it
   within the last 7 days. Three failed link checks in a row hide it.
3. An onsite event has coordinates that fall inside the Philippines and inside the province the
   announcement names (checked against PSGC boundaries). Hybrid events without a confirmed
   Philippine venue are listed without a pin.
4. Its country is the Philippines (or not stated).

Closed events stay in the *Closed* filter for 30 days. Everything held back is written to
`data/review.json` with the reason.

## Setup (GitHub Pages, free)

1. Create a GitHub repository and push this folder to the `main` branch.
2. Settings > Pages > Build and deployment > Source: **GitHub Actions**.
3. Settings > Secrets and variables > Actions:
   - Variable `HACKINTEL_CONTACT`: an email or URL. It goes in the scanner's User-Agent;
     OpenStreetMap's geocoder requires a contact.
   - Secret `ANTHROPIC_API_KEY` (optional): turns on AI extraction for pages without structured
     event data. Without it those pages are listed in `data/review.json` as "pages_needing_llm".
   - Variable `HACKINTEL_LLM_MODEL` (optional): the model used for extraction, default `claude-haiku-5-5`.
4. Actions tab > "Scan and deploy" > **Run workflow** for the first run. After that it runs
   every hour at :17.

## Run locally

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows      |  source .venv/bin/activate  (Linux)
pip install -r requirements.txt
python -m pytest -q               # 16 tests, no internet needed
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

Opening `index.html` straight from disk shows the embedded snapshot (header chip says
SNAPSHOT). Served over http it loads the current file (chip says LIVE).

## Adding sources

Edit `config/sources.yaml`. Types: `event_page`, `listing` (follows matching links on a news
page), `rss` (`leads_only: true` keeps headlines without opening them). Before enabling a site,
read its terms; the scanner checks robots.txt itself and waits 2 s between requests per host.
The fastest way to add coverage is your own **Google Alerts** feed (instructions in the file).

## Adding an event by hand

Found one on Facebook or Luma? Open the official page, then add an entry to
`config/manual_events.yaml` with only what the page states and `verified_manually` set to now.
Commit; the next run publishes it.

## Files

```
index.html                    built page (generated)
src/index.template.html       page source: HTML, CSS, JS
pipeline/                     scanner: fetch.py, extract.py, geo.py, store.py, run.py, config.py
config/sources.yaml           where to look, and blocked hosts
config/manual_events.yaml     hand-checked events
data/events.json              scanner output the page reads
data/review.json              held-back items with reasons
data/venues.json              gazetteer of city centres (each tested against PSGC boundaries)
data/geo.json                 map dots and province outlines (generated by tools/build_geo.py)
data/state/                   geocode cache and link-check history
tests/                        pytest suite with a local fixture website
.github/workflows/            hourly scan and Pages deploy
vendor/maplibre-gl.js         MapLibre GL JS 4.7.1 for the 3D view
docs/SOURCES.md               source research and the reasons behind each decision
```

## What is verified and what is not

- Tested here: 16 pipeline tests (extraction, location checks, publication rules, an
  end-to-end scan of a local fixture website, offline mode); the page in headless Chrome at
  desktop and phone width with live, snapshot and empty data.
- **Not tested here**: scans against the real sources and the 3D view with real map tiles. The
  build machine had no general internet access. The first GitHub Actions run is the real test:
  check `data/events.json` > `sources` for per-source errors and `data/review.json`.

## Credits

Boundaries: PSGC-based maps from `faeldon/philippines-json-maps` (MIT). 3D tiles: OpenFreeMap,
OpenMapTiles, OpenStreetMap contributors. Geocoding: Nominatim / OpenStreetMap. Map engine:
MapLibre GL JS (BSD-3-Clause).
