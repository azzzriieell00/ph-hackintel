"""End-to-end run against a local fixture website (no internet needed)."""
import functools
import http.server
import json
import pathlib
import shutil
import threading

import pytest

from pipeline import config, run

FIX = pathlib.Path(__file__).parent / "fixtures" / "site"


def fresh_copy(dst):
    """The fixture site with feed dates relative to today (fixed dates would age out of the 21-day signal window)."""
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime
    shutil.copytree(FIX, dst)
    when = format_datetime(datetime.now(timezone.utc) - timedelta(days=1), usegmt=True)
    for f in dst.rglob("*.xml"):
        f.write_text(f.read_text().replace("__RECENT__", when))
    return dst


class Handler(http.server.SimpleHTTPRequestHandler):
    """Static files, plus WordPress-style search feeds: /uni/?s=TERM&feed=rss2 -> /uni/search-TERM.xml"""

    def translate_path(self, path):
        from urllib.parse import parse_qs, urlsplit
        parts = urlsplit(path)
        q = parse_qs(parts.query)
        if q.get("feed") == ["rss2"] and q.get("s"):
            path = parts.path + "search-" + q["s"][0].replace(" ", "-") + ".xml"
        elif parts.path.startswith("/api/v1/events"):
            path = "/ctftime.json"
        return super().translate_path(path)

    def log_message(self, *a, **k):
        pass


class Base(str):
    """The server's base URL; .root is the folder it serves (tests may add pages to it)."""


@pytest.fixture()
def site(tmp_path_factory):
    root = fresh_copy(tmp_path_factory.mktemp("www") / "site")
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=str(root)))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = Base(f"http://127.0.0.1:{srv.server_address[1]}")
    base.root = root
    yield base
    srv.shutdown()


@pytest.fixture()
def workdir(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "data" / "state").mkdir(parents=True)
    shutil.copy(config.GEO_FILE, tmp_path / "data" / "geo.json")
    shutil.copy(config.VENUES_FILE, tmp_path / "data" / "venues.json")
    for name in ("SOURCES_FILE", "MANUAL_FILE", "EVENTS_FILE", "REVIEW_FILE", "GEOCODE_CACHE", "PAGE_STATE", "GEO_FILE", "VENUES_FILE"):
        rel = getattr(config, name).relative_to(config.ROOT)
        monkeypatch.setattr(config, name, tmp_path / rel)
    monkeypatch.setattr(config, "PER_HOST_DELAY", 0.0)
    monkeypatch.setattr(config, "NOMINATIM_ENABLED", False)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    monkeypatch.setattr(config, "LLM_PROVIDER", "")
    monkeypatch.setattr(config, "LLM_USAGE_FILE", tmp_path / "data" / "state" / "llm_usage.json")
    monkeypatch.setattr(config, "LLM_MIN_INTERVAL_S", 0.0)
    from pipeline import geo
    monkeypatch.setattr(geo, "_VENUES", None)
    return tmp_path


def write_sources(workdir, base):
    (workdir / "config" / "sources.yaml").write_text(f"""
sources:
  - id: org-news
    name: Org news
    type: listing
    url: {base}/news.html
  - id: feed
    name: Test feed
    type: rss
    leads_only: true
    url: {base}/feed.xml
blocked_hosts: [blocked.test]
""")


