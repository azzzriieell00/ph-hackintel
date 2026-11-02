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


@pytest.fixture()
def site():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(FIX))
    handler.log_message = lambda *a, **k: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
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
    assert held["Mystery Town Hackathon"]["hold_reason"].startswith("location unresolved")

    lead_urls = {l["url"] for l in out["leads"]}
    assert "https://blocked.test/e/123" in lead_urls          # never fetched, kept as a lead
    assert "https://news.example/a1" in lead_urls              # headline with keyword
    assert "https://news.example/a2" not in lead_urls          # no keyword
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
