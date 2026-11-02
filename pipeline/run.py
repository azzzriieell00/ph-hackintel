"""Run one discovery pass and write data/events.json and data/review.json.

    python -m pipeline.run                 # normal run
    python -m pipeline.run --offline       # no network: re-evaluate existing data and manual entries only
    python -m pipeline.run --only SOURCE   # run a single source id (debugging)
"""
from __future__ import annotations

import argparse
import sys
from datetime import timedelta

import feedparser
import yaml

from . import config, extract, geo
from .fetch import Http
from .store import (iso, load_json, make_id, merge, now_utc, parse_iso, publish_decision, write_json)

PH_NAMES = {"ph", "phl", "philippines", "republic of the philippines", "pilipinas"}
BLOCKED: set[str] = set()


def host_of(url: str) -> str:
    from urllib.parse import urlsplit
    return urlsplit(url).netloc.lower()


def is_blocked(url: str) -> bool:
    h = host_of(url)
    return h in BLOCKED or any(h.endswith("." + b) for b in BLOCKED)


def add_lead(report: dict, url: str, title: str = "", source_name: str = "", published: str | None = None, why: str = ""):
    report.setdefault("leads", []).append({"title": title.strip(), "url": url, "source": source_name,
                                           "published": published, "note": why})


def load_yaml(path):
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {}


def process_page(http, url: str, source: dict, report: dict, trusted: bool) -> list[dict]:
    """Fetch one announcement page and return candidate events from it."""
    if is_blocked(url):
        add_lead(report, url, source_name=source.get("name", ""), why="platform does not allow automated access: check by hand")
        return []
    r = http.get(url)
    if r is None or r.status_code >= 400:
        report["errors"].append(f"{url}: {'no response' if r is None else 'HTTP %d' % r.status_code}")
        return []
    html = r.text
    found = extract.jsonld_events(html, r.url)
    if not trusted:
        found = [e for e in found if extract.is_relevant(e["name"], e.get("description", ""))]
    if not found:
        text = extract.page_text(html)
        if trusted or extract.is_relevant(extract.page_title(html), text[:3000]):
            llm = extract.llm_extract(http, text, r.url)
            if llm:
                found = [llm]
            elif not config.ANTHROPIC_API_KEY:
                report["needs_llm"].append(r.url)
    for e in found:
        e["source_id"] = source["id"]
        e["sources"] = [{"label": source.get("name", source["id"]), "url": r.url}]
    return found


def run_source(http, src: dict, page_state: dict, report: dict) -> list[dict]:
    t = src["type"]
    if t == "event_page":
        return process_page(http, src["url"], src, report, trusted=True)
    if t == "listing":
        r = http.get(src["url"])
        if r is None or r.status_code >= 400:
            report["errors"].append(f"{src['url']}: {'no response or blocked' if r is None else 'HTTP %d' % r.status_code}")
            return []
        offsite: list = []
        links = extract.candidate_links(r.text, r.url, src.get("allow_hosts"), src.get("link_pattern"), offsite)
        for u, text in offsite:
            if is_blocked(u):   # e.g. a registration page on Luma or Facebook: a person has to check it
                add_lead(report, u, text, src.get("name", ""), None, "linked from source; platform does not allow automated access")
        return follow(http, links, src, page_state, report)
    if t == "rss":
        r = http.get(src["url"])
        if r is None or r.status_code >= 400:
            report["errors"].append(f"{src['url']}: feed unreachable")
            return []
        feed = feedparser.parse(r.content)
        links = []
        for it in feed.entries:
            title, summary = it.get("title", ""), it.get("summary", "")
            if src.get("require_keyword", True) and not extract.is_relevant(title, summary):
                continue
            if src.get("leads_only"):
                pub = None
                if it.get("published_parsed"):
                    from datetime import datetime, timezone
                    pub = iso(datetime(*it.published_parsed[:6], tzinfo=timezone.utc))
                srcname = (it.get("source") or {}).get("title", "") if isinstance(it.get("source"), dict) else ""
                add_lead(report, it.get("link", ""), title, srcname or src.get("name", ""), pub, "headline only, not verified")
                continue
            link = extract.clean_url(it.get("link"))
            if link and "google.com/url" in link:   # Google Alerts wraps results in a redirect
                from urllib.parse import parse_qs, urlsplit
                link = extract.clean_url((parse_qs(urlsplit(link).query).get("url") or [None])[0])
            if link:
                links.append(link)
        return follow(http, links, src, page_state, report)
    report["errors"].append(f"unknown source type {t!r}")
    return []


def follow(http, links, src, page_state, report):
    """Open announcement links, skipping ones checked in the last day."""
    out, now, opened = [], now_utc(), 0
    for link in links:
        last = parse_iso(page_state.get(link, {}).get("checked"))
        if last and now - last < timedelta(hours=src.get("recheck_hours", 24)):
            continue
        if opened >= config.MAX_FOLLOW_PER_SOURCE:
            report.setdefault("notes", []).append(f"stopped after {opened} new links; the rest wait for the next run")
            break
        opened += 1
        found = process_page(http, link, src, report, trusted=False)
        page_state[link] = {"checked": iso(now), "found": len(found)}
        out.extend(found)
    return out


def manual_events() -> list[dict]:
    doc = load_yaml(config.MANUAL_FILE)
    out = []
    for e in doc.get("events", []) or []:
        e = dict(e)
        e.setdefault("extraction", "manual")
        e.setdefault("source_id", "manual")
        for k in ("start", "end", "deadline", "verified_manually"):
            if e.get(k) is not None:
                e[k] = extract.parse_dt(str(e[k]), end_of_day=(k == "deadline"))
        e.setdefault("deadline_is_estimate", False)
        out.append(e)
    return out


