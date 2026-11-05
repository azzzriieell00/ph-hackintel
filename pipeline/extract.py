"""Turn fetched pages into candidate event records.

Two extraction paths:
  * schema.org Event JSON-LD embedded in the page (exact, preferred)
  * optional AI extraction of the visible text (Gemini or Anthropic, whichever key is set)
Nothing is guessed: fields the page does not state stay empty.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
import urllib.parse
from datetime import datetime, timedelta, timezone

from bs4 import BeautifulSoup
from dateutil import parser as dtparser

from . import config

PHT = timezone(timedelta(hours=8))
KW_RE = re.compile(r"\b(" + "|".join(re.escape(k) for k in config.KEYWORDS) + r")\b", re.I)
PH_RE = re.compile(r"\b(philippines|pilipinas|filipino|manila|cebu|davao|iloilo|luzon|visayas|mindanao)\b", re.I)


CTF_RE = re.compile(config.CTF_PATTERN, re.I)


def norm(s) -> str:
    """NFKC: turns the bold/italic Unicode letters common in Facebook-style titles (𝗛𝗮𝗰𝗸) into plain text."""
    return unicodedata.normalize("NFKC", s or "")


def is_relevant(*texts: str) -> bool:
    return any(KW_RE.search(norm(t)) for t in texts)


def classify(ev: dict) -> str:
    """'ctf' for capture-the-flag and cybersecurity competitions, otherwise 'hackathon'."""
    if (ev.get("category") or "").lower() in ("ctf", "hackathon"):
        return ev["category"].lower()
    text = " ".join([ev.get("name") or "", ev.get("description") or "", " ".join(ev.get("themes") or [])])
    return "ctf" if CTF_RE.search(text) else "hackathon"


def parse_dt(value, end_of_day: bool = False) -> str | None:
    """ISO 8601 with offset. Naive times are taken as Philippine time.
    end_of_day: a bare date (no time) becomes 23:59 that day, which is how deadlines are meant."""
    if not value:
        return None
    try:
        d = dtparser.isoparse(value) if isinstance(value, str) and "T" in value else dtparser.parse(str(value))
    except (ValueError, OverflowError, TypeError):
        return None
    if d.tzinfo is None:
        if end_of_day and not re.search(r"\d:\d", str(value)):
            d = d.replace(hour=23, minute=59)   # a bare date as a deadline means end of that day
        d = d.replace(tzinfo=PHT)
    return d.isoformat()


def clean_url(u: str | None, base: str | None = None) -> str | None:
    if not u or not isinstance(u, str):
        return None
    u = urllib.parse.urljoin(base or "", u.strip())
    p = urllib.parse.urlsplit(u)
    if p.scheme not in ("http", "https"):
        return None
    return urllib.parse.urlunsplit((p.scheme, p.netloc.lower(), p.path, p.query, ""))


def page_text(html: str, limit: int = 14000) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript", "svg", "nav", "footer", "header"]):
        t.decompose()
    text = re.sub(r"\n{3,}", "\n\n", norm(soup.get_text("\n", strip=True)))
    return text[:limit]


def page_title(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    return norm(soup.title.string).strip() if soup.title and soup.title.string else ""


def text_hash(text: str) -> str:
    return hashlib.sha1(re.sub(r"\s+", " ", text).encode("utf-8")).hexdigest()[:16]


# ------------------------------------------------------------------ JSON-LD
def _iter_nodes(obj):
    if isinstance(obj, list):
        for x in obj:
            yield from _iter_nodes(x)
    elif isinstance(obj, dict):
        yield obj
        if "@graph" in obj:
            yield from _iter_nodes(obj["@graph"])


def _types(node) -> list[str]:
    t = node.get("@type", [])
    return [x.split("/")[-1] for x in (t if isinstance(t, list) else [t]) if isinstance(x, str)]


def _first(v):
    return v[0] if isinstance(v, list) and v else v


def _name(v):
    v = _first(v)
    if isinstance(v, dict):
        return v.get("name")
    return v if isinstance(v, str) else None


def jsonld_events(html: str, page_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for tag in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        try:
            data = json.loads(tag.string or tag.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for node in _iter_nodes(data):
            if not any(t.endswith("Event") for t in _types(node)):
                continue
            ev = _from_jsonld(node, page_url)
            if ev:
                out.append(ev)
    return out


def _text(v) -> str:
    """Plain text from a JSON-LD value that may be a string, a list, or {"@value": ...}."""
    v = _first(v)
    if isinstance(v, dict):
        v = v.get("@value") or v.get("name") or ""
    return norm(v).strip() if isinstance(v, str) else ""


def _from_jsonld(n: dict, page_url: str) -> dict | None:
    name = _text(n.get("name"))
    if not name:
        return None
    mode_raw = str(n.get("eventAttendanceMode", ""))
    venue = city = province = country = None
    lat = lon = None
    physical = virtual = False
    locs = n.get("location")
    for loc in (locs if isinstance(locs, list) else [locs]):
        if isinstance(loc, str) and loc.strip():
            physical = True
            venue = venue or loc.strip()
            continue
        if not isinstance(loc, dict):
            continue
        if "VirtualLocation" in _types(loc):
            virtual = True
            continue
        physical = True
        venue = venue or _text(loc.get("name")) or None
        addr = _first(loc.get("address"))
        if isinstance(addr, dict):
            city = city or _text(addr.get("addressLocality")) or None
            province = province or _text(addr.get("addressRegion")) or None
            country = country or _text(addr.get("addressCountry")) or None
        elif isinstance(addr, str) and not venue:
            venue = addr
        geo = loc.get("geo")
        if isinstance(geo, dict):
            try:
                lat, lon = float(geo.get("latitude")), float(geo.get("longitude"))
            except (TypeError, ValueError):
                pass
    if "Mixed" in mode_raw or (physical and virtual):
        mode = "hybrid"
    elif "Online" in mode_raw or (virtual and not physical):
        mode = "online"
    else:
        mode = "onsite"
    offers = _first(n.get("offers"))
    deadline = parse_dt(offers.get("validThrough"), end_of_day=True) if isinstance(offers, dict) else None
    start = parse_dt(n.get("startDate"))
    org = _name(n.get("organizer"))
    return {
        "name": name,
        "organizer": org if isinstance(org, str) else None,
        "description": _text(n.get("description"))[:600],
        "mode": mode,
        "venue": venue, "city": city, "province": province, "country": country,
        "lat": lat, "lon": lon, "location_source": "source" if lat is not None else None,
        "start": start, "end": parse_dt(n.get("endDate")),
        "deadline": deadline or start,
        "deadline_is_estimate": deadline is None,
        "registration_url": clean_url((offers or {}).get("url") if isinstance(offers, dict) else None, page_url) or clean_url(n.get("url"), page_url) or page_url,
        "official_url": clean_url(n.get("url"), page_url) or page_url,
        "status_note": str(n.get("eventStatus", "")).split("/")[-1] or None,
        "extraction": "jsonld",
    }


# ------------------------------------------------------------------ listings
def candidate_links(html: str, base_url: str, allow_hosts: list[str] | None, pattern: str | None,
                    offsite: list | None = None, with_text: bool = False) -> list:
    """Links on a listing page that look like hackathon announcements.
    Matching links to other hosts are not returned; when `offsite` is a list they are appended
    to it as (url, link text) so the caller can record them as leads.
    with_text=True returns (url, link text) pairs instead of bare URLs."""
    soup = BeautifulSoup(html, "html.parser")
    rx = re.compile(pattern, re.I) if pattern else None
    seen, out = set(), []
    www = lambda h: h.lower()[4:] if h.lower().startswith("www.") else h.lower()
    base_host = urllib.parse.urlsplit(base_url).netloc.lower()
    hosts = {www(h) for h in (allow_hosts or [base_host])}
    for a in soup.find_all("a", href=True):
        u = clean_url(a["href"], base_url)
        if not u or u in seen:
            continue
        host = www(urllib.parse.urlsplit(u).netloc)
        text = norm(a.get_text(" ", strip=True)) or norm(a.get("title") or "")
        if rx and not (rx.search(u) or rx.search(text)):
            continue
        if not rx and not is_relevant(text, u.replace("-", " ")):
            continue
        seen.add(u)
        if host not in hosts:
            if offsite is not None:
                offsite.append((u, text))
            continue
        out.append((u, text) if with_text else u)
    return out


# ------------------------------------------------------------------ site discovery
FEED_TYPES = ("application/rss+xml", "application/atom+xml", "application/feed+xml")


def discover_feeds(html: str, base_url: str) -> list[str]:
    """Feeds a site announces in its <head> (<link rel="alternate" type="application/rss+xml">).
    Comment feeds are skipped; the main posts feed comes first."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for link in soup.find_all("link", href=True):
        rel = " ".join(link.get("rel") or []).lower()
        typ = (link.get("type") or "").lower().split(";")[0].strip()
        if "alternate" not in rel or typ not in FEED_TYPES:
            continue
        title = (link.get("title") or "").lower()
        u = clean_url(link["href"], base_url)
        if not u or "comment" in title or "/comments/" in u:
            continue
        if u not in out:
            out.append(u)
    return out


