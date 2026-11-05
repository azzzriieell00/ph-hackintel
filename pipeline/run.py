"""Run one discovery pass and write data/events.json and data/review.json.

    python -m pipeline.run                 # normal run
    python -m pipeline.run --offline       # no network: re-evaluate existing data and manual entries only
    python -m pipeline.run --only SOURCE   # run a single source id (debugging)

How a scan works
  1. Gather: every enabled source contributes links (feeds, listing pages, site homepages,
     daily site searches). Links are ranked by how promising they look (extract.link_score).
  2. Read: the best links are opened first, within per-source, per-run and time limits. Structured
     event data is read directly; other pages go to the AI (cached by page content) within its budget.
     Links that could not be read this time wait in a queue for the next scan.
  3. Check: duplicates are merged (same page, same name, or the same competition under another name),
     each event is located, its official link is checked, the publication rules are applied and a
     confidence score is computed.
"""
from __future__ import annotations

import argparse
import copy
import sys
import time
from collections import Counter
from datetime import timedelta
from urllib.parse import urlencode, urljoin, urlsplit

import yaml

from . import config, extract, geo
from .fetch import Http
from .store import (add_sources, confidence, is_finished, is_twin, iso, load_json, make_id, merge, now_utc, parse_iso,
                    phase, publish_decision, source_hosts, write_json)

PH_NAMES = {"ph", "phl", "philippines", "republic of the philippines", "pilipinas"}
BLOCKED: set[str] = set()


def host_of(url: str) -> str:
    return urlsplit(url).netloc.lower()


def is_blocked(url: str) -> bool:
    h = host_of(url)
    return h in BLOCKED or any(h.endswith("." + b) for b in BLOCKED)


def add_lead(report: dict, url: str, title: str = "", source_name: str = "", published: str | None = None, why: str = ""):
    report.setdefault("leads", []).append({"title": (title or "").strip(), "url": url, "source": source_name,
                                           "published": published, "note": why})


def load_yaml(path):
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {}


def older_than(stamp: str | None, now, hours: float) -> bool:
    d = parse_iso(stamp)
    return d is None or now - d >= timedelta(hours=hours)


def new_report(src: dict) -> dict:
    return {"id": src["id"], "name": src.get("name", src["id"]), "type": src["type"], "url": src.get("url"),
            "errors": [], "needs_llm": [], "notes": [], "found": 0, "pages": 0, "ai_read": 0, "ai_cache_hits": 0,
            "deferred": 0}


def fail(report: dict, http, url: str, r) -> None:
    report["errors"].append(f"{url}: {http.why(url) if r is None else 'HTTP %d' % r.status_code}")


# ------------------------------------------------------------------ work queue
class Queue:
    """Links to open this run, best first. Event pages named in sources.yaml are always read."""

    def __init__(self):
        self.items: dict[str, dict] = {}

    def add(self, url: str, src: dict, score: int, trusted: bool = False, force: bool = False, title: str = "",
            needs_ai: bool = False):
        cur = self.items.get(url)
        if cur and cur["score"] >= score and (cur["force"] or not force):
            return
        self.items[url] = {"url": url, "src": src, "score": score, "title": title or (cur or {}).get("title", ""),
                           "trusted": trusted or bool(cur and cur["trusted"]), "force": force or bool(cur and cur["force"]),
                           "needs_ai": needs_ai or bool(cur and cur.get("needs_ai"))}

    def ordered(self) -> list[dict]:
        return sorted(self.items.values(), key=lambda w: (not w["force"], -w["score"]))


def queue_feed_items(items: list[dict], src: dict, report: dict, queue: Queue, now, bonus: int = 0,
                     keyword_required: bool = True, open_links: bool = True) -> None:
    """Feed entries -> signals (headline mentions a competition) and links to read."""
    for it in items:
        pub = it["published"]
        if pub and now - pub > timedelta(days=config.FEED_MAX_AGE_D):
            continue
        relevant = extract.is_relevant(it["title"], it["summary"])
        if keyword_required and not relevant:
            continue
        if relevant:
            # Every matching headline is kept as a signal, whether or not an event can be extracted from it.
            add_lead(report, it["link"], it["title"], it.get("source") or src.get("name", ""), iso(pub) if pub else None,
                     "headline only, not verified")
        if open_links:
            queue.add(it["link"], src, extract.link_score(it["title"], it["link"], pub, now) + bonus, title=it["title"])


