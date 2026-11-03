"""The real config files: every source is well formed and none points at a blocked platform."""
import re
from urllib.parse import urlsplit

import yaml

from pipeline import config, run

TYPES = {"site", "event_page", "listing", "rss", "ctftime"}


def test_sources_file_is_valid():
    cfg = yaml.safe_load(config.SOURCES_FILE.read_text(encoding="utf-8"))
    ids = [s["id"] for s in cfg["sources"]]
    assert len(ids) == len(set(ids)), "duplicate source id"
    run.BLOCKED.clear()
    run.BLOCKED.update(h.lower() for h in cfg["blocked_hosts"])
    for s in cfg["sources"]:
        assert s["type"] in TYPES, s["id"]
        assert re.fullmatch(r"[a-z0-9-]+", s["id"]), s["id"]
        u = urlsplit(s["url"])
        assert u.scheme == "https" and u.netloc, s["id"]
        if s.get("enabled", True):
            assert not run.is_blocked(s["url"]), f"{s['id']} points at a blocked platform"
        if s.get("link_pattern"):
            re.compile(s["link_pattern"])


def test_manual_events_are_well_formed():
    doc = yaml.safe_load(config.MANUAL_FILE.read_text(encoding="utf-8"))
    ids = [e["id"] for e in doc["events"]]
    assert len(ids) == len(set(ids))
    for e in doc["events"]:
        assert e.get("category") in ("hackathon", "ctf"), e["id"]
        assert e.get("city") and e.get("province"), e["id"]
        assert e.get("start") or e.get("deadline"), e["id"]
        assert any(s.get("url") for s in e.get("sources") or []), f"{e['id']} needs at least one source link"