def is_wordpress(html: str) -> bool:
    h = html[:200000].lower()
    return ("/wp-content/" in h or "/wp-includes/" in h or "api.w.org" in h
            or bool(re.search(r'<meta[^>]+name=["\']generator["\'][^>]+wordpress', h)))


def feed_items(content: bytes | str, base_url: str | None = None) -> list[dict]:
    """Entries of an RSS/Atom feed as {title, link, summary, published (datetime|None)}.
    Relative links are resolved against base_url (the feed's own address)."""
    import feedparser
    feed = feedparser.parse(content)
    out = []
    for it in feed.entries:
        pub = None
        for key in ("published_parsed", "updated_parsed"):
            if it.get(key):
                pub = datetime(*it[key][:6], tzinfo=timezone.utc)
                break
        link = clean_url(it.get("link"), base_url)
        if link and "google.com/url" in link:   # Google Alerts wraps results in a redirect
            link = clean_url((urllib.parse.parse_qs(urllib.parse.urlsplit(link).query).get("url") or [None])[0])
        if not link:
            continue
        src = it.get("source")
        out.append({"title": norm(it.get("title", "")).strip(), "link": link,
                    "summary": norm(BeautifulSoup(it.get("summary", "") or "", "html.parser").get_text(" ", strip=True))[:1000],
                    "published": pub, "source": src.get("title", "") if isinstance(src, dict) else ""})
    return out