def test_end_to_end(site, workdir):
    write_sources(workdir, site)
    assert run.main([]) == 0
    out = json.loads((workdir / "data" / "events.json").read_text())
    names = {e["name"]: e for e in out["events"]}
    held = {e["name"]: e for e in out["held"]}

    ev = names["Iloilo Civic Hackathon 2026"]
    assert ev["deadline"] == "2099-11-30T23:59:00+08:00"
    assert ev["location_check"] == "MATCH" and ev["precision"] == "CITY" and ev["province"] == "Iloilo"
    assert ev["registration_url"].endswith("/register.html") and ev["link_status"] == "HTTP 200"
    assert ev["is_new"] is True and ev["sources"][0]["url"].endswith("/events/iloilo-hackathon.html")

    assert "Singapore Partner Hackathon" not in names and "Singapore Partner Hackathon" not in held
    assert held["Mystery Town Hackathon"]["hold_reason"].startswith("no confirmed Philippine venue")
    assert ev["category"] == "hackathon"
    ctf = names["Cebu Capture the Flag 2099"]
    assert ctf["category"] == "ctf" and ctf["province"] == "Cebu"

    lead_urls = {l["url"] for l in out["leads"]}
    assert "https://blocked.test/e/123" in lead_urls          # never fetched, kept as a lead
    assert "https://news.example/a1" in lead_urls              # headline with keyword
    assert "https://news.example/a2" not in lead_urls          # no keyword
    assert any(l["title"].startswith("[HACKATHON] Mystery Town") for l in out["leads"])  # no-pin event kept as a signal
    assert not any("other-host.test" in u for u in lead_urls)  # off-site links ignored

    review = json.loads((workdir / "data" / "review.json").read_text())
    assert any(i["reason"].startswith("country is") for i in review["items"])

    # second run: page already checked, event carried over, not new-from-scratch
    first_seen = ev["first_seen"]
    assert run.main([]) == 0
    out2 = json.loads((workdir / "data" / "events.json").read_text())
    ev2 = {e["name"]: e for e in out2["events"]}["Iloilo Civic Hackathon 2026"]
    assert ev2["first_seen"] == first_seen
    assert len(out2["leads"]) == len(out["leads"])


def test_offline_run_keeps_manual_events(workdir):
    (workdir / "config" / "sources.yaml").write_text("sources: []\n")
    from datetime import datetime, timedelta, timezone
    today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%dT%H:%M")
    (workdir / "config" / "manual_events.yaml").write_text(f"""
events:
  - id: m1
    name: Manual Hack
    mode: onsite
    city: Kalibo
    province: Aklan
    deadline: "2099-01-01"
    registration_url: https://example.org/x
    verified_manually: "{today}"
  - id: m2
    name: Stale Manual Hack
    mode: online
    deadline: "2099-01-01"
    registration_url: https://example.org/y
    verified_manually: "2020-01-01T00:00"
""")
    assert run.main(["--offline"]) == 0
    out = json.loads((workdir / "data" / "events.json").read_text())
    assert [e["id"] for e in out["events"]] == ["m1"]
    assert [e["id"] for e in out["held"]] == ["m2"]          # manual check older than 7 days
    assert out["events"][0]["location_check"] == "MATCH"


def test_removed_manual_event_disappears(workdir):
    (workdir / "config" / "sources.yaml").write_text("sources: []\n")
    from datetime import datetime, timedelta, timezone
    today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%dT%H:%M")
    (workdir / "config" / "manual_events.yaml").write_text(f"""
events:
  - id: gone
    name: Gone Hack
    mode: onsite
    city: Kalibo
    province: Aklan
    deadline: "2099-01-01"
    registration_url: https://example.org/x
    verified_manually: "{today}"
""")
    assert run.main(["--offline"]) == 0
    (workdir / "config" / "manual_events.yaml").write_text("events: []\n")
    assert run.main(["--offline"]) == 0
    out = json.loads((workdir / "data" / "events.json").read_text())
    assert not out["events"] and not out["held"] and not out["leads"]


def test_scanner_copy_of_manual_event_is_merged(site, workdir):
    """The same event entered by hand and found by the scanner on its official page appears once."""
    write_sources(workdir, site)
    from datetime import datetime, timedelta, timezone
    today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%dT%H:%M")
    (workdir / "config" / "manual_events.yaml").write_text(f"""
events:
  - id: iloilo-civic-manual
    name: Iloilo Civic Hackathon (hand entry)
    mode: onsite
    city: Iloilo City
    province: Iloilo
    start: "2099-12-05"
    deadline: "2099-11-30"
    official_url: {site}/events/iloilo-hackathon.html
    sources:
      - label: Facebook post
    verified_manually: "{today}"
""")
    assert run.main([]) == 0
    out = json.loads((workdir / "data" / "events.json").read_text())
    iloilo = [e for e in out["events"] if "Iloilo" in e["name"]]
    assert len(iloilo) == 1 and iloilo[0]["id"] == "iloilo-civic-manual"
    assert any(s.get("url", "").endswith("/events/iloilo-hackathon.html") for s in iloilo[0]["sources"])