def gather(http, src: dict, page_state: dict, report: dict, queue: Queue, now, slots: dict) -> list[dict]:
    """Adds links to the queue. Sources with structured data (ctftime) return their events directly."""
    t = src["type"]
    if t == "ctftime":
        return gather_ctftime(http, src, page_state, report, now)
    if t == "event_page":
        queue.add(src["url"], src, 1000, trusted=True, force=True)
    elif t == "listing":
        r = http.get(src["url"])
        if r is None or r.status_code >= 400:
            return fail(report, http, src["url"], r)
        queue_links(r, src, report, queue, now)
    elif t == "rss":
        r = http.get(src["url"])
        if r is None or r.status_code >= 400:
            return fail(report, http, src["url"], r)
        queue_feed_items(extract.feed_items(r.content, r.url), src, report, queue, now,
                         keyword_required=src.get("require_keyword", True), open_links=not src.get("leads_only"))
    elif t == "site":
        gather_site(http, src, page_state, report, queue, now, slots)
    else:
        report["errors"].append(f"unknown source type {t!r}")
    return []


def ctftime_event(e: dict) -> dict | None:
    """One CTFtime API event -> candidate, only for onsite CTFs whose location names the Philippines."""
    loc = extract.norm(e.get("location") if isinstance(e.get("location"), str) else "").strip()
    parts = [p.strip() for p in loc.split(",") if p.strip()]
    if not e.get("onsite") or not parts or not any(p.lower() in PH_NAMES for p in parts):
        return None
    place = [p for p in parts if p.lower() not in PH_NAMES]
    city = province = None
    hit = next((p for p in reversed(place) if geo.gazetteer_match(p, "city")), None)
    if hit:
        city = hit
    elif len(place) == 1:
        city = place[0]
    elif len(place) >= 2:
        city, province = place[-2], place[-1]
    ctftime_url = extract.clean_url(e.get("ctftime_url"))
    url = extract.clean_url(e.get("url")) or ctftime_url
    start = extract.parse_dt(e.get("start"))
    title = extract.norm(e.get("title") if isinstance(e.get("title"), str) else "").strip()
    if not title or not start:
        return None
    return {
        "name": title, "category": "ctf",
        "organizer": ", ".join(str(o["name"]) for o in (e.get("organizers") if isinstance(e.get("organizers"), list) else [])
                               if isinstance(o, dict) and o.get("name")) or None,
        "mode": "onsite", "venue": ", ".join(place) or None, "city": city, "province": province, "country": "Philippines",
        "lat": None, "lon": None, "start": start, "end": extract.parse_dt(e.get("finish")),
        "deadline": start, "deadline_is_estimate": True,
        "eligibility": e.get("restrictions") or None,
        "themes": [t for t in ["Capture the Flag", e.get("format")] if t],
        "registration_url": url, "official_url": url,
        "sources": [{"label": "CTFtime", "url": ctftime_url}] if ctftime_url else [],
        "extraction": "api",
    }


def gather_ctftime(http, src: dict, page_state: dict, report: dict, now) -> list[dict]:
    """CTFtime's public API (data export for analysis; see docs/SOURCES.md). Read every few hours in
    31-day windows from two months back to four months ahead; robots.txt asks for 10 s between requests."""
    st = page_state.setdefault("_sites", {}).setdefault(src["id"], {})
    if not older_than(st.get("fetched_at"), now, src.get("refresh_hours", 12)):
        report["notes"].append("not due yet; events from the last read are carried over")
        return []
    out, seen = [], set()
    t = int((now - timedelta(days=config.PAST_WINDOW_D)).timestamp())
    end = int((now + timedelta(days=120)).timestamp())
    while t < end:
        params = {"limit": 100, "start": t, "finish": min(t + 31 * 86400, end)}
        r = http.get(src["url"], params=params)
        if r is None or r.status_code >= 400:
            return fail(report, http, src["url"], r) or []
        try:
            data = r.json()
        except ValueError:
            report["errors"].append(f"{src['url']}: answer was not JSON")
            return []
        for e in data if isinstance(data, list) else []:
            if not isinstance(e, dict) or e.get("id") in seen:
                continue
            seen.add(e.get("id"))
            try:
                ev = ctftime_event(e)
            except (TypeError, ValueError, AttributeError) as x:
                report["notes"].append(f"CTFtime event {e.get('id')} skipped: {type(x).__name__}")
                continue
            if ev:
                ev["source_id"] = src["id"]
                out.append(ev)
        t += 31 * 86400
    st["fetched_at"] = iso(now)
    report["notes"].append(f"{len(seen)} CTFs listed in the window, {len(out)} onsite in the Philippines")
    return out