STRONG_RE = re.compile(r"\b(hackathon|hack-a-thon|capture[- ]the[- ]flag|ctf|hackforgov|hack4gov|buildathon|ideathon|"
                       r"datathon|hackfest|codefest|cyber ?security (competition|challenge)|cyber defen[cs]e (competition|exercise))\b", re.I)
ANNOUNCE_RE = re.compile(r"\b(registration|register|call for (participants|entries|teams)|open(s)? (to|for)|deadline|"
                         r"join|apply|application|now open|happening|upcoming|invit)", re.I)
RESULT_RE = re.compile(r"\b(wins?|won|champion|bags?|clinch|tops?|places?|runner[- ]up|finalists?|award)", re.I)
URL_DATE_RE = re.compile(r"/(20\d\d)/(0[1-9]|1[0-2])(?:/([0-3]\d))?/")


def link_score(title: str, url: str, published: datetime | None = None, now: datetime | None = None) -> int:
    """How promising a link is, so the best pages are read first when the AI budget is small.
    Strong competition words in the title count most; announcements and recent posts rank higher;
    old posts sink (past events are only kept for about two months)."""
    now = now or datetime.now(timezone.utc)
    t, u = norm(title), urllib.parse.unquote(url or "").replace("-", " ").replace("_", " ")
    score = 0
    if STRONG_RE.search(t):
        score += 50
    elif STRONG_RE.search(u):
        score += 30
    elif is_relevant(t, u):
        score += 10
    if ANNOUNCE_RE.search(t):
        score += 15
    if RESULT_RE.search(t):
        score += 8
    if PH_RE.search(t):
        score += 5
    if published is None:
        m = URL_DATE_RE.search(url or "")
        if m:
            try:
                published = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3) or 1), tzinfo=timezone.utc)
            except ValueError:      # e.g. /2026/02/30/ is not a date
                published = None
    if published is not None:
        age = (now - published).days
        score += 25 if age <= 14 else 15 if age <= 45 else 5 if age <= 75 else -40
    return score


