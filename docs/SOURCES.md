# Source research

How Philippine hackathons and CTFs are announced, and which channels the scanner may use.
First written 2026-11-02, updated 2026-11-03 (fix 8: website discovery, CTFtime, confidence).

## Findings

| Channel | Examples found | Automated use | Decision |
|---|---|---|---|
| hackathons.ph | Existing PH aggregator; listed 55 hackathons (26 upcoming) on 2026-11-02 | Terms: "Do not spam, scrape, or abuse the service or its APIs" | **Blocked.** Ask them for a feed or partnership. Use as a reference when checking by hand only |
| Devpost | Online hackathons open to PH students | Terms prohibit automated access, scraping and crawling | **Blocked.** Links become signals |
| Luma | Manila hackathons (Build For Bayan, Shipaton Manila, Voice AI Hackathon Manila) | Terms limit access to publicly supported interfaces | **Blocked.** Links become signals |
| Facebook / Instagram / X / LinkedIn | Most DICT regional, student council and org announcements | Meta and others prohibit automated collection | **Blocked.** Manual entry (`tools/add_event.py`); the official site the post links to is added as an `event_page` |
| University news sites | Bicol U, RTU, CarSU, USM, UP Tacloban, CIT-U, UP Manila, AdZU, SLU, Silliman, CHMSU, IMCC, USeP, NU, UP Mindanao, GSU, VSU, NDMC, FEU Tech, U. of Antique, HAU: posts about hackathons or CTFs came up in web searches (2025-2026 posts) | Public pages and feeds; robots.txt obeyed | **Enabled** as `site` sources |
| PH tech and regional news | NewsBytes.PH, Daily Tribune, Mindanao Gold Star Daily, SunStar (Hackathon tag), Cebu Daily News, The POST, InsiderPH, BitPinas, Inquirer Technology, PNA | Public feeds and pages; robots.txt obeyed | **Enabled** |
| Communities and agencies | DOST-ASTI, DEVCON Philippines, Circuitrocks events | Public sites | **Enabled** as `site` sources |
| CTFtime | Global CTF calendar with onsite locations | Public API "provided for data analysis and mobile applications only", no clones. robots.txt: Crawl-delay 10 | **Enabled**: only onsite CTFs located in the Philippines, linked back to CTFtime, read twice a day |
| Google News RSS | Headlines on PH hackathons and CTFs | robots.txt disallows /rss/search for our bot | **Off** |
| DICT, PIA | HackForGov news | HTTP 403 to GitHub's network | **Off**; we do not work around a refusal |
| GDG community pages (Bevy) | InnOlympics 2026, Hackfest | Terms not reviewed | **Not enabled.** Review terms first |
| Google Alerts RSS | Your own alerts | RSS made for this | Template in sources.yaml, needs your feed URL |

## Gemini "Grounding with Google Search" is not used for discovery

Asking Gemini to search Google would find more pages, but the Gemini API terms for grounded
results allow showing them only to the person who asked, forbid storing the links, and forbid
using automated means to "build an index, or identify pages to crawl or scrape". A scanner that
saves events to a public map is exactly that, so the AI is used only to **read** pages the
scanner was allowed to fetch.

## How the scanner decides what to read (fix 8)

1. **Gather.** Each source adds links: feed items, homepage links that mention a competition,
   and once a day the site's own search (WordPress sites, if robots.txt allows `?s=`).
2. **Rank.** `extract.link_score`: competition words in the title, words like "registration" or
   "deadline", Philippine place names and recent dates rank higher; posts older than ~2.5 months sink.
3. **Read.** Best links first, at most 15 per source and 60 per scan, and nothing new after
   12 minutes. Structured event data (schema.org) is used directly. Other pages go to the AI,
   whose answers are cached by page content, so an unchanged page is never sent twice.
   Pages the AI could not read yet (budget, quota) wait in a queue for later scans.
4. **Check the AI.** Dates must be written in the page (e.g. "Dec 3", "3 Disyembre", "12/03/2026"),
   years must be plausible, a deadline may not fall after the event, places must appear in the
   page, links must appear in the page. Whatever fails is dropped and listed under "AI checks".
5. **Merge.** The same competition from two sources becomes one pin with both sources: same
   official page, same name, or (new) same category, start date within a day, no conflicting
   province and mostly the same distinctive name words.
6. **Score.** Every event gets a 0-100 confidence with its reasons (who entered it, link check,
   manual check, location match, number of independent websites, stated deadline).

## Best ways to grow coverage, in order

1. Add any school, organizer or community website to `config/sources.yaml` as a `site`
   (only the address is needed; the scanner finds its feed).
2. When a Facebook post links to an official page, add that page as an `event_page`.
3. Google Alerts RSS for "hackathon Philippines", "CTF Philippines" etc.
4. A data agreement with hackathons.ph.
5. Manual entries (`tools/add_event.py`) for Facebook-only announcements.