def test_site_source_discovers_feed_homepage_links_and_search(site, workdir):
    (workdir / "config" / "sources.yaml").write_text(f"""
sources:
  - id: uni
    name: Test University news
    type: site
    url: {site}/uni/
blocked_hosts: []
""")
    assert run.main([]) == 0
    out = json.loads((workdir / "data" / "events.json").read_text())
    names = {e["name"]: e for e in out["events"]}
    assert names["Bohol Hackathon"]["province"] == "Bohol"                 # from the feed (and the homepage link)
    assert names["Hack4Health Hackathon"]["province"] == "Iloilo"          # from the site's own search feed
    rep = out["sources"][0]
    assert rep["feeds"] == [f"{site}/uni/feed.xml"]                        # comments feed skipped
    assert any("site search skipped: robots.txt disallows" in n for n in rep["notes"])   # wildcard rule obeyed
    assert rep["pages"] == 2 and rep["ok"] is True                        # enrollment page never opened
    leads = {l["title"] for l in out["leads"]}
    assert "Registration open: Bohol Hackathon" in leads and "Hack4Health wrap-up" not in leads
    for e in out["events"]:
        assert e["confidence"]["level"] in ("HIGH", "MEDIUM", "LOW") and e["confidence"]["reasons"]
    assert out["scan"]["pages_opened"] == 2 and out["scan"]["feeds_known"] == 1

    state = json.loads((workdir / "data" / "state" / "page_state.json").read_text())
    assert state["_sites"]["uni"]["wordpress"] is True and state["_sites"]["uni"]["search_at"]
    assert run.main([]) == 0                                              # second scan: nothing re-opened
    out2 = json.loads((workdir / "data" / "events.json").read_text())
    assert out2["sources"][0]["pages"] == 0 and {e["name"] for e in out2["events"]} == set(names)


def ai_page(site, name, year):
    (site.root / "uni" / "news" / "ai-ctf.html").write_text(
        f"<html><head><title>{name}</title></head><body><h1>{name}</h1>"
        f"<p>The {name} capture the flag happens on December 12, {year} at the Test Lab, Cebu City, Cebu. "
        f"Register before December 1.</p></body></html>")
    (site.root / "news2.html").write_text('<a href="/uni/news/ai-ctf.html">Cebu CTF Summit announced</a>')


def fake_ai(monkeypatch, year, calls):
    from pipeline import extract
    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-key")

    def answer(http, prompt, url):
        extract.BUDGET.used()
        calls.append(url)
        return {"events": [{"is_hackathon": True, "category": "ctf", "in_scope_philippines": True,
                            "name": "Cebu CTF Summit", "mode": "onsite", "venue": "Test Lab", "city": "Cebu City",
                            "province": "Cebu", "start": f"{year}-12-12", "registration_deadline": f"{year}-12-01"}]}
    monkeypatch.setattr(extract, "_call_gemini", answer)


def test_ai_results_are_cached_by_page_content(site, workdir, monkeypatch):
    from datetime import datetime
    year, calls = datetime.now().year + 1, []
    ai_page(site, "Cebu CTF Summit", year)
    fake_ai(monkeypatch, year, calls)
    (workdir / "config" / "sources.yaml").write_text(f"""
sources:
  - id: summit
    name: Summit page
    type: event_page
    url: {site}/uni/news/ai-ctf.html
""")
    assert run.main([]) == 0 and run.main([]) == 0
    assert len(calls) == 1                                    # unchanged page: the AI is asked once
    out = json.loads((workdir / "data" / "events.json").read_text())
    ev = {e["name"]: e for e in out["events"]}["Cebu CTF Summit"]
    assert ev["category"] == "ctf" and ev["deadline"] == f"{year}-12-01T23:59:00+08:00" and ev["province"] == "Cebu"
    assert ev["extraction"] == "llm" and out["sources"][0]["ai_cache_hits"] == 1
    assert out["llm"]["provider"] == "gemini"