def page_published(html: str) -> datetime | None:
    """When the page itself was published, from its metadata (article:published_time, schema.org
    datePublished, or the first <time datetime>). None when the page does not say."""
    soup = BeautifulSoup(html, "html.parser")
    cands = []
    for attr, val in (("property", "article:published_time"), ("name", "article:published_time"),
                      ("itemprop", "datePublished"), ("name", "date"), ("name", "pubdate")):
        tag = soup.find("meta", attrs={attr: val})
        if tag and tag.get("content"):
            cands.append(tag["content"])
    for tag in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        try:
            data = json.loads(tag.string or tag.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for node in _iter_nodes(data):
            if isinstance(node.get("datePublished"), str) and not any(t.endswith("Event") for t in _types(node)):
                cands.append(node["datePublished"])
    t = soup.find("time", attrs={"datetime": True})
    if t:
        cands.append(t["datetime"])
    for c in cands:
        try:
            d = dtparser.isoparse(c.strip()) if "T" in c else dtparser.parse(c.strip())
        except (ValueError, OverflowError, TypeError):
            continue
        return d if d.tzinfo else d.replace(tzinfo=PHT)
    return None


# ------------------------------------------------------------------ date checks for AI output
_MONTHS = [("january", "enero"), ("february", "pebrero"), ("march", "marso"), ("april", "abril"), ("may", "mayo"),
           ("june", "hunyo"), ("july", "hulyo"), ("august", "agosto"), ("september", "setyembre"),
           ("october", "oktubre"), ("november", "nobyembre"), ("december", "disyembre")]


def _month_rx(m: int) -> str:
    en, fil = _MONTHS[m - 1]
    names = {en, fil, en[:3], fil[:3]} | ({"sept"} if m == 9 else set())
    return r"(?:" + "|".join(sorted((re.escape(n) for n in names), key=len, reverse=True)) + r")\.?"


def date_mentioned(value: str | None, text: str) -> bool:
    """Does the page text state this calendar date? Accepts "Dec 3", "December 3rd", "3 Disyembre",
    "Nov 27-28" and "27-28 November" (either day), "12/03/2026", "2026-12-03". When a year is written
    right after the day ("December 3, 2025") it must be the same year. Used to reject dates an AI made up."""
    if not value:
        return False
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(value))
    if not m:
        return False
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    t = norm(text).lower()
    mon, day, yy = _month_rx(mo), str(d), str(y)[2:]
    suf = r"(?:st|nd|rd|th)?"
    dash = r"\s*(?:-|–|—|to|until|and|&)\s*"
    worded = [
        rf"\b{mon}\s*0?{day}{suf}\b",                                  # December 3, Dec. 03, Dec 3rd
        rf"\b0?{day}{suf}\s*(?:of\s+|ng\s+)?{mon}(?![a-z])",           # 3 December, 3rd of Dec
        rf"\b{mon}\s*\d{{1,2}}{suf}{dash}0?{day}{suf}\b",              # Nov 27-28 (end day)
        rf"\b0?{day}{suf}{dash}\d{{1,2}}{suf}\s*(?:of\s+|ng\s+)?{mon}(?![a-z])",   # 27-28 November (start day)
    ]
    for p in worded:
        for hit in re.finditer(p, t):
            # the year written next to it, if any, must agree ("December 3, 2025" is not 2026-12-03)
            after = re.match(r"[\s,.]*(?:\d{1,2}(?:st|nd|rd|th)?[\s,.]*)?(\d{4})\b", t[hit.end():hit.end() + 16])
            if not after or after.group(1) == str(y):
                return True
    numeric = [
        rf"\b0?{mo}[/.-]0?{day}[/.-](?:20)?{yy}\b",                    # 12/03/2026, 12-3-26
        rf"\b0?{day}[/.-]0?{mo}[/.-](?:20)?{yy}\b",                    # 03/12/2026 (day first)
        rf"\b{y}-0?{mo}-0?{day}\b",
    ]
    return any(re.search(p, t) for p in numeric)


# ------------------------------------------------------------------ LLM
LLM_PROMPT = """You extract hackathon and CTF events from web pages (announcements, or reports of events already held)
for a public map of Philippine competitions. Today is {today} (Philippine time).
Read the page text and answer with ONLY a JSON object, no prose. Use null for anything the page does not state.
Never guess dates, venues or links. Copy dates only as the page states them; if a date has no year, use the year
the page context makes certain, otherwise null.

{{
  "events": [                          // [] if the page is not about a specific competition; at most 5 items
    {{
      "is_hackathon": true|false,      // hackathon, CTF (capture the flag), cybersecurity competition, buildathon, ideathon or similar
      "category": "hackathon"|"ctf",   // "ctf" for capture-the-flag and other cybersecurity / hacking competitions
      "in_scope_philippines": true|false,  // held in the Philippines, or an online event aimed at Philippine participants
      "name": string,
      "organizer": string|null,
      "mode": "onsite"|"online"|"hybrid"|null,
      "venue": string|null,
      "city": string|null,             // Philippine city or municipality where it is held (not where a team comes from)
      "province": string|null,         // Philippine province, or "Metro Manila"
      "start": "YYYY-MM-DDTHH:MM" or "YYYY-MM-DD" or null,   // local Philippine time
      "end": same format or null,
      "registration_deadline": same format or null,
      "eligibility": string|null,
      "themes": [string],
      "prize": string|null,
      "registration_url": string|null, // only a URL that appears in the page text
      "registration_closed": true|false|null,
      "results": string|null           // for events already held: winners in one short factual line
    }}
  ]
}}

Page URL: {url}
Page text:
<<<
{text}
>>>"""


