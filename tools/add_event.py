"""Add a hackathon or CTF you found (for example on Facebook) to config/manual_events.yaml.

    python tools/add_event.py

You copy facts from the official post; the tool checks the location against the Philippine
province boundaries, builds the YAML entry, and appends it to config/manual_events.yaml.
Nothing is sent anywhere. Commit and push afterwards; the next scan publishes it.

Works for upcoming events and for events that already happened (past events stay ~2 months).
"""
from __future__ import annotations

import pathlib
import re
import sys
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from pipeline import config, extract, geo  # noqa: E402
from pipeline.store import slug  # noqa: E402

PHT = timezone(timedelta(hours=8))


def ask(prompt: str, required: bool = False, default: str | None = None, check=None) -> str:
    while True:
        hint = f" [{default}]" if default else (" (required)" if required else " (Enter to skip)")
        val = input(f"{prompt}{hint}: ").strip()
        if not val and default is not None:
            val = default
        if not val and required:
            print("  This field is required.")
            continue
        if val and check:
            err = check(val)
            if err:
                print(f"  {err}")
                continue
        return val


def check_date(v: str):
    return None if extract.parse_dt(v) else "Use YYYY-MM-DD or YYYY-MM-DD HH:MM (Philippine time)."


def check_url(v: str):
    return None if re.match(r"^https?://\S+$", v) else "Paste a full link starting with http:// or https://"


def check_latlon(v: str):
    m = re.match(r"^\s*(-?\d+(\.\d+)?)\s*,\s*(-?\d+(\.\d+)?)\s*$", v)
    return None if m else "Use: latitude, longitude   e.g. 10.6969, 122.5644"


def main() -> int:
    print(__doc__.split("\n\n")[0])
    print("Copy only what the official post states. Leave a field empty if it isn't stated.\n")

    name = ask("Event name", required=True)
    category = ask("Category: hackathon or ctf", default=extract.classify({"name": name}),
                   check=lambda v: None if v in ("hackathon", "ctf") else "Type hackathon or ctf.")
    organizer = ask("Organizer")
    mode = ask("Mode: onsite, hybrid or online", default="onsite",
               check=lambda v: None if v in ("onsite", "hybrid", "online") else "Type onsite, hybrid or online.")
    if mode == "online":
        print("  Note: the map shows events with a Philippine venue only. Online events go to Signals.")
    venue = ask("Venue name")
    city = ask("City or municipality", required=mode != "online")
    province = ask("Province (or Metro Manila)", required=mode != "online")
    start = ask("Event start date", required=True, check=check_date)
    end = ask("Event end date", check=check_date)
    deadline = ask("Registration deadline", check=check_date)
    eligibility = ask("Who can join")
    themes = [t.strip() for t in ask("Themes or tracks, comma separated").split(",") if t.strip()]
    reg = ask("Registration link (Google Form, organizer page...)", check=check_url)
    src_url = ask("Source link (the post or page you read this from)", required=True, check=check_url)
    src_label = ask("Source name", default="Facebook post" if "facebook.com" in src_url else "Official page")
    results = ask("Results / winners, one line (past events only)")
    by = ask("Your name (who checked it)", default="manual")

    ev = {"name": name, "mode": mode, "venue": venue, "city": city, "province": province}

    # Location check against the PSGC boundaries, using the gazetteer of city centres.
    if mode != "online":
        geo.resolve(ev, None)
        if ev.get("lat") is None:
            print(f"\n  '{city}, {province}' is not in the gazetteer, or does not match that province.")
            print("  Open Google Maps, right-click the venue, click the coordinates to copy them, and paste here.")
            ll = ask("Venue coordinates (lat, lon)", check=check_latlon)
            if ll:
                lat, lon = (float(x) for x in ll.split(","))
                ev.update(lat=lat, lon=lon, precision="EXACT", location_source="manual")
                ev = geo.resolve(ev, None)
            if ev.get("lat") is None:
                print("  Still not inside that province. Check the city, province and coordinates.")
                print("  The entry will be saved, but it will not appear on the map until the location resolves.")
        if ev.get("lat") is not None:
            print(f"  Location OK: {ev['lat']}, {ev['lon']} ({ev['precision']} precision, {ev['location_check']})")

    entry = {
        "id": slug(name if start[:4] in name else f"{name} {start[:4]}"),
        "name": name,
        "category": category,
        "organizer": organizer or None,
        "mode": mode,
        "venue": venue or None,
        "city": city or None,
        "province": province or None,
        "start": start,
        "end": end or None,
        "deadline": deadline or start,
        "deadline_is_estimate": not bool(deadline),
        "eligibility": eligibility or None,
        "themes": themes or None,
        "registration_url": reg or None,
        "results": results or None,
        "sources": [{"label": src_label, "url": src_url}],
        "verified_manually": datetime.now(PHT).strftime("%Y-%m-%dT%H:%M"),
        "verified_by": by,
    }
    if ev.get("location_source") == "manual":
        entry.update(lat=ev["lat"], lon=ev["lon"], precision="EXACT", location_source="manual")
    entry = {k: v for k, v in entry.items() if v is not None}

    block = yaml.safe_dump([entry], sort_keys=False, allow_unicode=True, width=110)
    block = "\n".join("  " + line if line else line for line in block.splitlines())

    print("\n----- entry -----\n" + block + "\n-----------------")
    if input("Save this to config/manual_events.yaml? [y/N]: ").strip().lower() != "y":
        print("Not saved.")
        return 1

    path = config.MANUAL_FILE
    text = path.read_text(encoding="utf-8")
    if re.search(r"^events:\s*\[\]\s*$", text, re.M):
        text = re.sub(r"^events:\s*\[\]\s*$", "events:", text, flags=re.M)
    if not text.endswith("\n"):
        text += "\n"
    existing = {e.get("id") for e in (yaml.safe_load(text) or {}).get("events") or []}
    if entry["id"] in existing:
        print(f"An entry with id '{entry['id']}' already exists. Edit it in the file instead.")
        return 1
    path.write_text(text + "\n" + block + "\n", encoding="utf-8")
    yaml.safe_load(path.read_text(encoding="utf-8"))  # fail loudly if the file is no longer valid YAML
    print(f"Saved. Next: git add config/manual_events.yaml, commit and push. The next scan publishes it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
