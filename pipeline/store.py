"""Merging runs, publication rules and the output files."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime, timedelta, timezone

from . import config

UTC = timezone.utc


def now_utc() -> datetime:
    return datetime.now(UTC)


def iso(d: datetime) -> str:
    return d.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone(timedelta(hours=8)))
    except ValueError:
        return None


def slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:60]


def make_id(ev: dict) -> str:
    if ev.get("id"):
        return ev["id"]
    start = (ev.get("start") or ev.get("deadline") or "")[:7]
    base = slug(ev["name"])
    h = hashlib.sha1((ev.get("official_url") or ev["name"]).encode()).hexdigest()[:6]
    return f"{base}-{start}-{h}" if start else f"{base}-{h}"


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def merge(prev: dict | None, cur: dict, now: datetime) -> dict:
    """Keep history fields from the previous run; take content fields from the current one."""
    out = dict(cur)
    if prev:
        out["first_seen"] = prev.get("first_seen") or iso(now)
        for k in ("last_verified", "link_failures", "link_status"):
            out.setdefault(k, prev.get(k))
        # a deadline moving later is worth flagging: organisers extend often
        pd, cd = parse_iso(prev.get("deadline")), parse_iso(cur.get("deadline"))
        if pd and cd and cd > pd + timedelta(hours=1):
            out["deadline_extended_from"] = prev.get("deadline")
        elif prev.get("deadline_extended_from"):
            out["deadline_extended_from"] = prev["deadline_extended_from"]
    else:
        out["first_seen"] = iso(now)
    out["last_seen"] = iso(now)
    return out


def publish_decision(ev: dict, now: datetime) -> tuple[bool, str]:
    dl = parse_iso(ev.get("deadline"))
    if not dl:
        return False, "no deadline or event date"
    if dl < now - timedelta(days=config.KEEP_CLOSED_DAYS):
        return False, "closed more than %d days ago" % config.KEEP_CLOSED_DAYS
    if (ev.get("status_note") or "").lower() in ("eventcancelled", "cancelled"):
        return False, "cancelled"
    lv = parse_iso(ev.get("last_verified"))
    mv = parse_iso(ev.get("verified_manually"))
    skew = timedelta(hours=12)  # a check stamped in the future is a typo, not a verification
    manual_fresh = bool(mv and -skew <= now - mv <= timedelta(days=config.MANUAL_OK_WINDOW_D))
    # Some official sites refuse requests from cloud servers. A recent check by a person still counts.
    if (ev.get("link_failures") or 0) >= config.MAX_LINK_FAILURES and not manual_fresh:
        return False, f"official link failed {ev['link_failures']} checks in a row"
    fresh = (lv and -skew <= now - lv <= timedelta(hours=config.LINK_OK_WINDOW_H)) or manual_fresh
    if not fresh:
        return False, "not verified recently (link check or manual check)"
    if ev.get("mode") == "onsite" and ev.get("lat") is None:
        return False, f"location unresolved ({ev.get('location_check')})"
    return True, "ok"