def queue_links(r, src: dict, report: dict, queue: Queue, now) -> None:
    offsite: list = []
    links = extract.candidate_links(r.text, r.url, src.get("allow_hosts"), src.get("link_pattern"), offsite, with_text=True)
    for u, text in offsite:
        if is_blocked(u):   # e.g. a registration page on Luma or Facebook: a person has to check it
            add_lead(report, u, text, src.get("name", ""), None, "linked from source; platform does not allow automated access")
    for u, text in links:
        queue.add(u, src, extract.link_score(text, u, None, now), title=text)


def gather_site(http, src: dict, page_state: dict, report: dict, queue: Queue, now, slots: dict) -> None:
    """A whole website (school, organizer, news outlet): its feeds every scan; its homepage links and
    its own search (WordPress sites) once a day. Feeds are found automatically from the homepage."""
    st = page_state.setdefault("_sites", {}).setdefault(src["id"], {})
    home = src["url"]
    first = not st.get("home_at")
    if first or (older_than(st.get("home_at"), now, 24) and slots["home"] > 0):
        if not first:
            slots["home"] -= 1
        st["home_at"] = iso(now)
        r = http.get(home)
        if r is None or r.status_code >= 400:
            fail(report, http, home, r)
            st["home_error"] = report["errors"][-1][-200:]
        else:
            st.pop("home_error", None)
            if not src.get("feeds"):
                feeds = extract.discover_feeds(r.text, r.url)
                st["wordpress"] = extract.is_wordpress(r.text)
                if not feeds and st["wordpress"]:
                    feeds = [urljoin(r.url if r.url.endswith("/") else r.url + "/", "feed/")]
                st["feeds"] = feeds[:2]
            queue_links(r, src, report, queue, now)
    feeds = list(src.get("feeds") or st.get("feeds") or [])
    report["feeds"] = feeds
    if not feeds and not st.get("home_error"):
        report["notes"].append("no feed found; reading homepage links once a day")
    latest: set[str] = set()       # links in the site's own feed, to recognise a search that is ignored
    for f in feeds[:2]:
        r = http.get(f)
        if r is None or r.status_code >= 400:
            fail(report, http, f, r)
            continue
        items = extract.feed_items(r.content, r.url)
        if not items:
            report["notes"].append(f"{f}: no feed entries")
        latest |= {it["link"] for it in items}
        queue_feed_items(items, src, report, queue, now)
    if st.get("wordpress") and src.get("search", True) and older_than(st.get("search_at"), now, 24) and slots["search"] > 0:
        slots["search"] -= 1
        st["search_at"] = iso(now)
        base = home if home.endswith("/") else home + "/"
        results = []
        for term in config.SEARCH_TERMS:
            url = base + "?" + urlencode({"s": term, "feed": "rss2", "orderby": "date", "order": "DESC"})
            r = http.get(url)
            if r is None or r.status_code >= 400:
                why = http.why(url) if r is None else f"HTTP {r.status_code}"
                report["notes"].append(f"site search skipped: {why}")
                break           # robots.txt or the server said no: do not try the other terms
            results.append(extract.feed_items(r.content, r.url))
        for i, items in enumerate(results):
            # A working search matched the term somewhere in each post, so a headline without it still counts.
            # Some sites ignore the search and send their latest posts instead: then only matching headlines count.
            links = [it["link"] for it in items]
            relevant = sum(1 for it in items if extract.is_relevant(it["title"], it["summary"], it["link"].replace("-", " ")))
            ignored = bool(links) and (
                (len(items) >= 5 and relevant * 2 < len(items))
                or (latest and set(links) <= latest and relevant < len(items))
                or any(links == [it["link"] for it in other] for j, other in enumerate(results) if j != i))
            if ignored and "site search seems to ignore the query; only matching headlines kept" not in report["notes"]:
                report["notes"].append("site search seems to ignore the query; only matching headlines kept")
            queue_feed_items(items, src, report, queue, now, bonus=10, keyword_required=ignored)