def test_pages_wait_in_queue_when_ai_budget_is_used_up(site, workdir, monkeypatch):
    from datetime import datetime
    year, calls = datetime.now().year + 1, []
    ai_page(site, "Cebu CTF Summit", year)
    fake_ai(monkeypatch, year, calls)
    (workdir / "config" / "sources.yaml").write_text(f"""
sources:
  - id: news2
    name: News 2
    type: listing
    url: {site}/news2.html
""")
    monkeypatch.setattr(config, "LLM_RUN_CAP", 0)
    assert run.main([]) == 0
    out = json.loads((workdir / "data" / "events.json").read_text())
    assert not out["events"] and not calls and out["scan"]["pending_links"] == 1
    review = json.loads((workdir / "data" / "review.json").read_text())
    assert list(review["pending_links"]) == [f"{site}/uni/news/ai-ctf.html"]

    (site.root / "news2.html").write_text("<p>nothing new</p>")     # the link is gone from the listing...
    monkeypatch.setattr(config, "LLM_RUN_CAP", 5)
    assert run.main([]) == 0                                         # ...but the queue remembers it
    out = json.loads((workdir / "data" / "events.json").read_text())
    assert [e["name"] for e in out["events"]] == ["Cebu CTF Summit"] and len(calls) == 1
    assert out["scan"]["pending_links"] == 0 and out["scan"]["queued_from_earlier"] == 1


def test_same_competition_under_another_name_is_merged(site, workdir):
    write_sources(workdir, site)
    from datetime import datetime, timedelta, timezone
    today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%dT%H:%M")
    (workdir / "config" / "manual_events.yaml").write_text(f"""
events:
  - id: civic-hack-iloilo
    name: Civic Hack Iloilo
    category: hackathon
    mode: onsite
    city: Iloilo City
    province: Iloilo
    start: "2099-12-05"
    deadline: "2099-11-30"
    sources:
      - label: Facebook post
    verified_manually: "{today}"
""")
    assert run.main([]) == 0
    out = json.loads((workdir / "data" / "events.json").read_text())
    iloilo = [e for e in out["events"] if "Iloilo" in e["name"]]
    assert [e["id"] for e in iloilo] == ["civic-hack-iloilo"]
    assert iloilo[0]["sources"][-1]["url"].endswith("/events/iloilo-hackathon.html")


def test_ctftime_api_onsite_philippine_ctfs_only(site, workdir):
    from datetime import datetime, timedelta, timezone
    soon = datetime.now(timezone.utc) + timedelta(days=20)
    def e(i, title, onsite, location):
        return {"id": i, "title": title, "onsite": onsite, "location": location, "format": "Jeopardy",
                "start": soon.isoformat(), "finish": (soon + timedelta(hours=8)).isoformat(),
                "url": f"{site}/register.html?ctf={i}", "ctftime_url": f"https://ctftime.org/event/{i}/",
                "organizers": [{"id": 1, "name": "Test Team"}], "restrictions": "Open"}
    (site.root / "ctftime.json").write_text(json.dumps([
        e(1, "Cebu Onsite CTF", True, "Test Lab, Cebu City, Philippines"),
        e(2, "Santa Rosa CTF", True, "Santa Rosa, CA, USA"),
        e(3, "Online PH CTF", False, "Philippines"),
        e(4, "Mystery PH CTF", True, "Philippines"),
    ]))
    (workdir / "config" / "sources.yaml").write_text(f"""
sources:
  - id: ctftime
    name: CTFtime
    type: ctftime
    url: {site}/api/v1/events/
""")
    assert run.main([]) == 0
    out = json.loads((workdir / "data" / "events.json").read_text())
    names = {e["name"]: e for e in out["events"]}
    assert list(names) == ["Cebu Onsite CTF"]
    ev = names["Cebu Onsite CTF"]
    assert ev["category"] == "ctf" and ev["province"] == "Cebu" and ev["extraction"] == "api"
    assert ev["sources"][0]["url"] == "https://ctftime.org/event/1/" and ev["deadline_is_estimate"] is True
    assert "Santa Rosa CTF" not in {e["name"] for e in out["held"]}
    assert any(l["title"] == "[CTF] Mystery PH CTF" for l in out["leads"])      # no city: a signal, not a pin
    assert run.main([]) == 0                                                    # not due again: carried over
    out2 = json.loads((workdir / "data" / "events.json").read_text())
    assert [e["name"] for e in out2["events"]] == ["Cebu Onsite CTF"]
    assert any("not due yet" in n for n in out2["sources"][0]["notes"])


