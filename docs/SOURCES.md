# Source research (2026-11-02)

How Philippine hackathons are announced, and which channels the scanner may use.

## Findings

| Channel | Examples found | Automated use | Decision |
|---|---|---|---|
| hackathons.ph | Existing PH aggregator; listed 55 hackathons (26 upcoming) on 2026-11-02 | Terms: "Do not spam, scrape, or abuse the service or its APIs" | **Blocked.** Ask them for a feed or partnership (contact on their site). Use as a reference when checking by hand only |
| Devpost | Online hackathons open to PH students (e.g. Cosmo Hacks 2026) | Terms prohibit automated access, scraping and crawling | **Blocked.** Links become signals |
| Luma | Manila hackathons (Voice AI Hackathon Manila, Agora Hackathon PH, Shipaton Manila; all past) | Terms limit access to publicly supported interfaces | **Blocked.** Links become signals |
| Facebook / Instagram / X / LinkedIn | Most DICT regional and university announcements | Meta and others prohibit automated collection | **Blocked.** Manual entry only |
| Google News RSS | Headlines on PH hackathons and CTFs | Public RSS feed | **Enabled**, headlines only (article links redirect with JavaScript) |
| Government sites (DICT, PIA, PNA) | Hackathon and HackForGov news | Public sites; robots.txt checked per run | **Enabled** as listings |
| NASA Space Apps | 2026 hackathon Nov 14-15, local events + virtual Universal Event | Official site | **Manual entry**; its local-events directory is disallowed by robots.txt |
| GDG community pages | InnOlympics 2026 (Quezon City, past) | Terms not reviewed | Not enabled. Review terms first |
| Google Alerts RSS | Your own alerts | RSS made for this | Template in sources.yaml, needs your feed URL |

## Why so few verified events at launch

On 2026-11-02 the only currently open event confirmed on an official page was NASA Space Apps
Challenge 2026. The other upcoming PH events found were listed on hackathons.ph without an
official page that could be checked, or on platforms the scanner must not read. They are
not copied into this project.

## Best ways to grow coverage, in order

1. Google Alerts RSS for "hackathon Philippines", "hackathon Iloilo", "CTF Philippines" etc.
2. Organizer pages that post announcements on their own sites (universities, DICT regional
   offices, tech communities). Add each as a `listing` after reading its terms.
3. A data agreement with hackathons.ph.
4. Manual entries for Facebook-only announcements.
