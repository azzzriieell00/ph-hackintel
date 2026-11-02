"""Assemble index.html from the template, the boundary geometry and a snapshot of data/events.json.

Usage (from the project folder):
    python tools/build_page.py

The snapshot lets the page work when opened straight from disk. When the folder is served over
http(s) (GitHub Pages, python -m http.server), the page loads data/events.json instead and
re-checks it every 10 minutes.
"""
import json
import pathlib

root = pathlib.Path(__file__).resolve().parent.parent
tpl = (root / "src" / "index.template.html").read_text(encoding="utf-8")
geo = json.loads((root / "data" / "geo.json").read_text(encoding="utf-8"))
try:
    events = json.loads((root / "data" / "events.json").read_text(encoding="utf-8"))
except FileNotFoundError:
    events = {"generated_at": None, "events": [], "leads": [], "sources": []}

snapshot = {k: events.get(k) for k in ("generated_at", "sources", "events", "leads")}
snapshot["held_count"] = len(events.get("held", []))


def embed(obj):
    # "</" inside a <script> block would end it early; escape it.
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/")


assert tpl.count("__GEO_JSON__") == 1 and tpl.count("__EVENTS_JSON__") == 1
html = tpl.replace("__GEO_JSON__", embed(geo)).replace("__EVENTS_JSON__", embed(snapshot))
out = root / "index.html"
out.write_text(html, encoding="utf-8")
print(f"wrote {out} ({out.stat().st_size/1024:.0f} KB), snapshot: {len(snapshot['events'] or [])} events, "
      f"{len(snapshot['leads'] or [])} leads")