# ------------------------------------------------------------------ reading pages
def ai_read(http, url: str, text: str, page_state: dict, report: dict, now) -> tuple[list[dict], bool]:
    """(events, deferred). Results are cached by page content: an unchanged page is never sent twice.
    report["_ai_failed"] is set when the AI was asked but gave no usable answer (not a budget/quota stop)."""
    cache = page_state.setdefault("_ai", {})
    h = extract.text_hash(text)
    c = cache.get(url)
    if c and (c.get("hash") == h or not older_than(c.get("at"), now, config.AI_REFRESH_H)):
        report["ai_cache_hits"] += 1
        return copy.deepcopy(c.get("events") or []), False
    if not config.llm_provider():
        report["needs_llm"].append(url)
        return [], False
    before = extract.BUDGET.run_count if extract.BUDGET else 0
    evs, answered = extract.llm_extract_ex(http, text, url)
    if not answered:           # budget used up, quota, or an error: try this page again in a later scan
        report["deferred"] += 1
        asked = bool(extract.BUDGET and extract.BUDGET.run_count > before)
        report["_ai_failed"] = asked and extract.BUDGET.last_error   # a real error, not a quota pause or cap
        if report["_ai_failed"]:
            fails = page_state.setdefault("_ai_fail", {})
            n = fails.get(url, {}).get("n", 0) + 1
            fails[url] = {"n": n, "at": iso(now)}
            if n >= 3:   # no usable answer three times: remember "nothing here" until the page changes
                cache[url] = {"hash": h, "at": iso(now), "events": [], "failed": True}
                fails.pop(url, None)
                return [], False
        return [], True
    report["ai_read"] += 1
    cache[url] = {"hash": h, "at": iso(now), "events": evs}
    return copy.deepcopy(evs), False


def process_page(http, url: str, source: dict, report: dict, trusted: bool, page_state: dict, now) -> tuple[list[dict], bool]:
    """Fetch one announcement page. Returns (candidate events, deferred)."""
    if is_blocked(url):
        add_lead(report, url, source_name=source.get("name", ""), why="platform does not allow automated access: check by hand")
        return [], False
    r = http.get(url)
    report["_status"] = r.status_code if r is not None else None
    if r is None or r.status_code >= 400:
        fail(report, http, url, r)
        return [], False
    html = r.text
    found = extract.jsonld_events(html, r.url)
    if not trusted:
        found = [e for e in found if extract.is_relevant(e["name"], e.get("description", ""))]
    deferred = False
    if not found:
        text = extract.page_text(html)
        if trusted or extract.is_relevant(extract.page_title(html), text[:3000]):
            pub = None if trusted else extract.page_published(html)
            if pub and now - pub > timedelta(days=config.FEED_MAX_AGE_D):
                # An old article (e.g. from a tag page): its event is long over. Do not spend the AI on it.
                report["old_pages_skipped"] = report.get("old_pages_skipped", 0) + 1
                return [], False
            found, deferred = ai_read(http, r.url, text, page_state, report, now)
    for e in found:
        e["source_id"] = source["id"]
        e["sources"] = [{"label": source.get("name", source["id"]), "url": r.url}]
    return found, deferred


def ai_stopped() -> bool:
    return bool(config.llm_provider()) and not extract.llm_available()


def keep(pending: dict, w: dict, now, attempts: int | None = None) -> None:
    old = pending.get(w["url"], {})
    pending[w["url"]] = {"source": w["src"]["id"], "score": w["score"], "title": (w["title"] or "")[:200],
                         "needs_ai": bool(w.get("needs_ai") or old.get("needs_ai")),
                         "ai_attempts": old.get("ai_attempts", 0) if attempts is None else attempts,
                         "added": old.get("added") or iso(now)}