def in_scope(ev: dict) -> tuple[bool, str]:
    c = (ev.get("country") or "").strip().lower()
    if c and c not in PH_NAMES:
        return False, f"country is {ev.get('country')}"
    return True, "ok"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--only")
    args = ap.parse_args(argv)

    now = now_utc()
    http = Http(user_agent=config.USER_AGENT, delay=config.PER_HOST_DELAY, timeout=config.HTTP_TIMEOUT)
    geocoder = geo.Geocoder(http, cache_path=config.GEOCODE_CACHE, enabled=config.NOMINATIM_ENABLED and not args.offline)
    page_state = load_json(config.PAGE_STATE, {})
    prev_doc = load_json(config.EVENTS_FILE, {"events": []})
    prev_all = {e["id"]: e for e in prev_doc.get("events", []) + prev_doc.get("held", [])}

    cfg = load_yaml(config.SOURCES_FILE)
    BLOCKED.clear()
    BLOCKED.update(h.lower() for h in cfg.get("blocked_hosts", []) or [])
    sources = [s for s in cfg.get("sources", []) if s.get("enabled", True)]
    if args.only:
        sources = [s for s in sources if s["id"] == args.only]

    candidates: list[dict] = manual_events()
    source_reports = []
    if not args.offline:
        for src in sources:
            rep = {"id": src["id"], "name": src.get("name", src["id"]), "type": src["type"], "url": src.get("url"),
                   "errors": [], "needs_llm": []}
            try:
                found = run_source(http, src, page_state, rep)
            except Exception as e:  # one broken source must not stop the run
                found = []
                rep["errors"].append(f"crash: {type(e).__name__}: {e}"[:300])
            rep["found"] = len(found)
            rep["ok"] = not rep["errors"]
            source_reports.append(rep)
            candidates.extend(found)
            print(f"[source] {src['id']}: {len(found)} candidate(s), {len(rep['errors'])} error(s)", file=sys.stderr)

    review, by_id = [], {}
    for c in candidates:
        ok, why = in_scope(c)
        if not ok:
            review.append({"name": c.get("name"), "url": (c.get("sources") or [{}])[0].get("url"), "reason": why})
            continue
        c["id"] = make_id(c)
        if c["id"] in by_id:            # same event from two sources: keep both source links
            by_id[c["id"]]["sources"] = by_id[c["id"]].get("sources", []) + c.get("sources", [])
            continue
        by_id[c["id"]] = c

    # Events found earlier but not seen this run are carried forward until they expire.
    for pid, p in prev_all.items():
        if pid not in by_id:
            p = dict(p)
            p["carried_over"] = True
            by_id[pid] = p

    published, held = [], []
    for eid, ev in by_id.items():
        ev = merge(prev_all.get(eid), ev, now) if not ev.get("carried_over") else ev
        if ev.get("lat") is None or ev.get("location_source") is None or ev.get("location_check") in (None, "UNRESOLVED", "NO_VENUE_CONFIRMED"):
            geo.resolve(ev, geocoder)
        url = ev.get("registration_url") or ev.get("official_url")
        dl = parse_iso(ev.get("deadline"))
        if url and is_blocked(url):
            ev["link_status"] = "not checked automatically: platform does not allow it"
        elif url and not args.offline and dl and dl > now - timedelta(days=1):
            ok, status = http.check_link(url)
            ev["link_status"] = status
            if ok:
                ev["last_verified"] = iso(now)
                ev["link_failures"] = 0
            else:
                ev["link_failures"] = (ev.get("link_failures") or 0) + 1
        fs = parse_iso(ev.get("first_seen"))
        ev["is_new"] = bool(fs and now - fs <= timedelta(hours=config.NEW_WINDOW_H)) and ev.get("extraction") != "manual"
        ok, why = publish_decision(ev, now)
        ev.pop("description", None)
        if ok:
            ev.pop("hold_reason", None)
            published.append(ev)
        else:
            ev["hold_reason"] = why
            held.append(ev)
            review.append({"name": ev.get("name"), "id": eid, "url": url, "reason": why})

    published.sort(key=lambda e: e.get("deadline") or "")

    # Leads: unverified headlines and links on platforms we must not fetch. Kept 21 days.
    leads = {l["url"]: l for l in prev_doc.get("leads", []) if l.get("url")}
    for r in source_reports:
        for l in r.get("leads", []):
            if l.get("url"):
                leads.setdefault(l["url"], {**l, "first_seen": iso(now)})
    cutoff = now - timedelta(days=21)
    leads = sorted((l for l in leads.values() if (parse_iso(l.get("published") or l.get("first_seen")) or now) >= cutoff),
                   key=lambda l: l.get("published") or l.get("first_seen") or "", reverse=True)[:60]
    write_json(config.EVENTS_FILE, {
        "generated_at": iso(now),
        "generator": "PH HackIntel pipeline 1.0",
        "offline_run": bool(args.offline),
        "llm_enabled": bool(config.ANTHROPIC_API_KEY),
        "sources": [{**{k: v for k, v in r.items() if k not in ("needs_llm", "leads")},
                     "needs_llm": len(r["needs_llm"]), "leads": len(r.get("leads", []))} for r in source_reports],
        "events": published,
        "leads": leads,
        "held": held,
    })
    write_json(config.REVIEW_FILE, {"generated_at": iso(now), "items": review,
                                    "pages_needing_llm": sorted({u for r in source_reports for u in r["needs_llm"]})})
    write_json(config.PAGE_STATE, page_state)
    geocoder.save()
    print(f"[done] published={len(published)} held={len(held)} review={len(review)} "
          f"http_errors={len(http.log)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