class LLMBudget:
    """Counts AI requests per scan and per UTC day (all providers together) so free tiers are never exceeded,
    and remembers which provider is paused after a quota answer. Stored in data/state/llm_usage.json."""

    def __init__(self, path=None):
        self.path = path or config.LLM_USAGE_FILE
        self.today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, AttributeError):
            saved = {}
        self.day_count = saved.get("count", 0) if saved.get("date") == self.today else 0
        self.blocked = dict(saved.get("blocked") or {})        # provider -> UTC time its quota resets
        if saved.get("blocked_until") and "gemini" not in self.blocked:   # older single-provider format
            self.blocked["gemini"] = saved["blocked_until"]
        self.by_provider = dict(saved.get("by_provider") or {}) if saved.get("date") == self.today else {}
        self.run_count = 0
        self.run_by_provider = {}
        self.cache_hits = 0
        self.stopped = ""          # reason the AI was switched off for the rest of this run
        self.off = {}              # provider -> reason it is skipped for the rest of this run (bad key, no model)
        self.models = {}           # provider -> model used this run
        self.last_error = False    # the last page got a real error or unusable answer (not just quota/budget)
        self._last_call = {}

    # ---- which providers may be asked
    def is_blocked(self, provider) -> bool:
        bu = _parse_utc(self.blocked.get(provider))
        return bool(bu and datetime.now(timezone.utc) < bu)

    def ready(self) -> list[str]:
        return [p for p in config.llm_chain() if p not in self.off and not self.is_blocked(p)]

    def allow(self) -> bool:
        if self.stopped:
            return False
        if self.run_count >= config.LLM_RUN_CAP:
            self.stopped = f"run cap of {config.LLM_RUN_CAP} AI requests reached"
        elif self.day_count >= config.LLM_DAILY_CAP:
            self.stopped = f"daily cap of {config.LLM_DAILY_CAP} AI requests reached"
        elif not self.ready():
            paused = {p: self.blocked[p] for p in config.llm_chain() if self.is_blocked(p)}
            if paused:
                self.stopped = ("AI paused after quota answers (HTTP 429): "
                                + ", ".join(f"{p} until {u}" for p, u in paused.items()))
            else:
                self.stopped = "no AI provider usable this run: " + "; ".join(f"{p}: {r}" for p, r in self.off.items())
        return not self.stopped

    @property
    def model(self):        # first model used this run (older callers and the page)
        return next(iter(self.models.values()), None)

    @property
    def blocked_until(self):
        return self.blocked.get("gemini")

    def pace(self, provider="gemini"):
        """Space requests to one provider out (free tiers also limit requests per minute)."""
        last = self._last_call.get(provider, 0.0)
        wait = config.LLM_MIN_INTERVAL_S - (time.monotonic() - last)
        if wait > 0 and last:
            time.sleep(wait)
        self._last_call[provider] = time.monotonic()

    def used(self, provider="gemini"):
        self.run_count += 1
        self.day_count += 1
        self.run_by_provider[provider] = self.run_by_provider.get(provider, 0) + 1
        self.by_provider[provider] = self.by_provider.get(provider, 0) + 1

    def quota_hit(self, body: str, provider="gemini", retry_after=None):
        """HTTP 429 from one provider: pause only that provider. A daily quota resets around midnight
        Pacific time (07:00-08:00 UTC) for Gemini, 00:00 UTC for most others; otherwise retry in 20 minutes."""
        now = datetime.now(timezone.utc)
        try:
            ra = float(retry_after) if retry_after else 0
        except ValueError:
            ra = 0
        if re.search(r"per ?day|PerDay|daily|tokens per day|requests per day|\bRPD\b|free-models-per-day", body or "", re.I):
            until = now.replace(hour=8 if provider == "gemini" else 0, minute=0, second=0, microsecond=0)
            if until <= now:
                until += timedelta(days=1)
        elif ra > 0:
            until = now + timedelta(seconds=min(max(ra, 60), 86400))
        else:
            until = now + timedelta(minutes=20)
        self.blocked[provider] = until.replace(microsecond=0).isoformat().replace("+00:00", "Z")

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        doc = {"date": self.today, "count": self.day_count}
        if self.by_provider:
            doc["by_provider"] = self.by_provider
        live = {p: u for p, u in self.blocked.items() if self.is_blocked(p)}
        if live:
            doc["blocked"] = live
        self.path.write_text(json.dumps(doc) + "\n", encoding="utf-8")