def read_queue(http, queue: Queue, reports: dict, page_state: dict, now, deadline: float) -> tuple[list[dict], dict]:
    pending = page_state.setdefault("_queue", {})
    out, per_src, pages, capped = [], Counter(), 0, set()
    refused = Counter()             # hosts that answered 401/403/429 this scan
    for w in queue.ordered():
        url, src = w["url"], w["src"]
        rep = reports[src["id"]]
        host = host_of(url)
        if refused[host] >= 2 and not w["force"]:
            if f"refused:{host}" not in capped:
                rep["notes"].append(f"{host} refused 2 pages; its other pages are skipped this scan")
                capped.add(f"refused:{host}")
            continue
        if not w["force"]:
            last = parse_iso(page_state.get(url, {}).get("checked"))
            if last and now - last < timedelta(hours=src.get("recheck_hours", 24)):
                pending.pop(url, None)
                continue
            limit = None
            if per_src[src["id"]] >= config.MAX_FOLLOW_PER_SOURCE:
                limit = f"stopped after {per_src[src['id']]} new links; the rest wait for the next run"
            elif pages >= config.MAX_PAGES_PER_RUN:
                limit = f"scan page limit ({config.MAX_PAGES_PER_RUN}) reached; links wait for the next run"
            elif time.monotonic() > deadline:
                limit = "scan time budget reached; links wait for the next run"
            elif w.get("needs_ai") and ai_stopped():
                limit = "AI budget used up for this scan; pages wait for the next run"
            if limit:
                if src["id"] not in capped:
                    rep["notes"].append(limit)
                    capped.add(src["id"])
                keep(pending, w, now)
                continue
            per_src[src["id"]] += 1
            pages += 1
        rep["pages"] += 1
        rep["_ai_failed"] = False
        try:
            found, deferred = process_page(http, url, src, rep, w["trusted"], page_state, now)
        except Exception as e:   # one odd page must never stop the others
            rep["errors"].append(f"{url}: crash: {type(e).__name__}: {e}"[:300])
            found, deferred = [], False
        if rep.pop("_status", None) in (401, 403, 429):
            refused[host] += 1
        attempts = pending.get(url, {}).get("ai_attempts", 0) + (1 if rep.pop("_ai_failed", False) else 0)
        if deferred and attempts >= 3:
            rep["notes"].append(f"AI gave no usable answer 3 times; page set aside: {url}"[:300])
            deferred = False
        if deferred and not w["force"]:
            w["needs_ai"] = True
            keep(pending, w, now, attempts)
        else:
            if not w["force"]:
                page_state[url] = {"checked": iso(now), "found": len(found)}
            pending.pop(url, None)
        rep["found"] += len(found)
        out.extend(found)
    return out, {"pages_opened": pages}


def prune_state(page_state: dict, now) -> None:
    for k in [k for k, v in page_state.items()
              if not k.startswith("_") and older_than((v or {}).get("checked") if isinstance(v, dict) else None, now, 24 * 60)]:
        del page_state[k]
    for key in ("_ai", "_ai_fail"):
        d = page_state.get(key, {})
        for k in [k for k, v in d.items() if older_than(v.get("at"), now, 24 * 30)]:
            del d[k]
    q = page_state.get("_queue", {})
    for k in [k for k, v in q.items() if older_than(v.get("added"), now, 24 * config.QUEUE_MAX_AGE_D)]:
        del q[k]
    if len(q) > 300:      # keep the most promising
        for k, _ in sorted(q.items(), key=lambda kv: kv[1].get("score", 0))[:len(q) - 300]:
            del q[k]


# ------------------------------------------------------------------ manual entries
def manual_events() -> list[dict]:
    doc = load_yaml(config.MANUAL_FILE)
    out = []
    for e in doc.get("events", []) or []:
        e = dict(e)
        e.setdefault("extraction", "manual")
        e.setdefault("source_id", "manual")
        for k in ("start", "end", "deadline", "verified_manually"):
            if e.get(k) is not None:
                # a stated deadline given as a bare date means 23:59; an estimated one is just the start date
                e[k] = extract.parse_dt(str(e[k]), end_of_day=(k == "deadline" and not e.get("deadline_is_estimate")))
        e.setdefault("deadline_is_estimate", False)
        out.append(e)
    return out


