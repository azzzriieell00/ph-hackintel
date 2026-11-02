"""Turn fetched pages into candidate event records.

Two extraction paths:
  * schema.org Event JSON-LD embedded in the page (exact, preferred)
  * optional LLM extraction of the visible text (only if ANTHROPIC_API_KEY is set)
Nothing is guessed: fields the page does not state stay empty.
"""
from __future__ import annotations

import json
import re
import urllib.parse
from datetime import datetime, timedelta, timezone

from bs4 import BeautifulSoup
from dateutil import parser as dtparser

from . import config

PHT = timezone(timedelta(hours=8))
KW_RE = re.compile(r"\b(" + "|".join(re.escape(k) for k in config.KEYWORDS) + r")\b", re.I)
PH_RE = re.compile(r"\b(philippines|pilipinas|filipino|manila|cebu|davao|iloilo|luzon|visayas|mindanao)\b", re.I)


CTF_RE = re.compile(config.CTF_PATTERN, re.I)


def is_relevant(*texts: str) -> bool:
    return any(KW_RE.search(t or "") for t in texts)


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
    if not u:
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
    text = re.sub(r"\n{3,}", "\n\n", soup.get_text("\n", strip=True))
    return text[:limit]


def page_title(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    return (soup.title.string or "").strip() if soup.title and soup.title.string else ""


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


def _from_jsonld(n: dict, page_url: str) -> dict | None:
    name = (n.get("name") or "").strip()
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
        venue = venue or loc.get("name")
        addr = loc.get("address")
        if isinstance(addr, dict):
            city = city or addr.get("addressLocality")
            province = province or addr.get("addressRegion")
            country = country or _name(addr.get("addressCountry"))
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
    return {
        "name": name,
        "organizer": _name(n.get("organizer")),
        "description": (n.get("description") or "")[:600],
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
                    offsite: list | None = None) -> list[str]:
    """Links on a listing page that look like hackathon announcements.
    Matching links to other hosts are not returned; when `offsite` is a list they are appended
    to it as (url, link text) so the caller can record them as leads."""
    soup = BeautifulSoup(html, "html.parser")
    rx = re.compile(pattern, re.I) if pattern else None
    seen, out = set(), []
    base_host = urllib.parse.urlsplit(base_url).netloc.lower()
    hosts = {h.lower() for h in (allow_hosts or [base_host])}
    for a in soup.find_all("a", href=True):
        u = clean_url(a["href"], base_url)
        if not u or u in seen:
            continue
        host = urllib.parse.urlsplit(u).netloc.lower()
        text = a.get_text(" ", strip=True)
        if rx and not (rx.search(u) or rx.search(text)):
            continue
        if not rx and not is_relevant(text, u.replace("-", " ")):
            continue
        seen.add(u)
        if host not in hosts:
            if offsite is not None:
                offsite.append((u, text))
            continue
        out.append(u)
    return out


# ------------------------------------------------------------------ LLM
LLM_PROMPT = """You extract hackathon and CTF announcements from web pages for a public map of Philippine competitions.
Read the page text and answer with ONLY a JSON object, no prose. Use null for anything the page does not state.
Never guess dates, venues or links.

{{
  "is_hackathon": true|false,          // hackathon, CTF (capture the flag), cybersecurity competition, buildathon, ideathon or similar
  "category": "hackathon"|"ctf",       // "ctf" for capture-the-flag and other cybersecurity / hacking competitions
  "in_scope_philippines": true|false,  // held in the Philippines, or an online event aimed at Philippine participants
  "name": string,
  "organizer": string|null,
  "mode": "onsite"|"online"|"hybrid"|null,
  "venue": string|null,
  "city": string|null,                 // Philippine city or municipality
  "province": string|null,             // Philippine province, or "Metro Manila"
  "start": "YYYY-MM-DDTHH:MM" or "YYYY-MM-DD" or null,   // local Philippine time
  "end": same format or null,
  "registration_deadline": same format or null,
  "eligibility": string|null,
  "themes": [string],
  "prize": string|null,
  "registration_url": string|null,     // only a URL that appears in the page text
  "registration_closed": true|false|null
}}

Page URL: {url}
Page text:
<<<
{text}
>>>"""


def llm_extract(http, text: str, url: str) -> dict | None:
    """Optional. Returns a candidate or None. Requires ANTHROPIC_API_KEY."""
    if not config.ANTHROPIC_API_KEY:
        return None
    try:
        r = http.s.post("https://api.anthropic.com/v1/messages", timeout=60, headers={
            "x-api-key": config.ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json",
        }, json={
            "model": config.ANTHROPIC_MODEL, "max_tokens": 900, "temperature": 0,
            "messages": [{"role": "user", "content": LLM_PROMPT.format(url=url, text=text)}],
        })
        if r.status_code != 200:
            http.log.append({"url": url, "error": f"LLM HTTP {r.status_code}: {r.text[:200]}"})
            return None
        raw = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
        m = re.search(r"\{.*\}", raw, re.S)
        data = json.loads(m.group(0)) if m else None
    except Exception as e:  # network, JSON or schema surprises: log and skip, never crash the run
        http.log.append({"url": url, "error": f"LLM extraction failed: {type(e).__name__}: {e}"[:300]})
        return None
    return from_llm(data, url, text)


def from_llm(data: dict | None, url: str, text: str) -> dict | None:
    if not isinstance(data, dict) or not data.get("is_hackathon") or not data.get("in_scope_philippines"):
        return None
    name = (data.get("name") or "").strip()
    if not name:
        return None
    reg = clean_url(data.get("registration_url"), url)
    if reg and reg not in text and reg.rstrip("/") not in text:
        reg = None  # the model must not invent links: keep only URLs present in the page
    deadline = parse_dt(data.get("registration_deadline"), end_of_day=True)
    start = parse_dt(data.get("start"))
    return {
        "name": name, "organizer": data.get("organizer"), "mode": data.get("mode") or "onsite",
        "venue": data.get("venue"), "city": data.get("city"), "province": data.get("province"),
        "country": "Philippines", "lat": None, "lon": None,
        "start": start, "end": parse_dt(data.get("end")),
        "deadline": deadline or start, "deadline_is_estimate": deadline is None,
        "eligibility": data.get("eligibility"), "themes": [t for t in (data.get("themes") or []) if isinstance(t, str)][:6],
        "prize": data.get("prize"),
        "registration_url": reg or url, "official_url": url,
        "registration_closed": data.get("registration_closed"),
        "category": data.get("category") if data.get("category") in ("hackathon", "ctf") else None,
        "extraction": "llm",
    }