def _parse_utc(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")) if s else None
    except ValueError:
        return None


BUDGET: LLMBudget | None = None


class Quota(Exception):
    """HTTP 429: this provider is paused; try the next one."""


class Unusable(Exception):
    """Bad key, no credit or no suitable model: skip this provider for the rest of the run."""


def _json_from(raw: str):
    m = re.search(r"\{.*\}", raw or "", re.S)
    return json.loads(m.group(0)) if m else None


def _check(http, r, provider: str, url: str):
    """Shared handling of a provider's HTTP answer."""
    if r.status_code == 429:
        BUDGET.quota_hit(r.text, provider, (getattr(r, "headers", None) or {}).get("retry-after"))
        raise Quota(provider)
    if r.status_code in (401, 402, 403):
        raise Unusable(f"HTTP {r.status_code} (key, permission or credit problem)")
    if r.status_code != 200:
        http.log.append({"url": url, "error": f"{provider} HTTP {r.status_code}: {r.text[:200]}"})
        return False
    return True


def _pick_gemini_model(http) -> str | None:
    """Newest Flash-Lite model that supports generateContent (free-tier friendly), unless one is configured."""
    if config.GEMINI_MODEL:
        return config.GEMINI_MODEL if config.GEMINI_MODEL.startswith("models/") else "models/" + config.GEMINI_MODEL
    r = http.s.get(f"{config.GEMINI_BASE}/models", timeout=30, params={"pageSize": 1000},
                   headers={"x-goog-api-key": config.GEMINI_API_KEY})
    if r.status_code != 200:
        http.log.append({"url": "gemini:models", "error": f"Gemini model list HTTP {r.status_code}: {r.text[:200]}"})
        return None
    names = [m["name"] for m in r.json().get("models", [])
             if "generateContent" in m.get("supportedGenerationMethods", [])]

    def score(name):
        n = name.lower()
        if any(x in n for x in ("tts", "audio", "image", "live", "embedding", "vision", "exp")):
            return None
        pref = 2 if "flash-lite" in n else 1 if "flash" in n else 0
        if not pref:
            return None
        m = re.search(r"gemini-(\d+)(?:\.(\d+))?", n)
        ver = (int(m.group(1)), int(m.group(2) or 0)) if m else (0, 0)
        return (pref, 0 if "preview" in n else 1, ver, -len(n))
    ranked = sorted((s, n) for n in names if (s := score(n)) is not None)
    return ranked[-1][1] if ranked else None


def _call_gemini(http, prompt: str, url: str):
    if "gemini" not in BUDGET.models:
        model = _pick_gemini_model(http)
        if not model:
            raise Unusable("no usable Gemini model found for this key")
        BUDGET.models["gemini"] = model
    BUDGET.pace("gemini")
    r = http.s.post(f"{config.GEMINI_BASE}/{BUDGET.models['gemini']}:generateContent", timeout=90,
                    headers={"x-goog-api-key": config.GEMINI_API_KEY, "content-type": "application/json"},
                    json={"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                          "generationConfig": {"temperature": 0, "responseMimeType": "application/json",
                                               "maxOutputTokens": 2500}})
    BUDGET.used("gemini")
    if not _check(http, r, "gemini", url):
        return None
    cands = r.json().get("candidates") or []
    parts = (cands[0].get("content") or {}).get("parts", []) if cands else []
    return _json_from("".join(p.get("text", "") for p in parts))