def in_scope(ev: dict) -> tuple[bool, str]:
    c = (ev.get("country") or "").strip().lower()
    if c and c not in PH_NAMES:
        return False, f"country is {ev.get('country')}"
    return True, "ok"


# ------------------------------------------------------------------ main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--only")
    args = ap.parse_args(argv)

    now = now_utc()
    t0 = time.monotonic()
    deadline = t0 + config.SCAN_TIME_BUDGET_S
    extract.BUDGET = None   # fresh AI request budget for this run (day count is read from data/state)
    http = Http(user_agent=config.USER_AGENT, delay=config.PER_HOST_DELAY, timeout=config.HTTP_TIMEOUT)
    geocoder = geo.Geocoder(http, cache_path=config.GEOCODE_CACHE, enabled=config.NOMINATIM_ENABLED and not args.offline)
    page_state = load_json(config.PAGE_STATE, {})
    prev_doc = load_json(config.EVENTS_FILE, {"events": []})
    prev_all = {e["id"]: e for e in prev_doc.get("events", []) + prev_doc.get("held", [])}

    cfg = load_yaml(config.SOURCES_FILE)
    BLOCKED.clear()
    BLOCKED.update(h.lower() for h in cfg.get("blocked_hosts", []) or [])
    http.blocked = BLOCKED          # also checked on every redirect hop
    sources = [s for s in cfg.get("sources", []) or [] if s.get("enabled", True)]
    if args.only:
        sources = [s for s in sources if s["id"] == args.only]
    # Least recently scanned first, so a source cut off by the time budget goes first next time.
    runs = page_state.setdefault("_source_runs", {})
    sources.sort(key=lambda s: runs.get(s["id"], ""))

    candidates: list[dict] = manual_events()
    reports: dict[str, dict] = {}
    scan = {"sources_total": len(sources), "sources_scanned": 0, "sources_skipped": 0, "pages_opened": 0,
            "queued_from_earlier": 0, "links_considered": 0}
    if not args.offline:
        queue, slots = Queue(), {"home": config.DAILY_REFRESH_PER_RUN, "search": config.DAILY_REFRESH_PER_RUN}
        by_src = {s["id"]: s for s in sources}
        for src in sources:
            rep = reports[src["id"]] = new_report(src)
            if time.monotonic() > deadline:
                rep["notes"].append("skipped: scan time budget reached (runs first next time)")
                rep["skipped"] = True
                scan["sources_skipped"] += 1
                continue
            try:
                direct = gather(http, src, page_state, rep, queue, now, slots) or []
                rep["found"] += len(direct)
                candidates.extend(direct)
                runs[src["id"]] = iso(now)
                scan["sources_scanned"] += 1
            except Exception as e:  # one broken source must not stop the run
                rep["errors"].append(f"crash: {type(e).__name__}: {e}"[:300])
        # links that could not be read in earlier scans
        for url, p in list(page_state.get("_queue", {}).items()):
            if p.get("source") not in by_src:
                continue
            if url in queue.items:      # found again: keep what we learned (it needs the AI)
                queue.items[url]["needs_ai"] = queue.items[url]["needs_ai"] or bool(p.get("needs_ai"))
            else:
                queue.add(url, by_src[p["source"]], p.get("score", 0), title=p.get("title", ""), needs_ai=p.get("needs_ai", False))
                scan["queued_from_earlier"] += 1
        scan["links_considered"] = len(queue.items)
        try:
            found, stats = read_queue(http, queue, reports, page_state, now, deadline)
        except Exception as e:  # never lose the manual entries because of a reading bug
            found, stats = [], {"pages_opened": 0}
            print(f"[error] reading pages failed: {type(e).__name__}: {e}", file=sys.stderr)
        scan.update(stats)
        candidates.extend(found)
        for rep in reports.values():
            rep["ok"] = not rep["errors"]
            print(f"[source] {rep['id']}: {rep['found']} candidate(s), {rep['pages']} page(s), "
                  f"{len(rep['errors'])} error(s)", file=sys.stderr)

    review, by_id = [], {}
    for c in candidates:            # manual entries come first, so they win over scanner copies
        ok, why = in_scope(c)
        if not ok:
            review.append({"name": c.get("name"), "url": (c.get("sources") or [{}])[0].get("url"), "reason": why})
            continue
        c["id"] = make_id(c)
        c["category"] = extract.classify(c)
        twin = next((e for e in by_id.values() if is_twin(e, c)), None)
        if twin:                    # same event from two sources: keep the first record, add the source links
            add_sources(twin, c)
            continue
        if c.get("extraction") != "manual":
            # Seen in an earlier scan under another id (another source or name)? Keep that identity and history.
            prev = next((p for pid, p in prev_all.items()
                         if pid not in by_id and p.get("extraction") != "manual" and is_twin(p, c)), None)
            if prev:
                c["id"] = prev["id"]
                add_sources(c, prev)
        by_id[c["id"]] = c

    # Events found earlier but not seen this run are carried forward until they expire,
    # unless this run found the same event under another id (then only its sources are kept).
    manual_ids = {e.get("id") for e in candidates if e.get("extraction") == "manual"}
    for pid, p in prev_all.items():
        if p.get("extraction") == "manual" and pid not in manual_ids:
            continue        # removed from manual_events.yaml: drop it everywhere, including Signals
        if pid in by_id:
            continue
        twin = next((e for e in by_id.values() if is_twin(e, p)), None)
        if twin:
            add_sources(twin, p)
            continue
        p = dict(p)
        p["carried_over"] = True
        by_id[pid] = p

    published, held, no_pin_leads = [], [], []
    for eid, ev in by_id.items():
        ev = merge(prev_all.get(eid), ev, now) if not ev.get("carried_over") else ev
        if ev.get("lat") is None or ev.get("location_source") is None or ev.get("location_check") in (None, "UNRESOLVED", "NO_VENUE_CONFIRMED"):
            geo.resolve(ev, geocoder)
        url = ev.get("registration_url") or ev.get("official_url")
        if url and is_blocked(url):
            ev["link_status"] = "not checked automatically: platform does not allow it"
        elif url and not args.offline and not is_finished(ev, now):
            ok, status = http.check_link(url)
            ev["link_status"] = status
            if ok is True:
                ev["last_verified"] = iso(now)
                ev["link_failures"] = 0
            elif ok is False:
                ev["link_failures"] = (ev.get("link_failures") or 0) + 1
            # ok is None: inconclusive (e.g. the scanner could not verify the site's TLS chain); counts neither way
        fs = parse_iso(ev.get("first_seen"))
        ev["is_new"] = bool(fs and now - fs <= timedelta(hours=config.NEW_WINDOW_H)) and ev.get("extraction") != "manual"
        ev["category"] = extract.classify(ev)
        ev["phase"] = phase(ev, now)
        ev["corroboration"] = len(source_hosts(ev))
        ok, why = publish_decision(ev, now)
        ev["confidence"] = confidence(ev, now)
        ev.pop("description", None)
        if ok:
            ev.pop("hold_reason", None)
            published.append(ev)
        else:
            ev["hold_reason"] = why
            held.append(ev)
            review.append({"name": ev.get("name"), "id": eid, "url": url, "reason": why})
            # Real competitions without a pin (online, venue not confirmed) still show up under Signals,
            # and so do AI finds whose dates could not be confirmed in the page text.
            # (not when the page reports results, or the AI's dates were years off: that event is over)
            ai_undated = (ev.get("extraction") == "llm" and why == "no deadline or event date" and not ev.get("results")
                          and not any("implausible year" in c for c in ev.get("ai_checks") or []))
            if url and not is_finished(ev, now) and ("kept as a signal" in why or ai_undated):
                note = "AI found a competition but no date it could confirm" if ai_undated else why.split(" (kept")[0]
                no_pin_leads.append({"title": f"[{ev['category'].upper()}] {ev['name']}", "url": url,
                                     "source": (ev.get("sources") or [{}])[0].get("label", ""),
                                     "published": None, "note": note, "from_event": True})

    # upcoming first (soonest deadline), then finished events (most recent first)
    upcoming = sorted((e for e in published if e["phase"] != "finished"), key=lambda e: e.get("deadline") or e.get("start") or "")
    past = sorted((e for e in published if e["phase"] == "finished"), key=lambda e: e.get("end") or e.get("start") or "", reverse=True)
    published = upcoming + past

    # Leads: unverified headlines and links on platforms we must not fetch. Kept 21 days.
    # Signals made from held events are rebuilt every scan (so a fixed or expired one disappears at once);
    # headline signals are kept for 21 days.
    leads = {l["url"]: l for l in prev_doc.get("leads", []) if l.get("url") and not l.get("from_event")
             and not str(l.get("title", "")).startswith(("[HACKATHON] ", "[CTF] "))}
    prev_first = {l.get("url"): l.get("first_seen") for l in prev_doc.get("leads", []) if l.get("url")}
    for l in no_pin_leads:
        leads[l["url"]] = {**leads.get(l["url"], {}), **l, "first_seen": prev_first.get(l["url"]) or iso(now)}
    for r in reports.values():
        for l in r.get("leads", []):
            if l.get("url"):
                leads.setdefault(l["url"], {**l, "first_seen": iso(now)})
    cutoff = now - timedelta(days=21)
    leads = sorted((l for l in leads.values() if (parse_iso(l.get("published") or l.get("first_seen")) or now) >= cutoff),
                   key=lambda l: l.get("published") or l.get("first_seen") or "", reverse=True)[:60]

    budget = extract.BUDGET
    scan.update({
        "duration_s": round(time.monotonic() - t0, 1),
        "pending_links": len(page_state.get("_queue", {})),
        "feeds_known": sum(len(r.get("feeds") or []) for r in reports.values()),
        "ai_pages_read": sum(r["ai_read"] for r in reports.values()),
        "ai_cache_hits": sum(r["ai_cache_hits"] for r in reports.values()),
        "candidates": len(candidates),
    })
    write_json(config.EVENTS_FILE, {
        "generated_at": iso(now),
        "generator": "PH HackIntel pipeline 2.0",
        "offline_run": bool(args.offline),
        "llm_enabled": bool(config.llm_provider()),
        "llm": {"provider": (next(iter(budget.run_by_provider), None) if budget else None) or config.llm_provider() or None,
                "model": (budget.models.get(next(iter(budget.run_by_provider), ""), budget.model) if budget else None)
                         or (config.ANTHROPIC_MODEL if config.llm_provider() == "anthropic" else None),
                "chain": [{"provider": p,
                           "model": budget.models.get(p) if budget else None,
                           "requests_this_run": budget.run_by_provider.get(p, 0) if budget else 0,
                           "requests_today": budget.by_provider.get(p, 0) if budget else 0,
                           "paused_until": (budget.blocked.get(p) if budget and budget.is_blocked(p) else None),
                           "skipped": (budget.off.get(p) if budget else None)} for p in config.llm_chain()],
                "requests_this_run": budget.run_count if budget else 0,
                "requests_today": budget.day_count if budget else 0,
                "daily_cap": config.LLM_DAILY_CAP,
                "stopped": (budget.stopped or None) if budget else None},
        "scan": scan,
        "sources": [{**{k: v for k, v in r.items() if k not in ("needs_llm", "leads")},
                     "needs_llm": len(r["needs_llm"]), "leads": len(r.get("leads", []))} for r in reports.values()],
        "events": published,
        "leads": leads,
        "held": held,
    })
    write_json(config.REVIEW_FILE, {"generated_at": iso(now), "items": review,
                                    "pages_needing_llm": sorted({u for r in reports.values() for u in r["needs_llm"]}),
                                    "pending_links": page_state.get("_queue", {})})
    prune_state(page_state, now)
    write_json(config.PAGE_STATE, page_state)
    if budget and (budget.run_count or budget.blocked):
        budget.save()
    geocoder.save()
    print(f"[done] published={len(published)} held={len(held)} review={len(review)} pages={scan['pages_opened']} "
          f"pending={scan['pending_links']} http_errors={len(http.log)} in {scan['duration_s']}s", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
