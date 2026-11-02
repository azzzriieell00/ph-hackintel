"""Location resolution: text -> coordinates -> province check.

Order of attempts, most precise first:
  1. venue gazetteer (data/venues.json), checked by hand
  2. Nominatim (OpenStreetMap) for the venue, then for the city
  3. gazetteer city centre
A result is accepted only if its coordinates fall in the Philippines and, when the
record names a province, inside that province (using the same PSGC dot grid the map draws).
"""
from __future__ import annotations

import json
import math
import re
import time
import unicodedata

from . import config

_GEO = None
_BUCKETS = None
CELL = 0.1
KX = math.cos(math.radians(12.5))


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    s = s.lower().replace("&", " and ")
    s = re.sub(r"\bcity of\b", "", s)
    s = re.sub(r"\bcity\b", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def _load_geo():
    global _GEO, _BUCKETS
    if _GEO is None:
        _GEO = json.loads(config.GEO_FILE.read_text(encoding="utf-8"))
        _BUCKETS = {}
        d = _GEO["dots"]
        for i in range(0, len(d), 3):
            key = (math.floor(d[i] / CELL), math.floor(d[i + 1] / CELL))
            _BUCKETS.setdefault(key, []).append(i // 3)
    return _GEO


def province_display(i: int) -> str:
    raw = _load_geo()["provinces"][i][0]
    if raw.startswith("NCR"):
        return "Metro Manila"
    return re.sub(r"\s*\(Not a Province\)\s*", "", raw, flags=re.I).strip()


def region_of(i: int) -> str:
    g = _load_geo()
    return g["regions"][g["provinces"][i][1]]


def lookup(lat: float, lon: float, max_deg: float = 0.12):
    """Nearest land dot within max_deg. Returns province index or None (open sea / abroad)."""
    g = _load_geo()
    d = g["dots"]
    cx, cy = math.floor(lon / CELL), math.floor(lat / CELL)
    best, bd = None, max_deg * max_deg
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            for idx in _BUCKETS.get((cx + dx, cy + dy), ()):
                ex = (d[idx * 3] - lon) * KX
                ey = d[idx * 3 + 1] - lat
                dist = ex * ex + ey * ey
                if dist < bd:
                    bd, best = dist, d[idx * 3 + 2]
    return best


def province_matches(stated: str | None, prov_index: int | None) -> bool | None:
    """True/False when a province is stated, None when nothing to compare."""
    if prov_index is None:
        return False
    if not stated:
        return None
    a, b = _norm(stated), _norm(province_display(prov_index))
    if a in ("metro manila", "ncr", "national capital region"):
        return b == "metro manila"
    return a == b


# ---------------------------------------------------------------- gazetteer
_VENUES = None


def venues():
    global _VENUES
    if _VENUES is None:
        _VENUES = json.loads(config.VENUES_FILE.read_text(encoding="utf-8"))["venues"]
    return _VENUES


def gazetteer_match(text: str, kind: str | None = None):
    """Exact (normalised) match on a venue/city name or alias."""
    n = _norm(text)
    if not n:
        return None
    for v in venues():
        if kind and v["kind"] != kind:
            continue
        names = [v["name"], *v.get("aliases", [])]
        if any(_norm(x) == n for x in names):
            return v
    return None


# ---------------------------------------------------------------- nominatim
class Geocoder:
    def __init__(self, http, cache_path=config.GEOCODE_CACHE, enabled=config.NOMINATIM_ENABLED):
        self.http = http
        self.cache_path = cache_path
        self.enabled = enabled
        try:
            self.cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            self.cache = {}
        self._last = 0.0

    def save(self):
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(self.cache, indent=1, ensure_ascii=False), encoding="utf-8")

    def search(self, query: str):
        key = _norm(query)
        if key in self.cache:
            return self.cache[key]
        if not self.enabled:
            return []
        wait = 1.1 - (time.time() - self._last)  # usage policy: max 1 request per second
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()
        r = self.http.get(config.NOMINATIM_URL, params={
            "q": query, "countrycodes": "ph", "format": "jsonv2", "addressdetails": 1, "limit": 3,
        }, respect_robots=False)  # Nominatim is an API with its own usage policy, which we follow
        results = []
        if r is not None and r.status_code == 200:
            for it in r.json():
                results.append({
                    "lat": float(it["lat"]), "lon": float(it["lon"]),
                    "category": it.get("category") or it.get("class"), "type": it.get("type"),
                    "display_name": it.get("display_name", ""),
                })
        self.cache[key] = results
        return results


def _precision_for(result) -> str:
    if result.get("category") in ("amenity", "building", "office", "tourism", "leisure") or \
            result.get("type") in ("university", "college", "school", "conference_centre", "events_venue"):
        return "EXACT"
    return "CITY"


def resolve(ev: dict, geocoder: Geocoder | None):
    """Fill lat/lon/precision/location_source/location_check on an onsite or hybrid event.

    Returns the event. Never invents a location: when nothing passes the checks,
    lat/lon stay None and location_check explains why.
    """
    if ev.get("mode") == "online":
        ev.update(lat=None, lon=None, precision="NONE", location_check="N/A")
        return ev

    stated_prov = ev.get("province") or None
    city, venue = ev.get("city") or "", ev.get("venue") or ""

    def accept(lat, lon, precision, source):
        p = lookup(lat, lon)
        if p is None:
            return False
        ok = province_matches(stated_prov, p)
        if ok is False:
            return False
        ev.update(lat=round(lat, 5), lon=round(lon, 5), precision=precision, location_source=source,
                  province=ev.get("province") or province_display(p),
                  location_check="MATCH" if ok else "PROVINCE_FROM_MAP")
        return True

    # Coordinates given by a structured source still have to pass the checks.
    if ev.get("lat") is not None and ev.get("lon") is not None:
        if accept(float(ev["lat"]), float(ev["lon"]), ev.get("precision") or "EXACT", ev.get("location_source") or "source"):
            return ev

    if venue:
        v = gazetteer_match(venue, "venue")
        if v and accept(v["lat"], v["lon"], v["precision"], "gazetteer"):
            return ev
    if geocoder and venue and city:
        for r in geocoder.search(f"{venue}, {city}, Philippines"):
            if accept(r["lat"], r["lon"], _precision_for(r), "nominatim"):
                return ev
    if city:
        v = gazetteer_match(city, "city")
        if v and accept(v["lat"], v["lon"], "CITY", "gazetteer"):
            if not ev.get("province"):
                ev["province"] = v["province"]
            return ev
        if geocoder:
            for r in geocoder.search(f"{city}, {stated_prov + ', ' if stated_prov else ''}Philippines"):
                if accept(r["lat"], r["lon"], "CITY", "nominatim"):
                    return ev

    if ev.get("mode") == "hybrid":
        check = "NO_VENUE_CONFIRMED"    # can still join online; listed without a pin
    else:
        check = "UNRESOLVED" if (venue or city) else "NO_LOCATION"
    ev.update(lat=None, lon=None, precision="NONE", location_check=check)
    return ev