def _call_anthropic(http, prompt: str, url: str):
    BUDGET.models.setdefault("anthropic", config.ANTHROPIC_MODEL)
    BUDGET.pace("anthropic")
    r = http.s.post("https://api.anthropic.com/v1/messages", timeout=60, headers={
        "x-api-key": config.ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json",
    }, json={"model": config.ANTHROPIC_MODEL, "max_tokens": 2500, "temperature": 0,
             "messages": [{"role": "user", "content": prompt}]})
    BUDGET.used("anthropic")
    if not _check(http, r, "anthropic", url):
        return None
    return _json_from("".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text"))


# OpenAI-compatible free tiers. Models are picked from each provider's own list, in this order of preference.
OPENAI_COMPAT = {
    "groq": {"base": "https://api.groq.com/openai/v1", "key": "GROQ_API_KEY", "model": "GROQ_MODEL",
             "prefer": ["openai/gpt-oss-120b", "llama-3.3-70b-versatile", "openai/gpt-oss-20b", "llama-3.1-8b-instant"]},
    "cerebras": {"base": "https://api.cerebras.ai/v1", "key": "CEREBRAS_API_KEY", "model": "CEREBRAS_MODEL",
                 "prefer": ["gpt-oss-120b", "llama-3.3-70b", "qwen-3-235b-a22b-instruct-2507", "llama3.1-8b"]},
    "mistral": {"base": "https://api.mistral.ai/v1", "key": "MISTRAL_API_KEY", "model": "MISTRAL_MODEL",
                "prefer": ["mistral-small-latest", "mistral-medium-latest", "open-mistral-nemo"]},
    # GitHub Models: the workflow's own token (permission models: read); no model list call, a fixed small model.
    "github": {"base": "https://models.github.ai/inference", "key": "GITHUB_MODELS_TOKEN", "model": "GITHUB_MODEL",
               "prefer": [], "default": "openai/gpt-4.1-mini"},
    # OpenRouter: only models whose id ends in ":free" are ever used (the account has no credit).
    "openrouter": {"base": "https://openrouter.ai/api/v1", "key": "OPENROUTER_API_KEY", "model": "OPENROUTER_MODEL",
                   "prefer": ["openai/gpt-oss-120b:free", "meta-llama/llama-3.3-70b-instruct:free",
                              "deepseek/deepseek-chat-v3.1:free", "qwen/qwen3-235b-a22b:free",
                              "google/gemma-3-27b-it:free", "mistralai/mistral-small-3.2-24b-instruct:free"],
                   "free_only": True},
}
_SKIP_MODEL = re.compile(r"whisper|guard|tts|embed|moderat|ocr|vision|audio|image|transcri|safeguard|prompt-guard", re.I)


def _pick_openai_model(http, provider: str) -> str | None:
    spec = OPENAI_COMPAT[provider]
    want = getattr(config, spec["model"], "")
    if want:
        if spec.get("free_only") and not want.endswith(":free"):
            raise Unusable(f"configured model {want!r} is not a free model; only ':free' models are allowed")
        return want
    if spec.get("default"):
        return spec["default"]
    r = http.s.get(f"{spec['base']}/models", timeout=30,
                   headers={"authorization": f"Bearer {getattr(config, spec['key'])}"})
    if r.status_code in (401, 403):
        raise Unusable(f"model list HTTP {r.status_code} (check the key)")
    if r.status_code != 200:
        http.log.append({"url": f"{provider}:models", "error": f"{provider} model list HTTP {r.status_code}: {r.text[:200]}"})
        return None
    ids = [m.get("id", "") for m in (r.json().get("data") or []) if isinstance(m, dict)]
    if spec.get("free_only"):
        ids = [i for i in ids if i.endswith(":free")]
    for p in spec["prefer"]:
        if p in ids:
            return p
    rest = sorted(i for i in ids if i and not _SKIP_MODEL.search(i))
    good = [i for i in rest if re.search(r"gpt-oss|llama-3\.3|llama-4|qwen|mistral|gemma|deepseek", i, re.I)]
    return (good or rest or [None])[0]


def _call_openai_compat(http, provider: str, prompt: str, url: str):
    spec = OPENAI_COMPAT[provider]
    if provider not in BUDGET.models:
        model = _pick_openai_model(http, provider)
        if not model:
            raise Unusable("no suitable model in the provider's list")
        BUDGET.models[provider] = model
    model = BUDGET.models[provider]
    if spec.get("free_only") and not model.endswith(":free"):     # belt and braces: never a paid model
        raise Unusable(f"refusing non-free model {model!r}")
    body = {"model": model, "temperature": 0, "max_tokens": 3000,
            "messages": [{"role": "user", "content": prompt}]}
    if "gpt-oss" in model and provider in ("groq", "cerebras"):
        body["reasoning_effort"] = "low"
    headers = {"authorization": f"Bearer {getattr(config, spec['key'])}", "content-type": "application/json"}
    if provider == "openrouter" and config.REPO_URL:
        headers.update({"HTTP-Referer": config.REPO_URL, "X-Title": "PH HackIntel Map"})
    BUDGET.pace(provider)
    r = http.s.post(f"{spec['base']}/chat/completions", timeout=90, headers=headers, json=body)
    BUDGET.used(provider)
    if not _check(http, r, provider, url):
        return None
    choices = r.json().get("choices") or []
    content = ((choices[0].get("message") or {}).get("content") or "") if choices else ""
    return _json_from(content if isinstance(content, str) else "")


def _call(provider: str, http, prompt: str, url: str):
    """Looked up at call time so tests can replace a single provider."""
    if provider == "gemini":
        return _call_gemini(http, prompt, url)
    if provider == "anthropic":
        return _call_anthropic(http, prompt, url)
    return _call_openai_compat(http, provider, prompt, url)


def llm_available() -> bool:
    """True when at least one AI provider is configured, not paused, and the budget is not used up."""
    global BUDGET
    if not config.llm_provider():
        return False
    if BUDGET is None:
        BUDGET = LLMBudget()
    return BUDGET.allow()


def llm_extract_ex(http, text: str, url: str) -> tuple[list[dict], bool]:
    """(events, answered). Tries each provider in the chain until one gives a usable answer; a provider that
    answers 429 is paused and the next one is asked. answered is False when no AI answer was obtained, so the
    caller can try the page again later. BUDGET.last_error tells it whether that was a real failure."""
    if not llm_available():
        return [], False
    BUDGET.last_error = False
    prompt = LLM_PROMPT.format(url=url, text=text, today=datetime.now(PHT).strftime("%Y-%m-%d"))
    for provider in BUDGET.ready():
        if not BUDGET.allow():
            break
        try:
            data = _call(provider, http, prompt, url)
        except Quota:
            continue
        except Unusable as e:
            BUDGET.off[provider] = str(e)
            http.log.append({"url": f"{provider}:setup", "error": f"{provider} skipped this run: {e}"[:300]})
            continue
        except Exception as e:  # network, JSON or schema surprises: log, try the next provider, never crash
            http.log.append({"url": url, "error": f"AI extraction ({provider}) failed: {type(e).__name__}: {e}"[:300]})
            BUDGET.last_error = True
            continue
        if isinstance(data, dict):
            BUDGET.last_error = False
            return from_llm_many(data, url, text), True
        BUDGET.last_error = True
    BUDGET.allow()          # records why nothing is left (all paused, caps) for the scan report
    return [], False


def llm_extract(http, text: str, url: str) -> dict | None:
    """Optional. The first event the AI found on the page, or None. Needs at least one AI key."""
    evs, _ = llm_extract_ex(http, text, url)
    return evs[0] if evs else None


def from_llm_many(data: dict, url: str, text: str) -> list[dict]:
    items = data.get("events") if isinstance(data.get("events"), list) else [data]
    out, seen = [], set()
    for item in items[:5]:
        ev = from_llm(item, url, text) if isinstance(item, dict) else None
        if ev and ev["name"].lower() not in seen:
            seen.add(ev["name"].lower())
            out.append(ev)
    return out


def place_mentioned(value: str, text: str) -> bool:
    """Is a venue / city / province name in the page text? "Iloilo City" matches "Iloilo" and "City of Iloilo"."""
    t = re.sub(r"\s+", " ", norm(text).lower())
    v = re.sub(r"\s+", " ", norm(value).lower()).strip()
    variants = {v, re.sub(r"^city of\s+", "", v), re.sub(r"\s+city$", "", v)}
    if v in ("metro manila", "ncr", "national capital region"):
        variants |= {"metro manila", "ncr", "national capital region", "manila"}
    return any(len(x) >= 3 and re.search(r"(?<![a-z])" + re.escape(x) + r"(?![a-z])", t) for x in variants)


def _date_part(v) -> str | None:
    m = re.match(r"\d{4}-\d{2}-\d{2}", str(v or ""))
    return m.group(0) if m else None


def from_llm(data: dict | None, url: str, text: str) -> dict | None:
    """Check one AI answer against the page. Anything the page does not support is dropped:
    links not in the text, dates not stated in the text, impossible years, a deadline after the event."""
    if not isinstance(data, dict) or not data.get("is_hackathon") or not data.get("in_scope_philippines"):
        return None
    name = norm(data.get("name") if isinstance(data.get("name"), str) else "").strip()
    if not name or len(name) > 160:
        return None
    reg = clean_url(data.get("registration_url") if isinstance(data.get("registration_url"), str) else None, url)
    if reg and reg not in text and reg.rstrip("/") not in text:
        reg = None  # the model must not invent links: keep only URLs present in the page
    checks, this_year = [], datetime.now(PHT).year

    def checked(key):
        raw = data.get(key)
        if not isinstance(raw, str) or not _date_part(raw):
            return None
        if not (this_year - 2 <= int(raw[:4]) <= this_year + 2):
            checks.append(f"{key} {raw} dropped: implausible year")
            return None
        if not date_mentioned(_date_part(raw), text):
            checks.append(f"{key} {raw} dropped: not stated in the page text")
            return None
        return raw

    s_raw, e_raw, d_raw = checked("start"), checked("end"), checked("registration_deadline")
    start, end = parse_dt(s_raw), parse_dt(e_raw)
    deadline = parse_dt(d_raw, end_of_day=True)
    sd, ed, dd = _date_part(start), _date_part(end), _date_part(deadline)
    if sd and ed and ed < sd:
        checks.append("end before start: end dropped")
        end, ed = None, None
    if dd and sd and dd > max(sd, ed or sd):
        checks.append("deadline after the event: deadline dropped")
        deadline = None
    def s(v, n=300):
        return norm(v).strip()[:n] if isinstance(v, str) and v.strip() else None

    def place(key, n):
        v = s(data.get(key), n)
        if v and not place_mentioned(v, text):
            checks.append(f"{key} {v!r} dropped: not in the page text")
            return None
        return v
    mode = data.get("mode") if data.get("mode") in ("onsite", "online", "hybrid") else "unspecified"
    return {
        "name": name, "organizer": s(data.get("organizer"), 200), "mode": mode,
        "venue": place("venue", 200), "city": place("city", 80), "province": place("province", 80),
        "country": "Philippines", "lat": None, "lon": None,
        "start": start, "end": end,
        "deadline": deadline or start, "deadline_is_estimate": deadline is None,
        "eligibility": s(data.get("eligibility")), "themes": [norm(t)[:60] for t in (data.get("themes") if isinstance(data.get("themes"), list) else []) if isinstance(t, str)][:6],
        "prize": s(data.get("prize")),
        "registration_url": reg or url, "official_url": url,
        "registration_closed": data.get("registration_closed") if isinstance(data.get("registration_closed"), bool) else None,
        "category": data.get("category") if data.get("category") in ("hackathon", "ctf") else None,
        "results": s(data.get("results"), 400),
        "ai_checks": checks or None,
        "extraction": "llm",
    }