def test_one_page_announcing_two_events_keeps_both(site, workdir, monkeypatch):
    from datetime import datetime
    from pipeline import extract
    year = datetime.now().year + 1
    (site.root / "uni" / "news" / "two.html").write_text(
        f"<html><head><title>Two competitions</title></head><body>Cebu Hackathon on December 12, {year} in Cebu City, Cebu. "
        f"Davao CTF on December 12, {year} in Davao City, Davao del Sur.</body></html>")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-key")

    def answer(http, prompt, url):
        extract.BUDGET.used()
        ev = lambda n, c, city, prov: {"is_hackathon": True, "category": c, "in_scope_philippines": True, "name": n,
                                       "city": city, "province": prov, "start": f"{year}-12-12"}
        return {"events": [ev("Cebu Hackathon", "hackathon", "Cebu City", "Cebu"),
                           ev("Davao CTF", "ctf", "Davao City", "Davao del Sur")]}
    monkeypatch.setattr(extract, "_call_gemini", answer)
    (workdir / "config" / "sources.yaml").write_text(f"""
sources:
  - id: two
    name: Two
    type: event_page
    url: {site}/uni/news/two.html
""")
    assert run.main([]) == 0
    out = json.loads((workdir / "data" / "events.json").read_text())
    assert sorted(e["name"] for e in out["events"]) == ["Cebu Hackathon", "Davao CTF"]


def test_twin_found_in_a_later_scan_keeps_one_pin(site, workdir):
    write_sources(workdir, site)
    assert run.main([]) == 0
    first = {e["name"]: e for e in json.loads((workdir / "data" / "events.json").read_text())["events"]}
    old_id = first["Iloilo Civic Hackathon 2026"]["id"]
    # a new page reports the same competition under another name; the original page is not re-read
    (site.root / "events" / "civic-report.html").write_text(
        '<html><head><title>Civic Hack Iloilo</title><script type="application/ld+json">{"@type": "Event", '
        '"name": "Civic Hack Iloilo hackathon", "startDate": "2099-12-05T09:00:00+08:00", "location": {"@type": "Place", '
        '"name": "Test Venue Hall", "address": {"addressLocality": "Iloilo City", "addressRegion": "Iloilo", "addressCountry": "PH"}}, '
        '"offers": {"validThrough": "2099-11-30", "url": "/register.html"}}</script></head></html>')
    (site.root / "news.html").write_text('<a href="/events/civic-report.html">Civic Hack Iloilo hackathon report</a>')
    assert run.main([]) == 0
    out = json.loads((workdir / "data" / "events.json").read_text())
    iloilo = [e for e in out["events"] if "Iloilo" in e["name"] or "Civic" in e["name"]]
    assert len(iloilo) == 1 and iloilo[0]["id"] == old_id
    assert {s["url"].rsplit("/", 1)[-1] for s in iloilo[0]["sources"]} >= {"iloilo-hackathon.html", "civic-report.html"}


def test_unreadable_ai_answers_stop_after_three_tries(site, workdir, monkeypatch):
    from datetime import datetime
    from pipeline import extract
    ai_page(site, "Cebu CTF Summit", datetime.now().year + 1)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-key")
    calls = []

    def broken(http, prompt, url):
        extract.BUDGET.used()
        calls.append(url)
        return None                      # e.g. a blocked or truncated answer
    monkeypatch.setattr(extract, "_call_gemini", broken)
    (workdir / "config" / "sources.yaml").write_text(f"""
sources:
  - id: news2
    name: News 2
    type: listing
    url: {site}/news2.html
""")
    for _ in range(5):
        assert run.main([]) == 0
    assert len(calls) == 3
    out = json.loads((workdir / "data" / "events.json").read_text())
    assert out["scan"]["pending_links"] == 0


def test_a_crashing_page_does_not_stop_the_others(site, workdir, monkeypatch):
    real = run.process_page

    def flaky(http, url, *a, **k):
        if url.endswith("/events/cebu-ctf.html"):
            raise RuntimeError("boom")
        return real(http, url, *a, **k)
    monkeypatch.setattr(run, "process_page", flaky)
    write_sources(workdir, site)
    assert run.main([]) == 0
    out = json.loads((workdir / "data" / "events.json").read_text())
    assert "Iloilo Civic Hackathon 2026" in {e["name"] for e in out["events"]}
    rep = {r["id"]: r for r in out["sources"]}["org-news"]
    assert rep["ok"] is False and any("crash: RuntimeError: boom" in e for e in rep["errors"])
