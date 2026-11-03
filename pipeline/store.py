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


def event_end(ev: dict) -> datetime | None:
    """When the event itself is over: end, else start (+1 day), else the deadline."""
    end = parse_iso(ev.get("end"))
    if end:
        return end
    start = parse_iso(ev.get("start"))
    if start:
        return start + timedelta(days=1)
    return parse_iso(ev.get("deadline"))


def is_finished(ev: dict, now: datetime) -> bool:
    end = event_end(ev)
    return bool(end and end < now)


def phase(ev: dict, now: datetime) -> str:
    """upcoming (registration open) | registration_closed | ongoing | finished"""
    if is_finished(ev, now):
        return "finished"
    start, dl = parse_iso(ev.get("start")), parse_iso(ev.get("deadline"))
    if start and start <= now:
        return "ongoing"
    if dl and dl < now:
        return "registration_closed"
    return "upcoming"


def publish_decision(ev: dict, now: datetime) -> tuple[bool, str]:
    dl = parse_iso(ev.get("deadline"))
    if not dl and not parse_iso(ev.get("start")):
        return False, "no deadline or event date"
    if (ev.get("status_note") or "").lower() in ("eventcancelled", "cancelled"):
        return False, "cancelled"
    if is_finished(ev, now):
        # Past events: shown for about two months. They need a pin and a public source that documents them;
        # registration links are not checked any more (they usually close after the event).
        if event_end(ev) < now - timedelta(days=config.PAST_WINDOW_D):
            return False, f"finished more than {config.PAST_WINDOW_D} days ago"
        if not any(s.get("url") for s in ev.get("sources") or []):
            return False, "finished event without a source link"
        if ev.get("lat") is None or ev.get("lon") is None:
            return False, f"finished event without a confirmed Philippine venue ({ev.get('location_check')})"
        return True, "ok"
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
    if ev.get("lat") is None or ev.get("lon") is None:
        # The map shows pinned events only: a confirmed place in the Philippines is required.
        if ev.get("mode") == "online":
            return False, "online only: no Philippine venue to pin (kept as a signal)"
        return False, f"no confirmed Philippine venue ({ev.get('location_check')}) (kept as a signal)"
    return True, "ok"


# ------------------------------------------------------------------ analysis helpers
_STOP = {"the", "of", "and", "for", "in", "on", "at", "a", "an", "ph", "philippines", "philippine", "pilipinas",
         "edition", "annual", "national", "regional", "region", "competition", "challenge", "hackathon", "ctf",
         "capture", "flag", "cybersecurity", "cyber", "security", "contest", "event", "round", "qualifying",
         "qualifier", "qualifiers", "finals", "final", "season", "leg", "st", "nd", "rd", "th", "s"}


def name_tokens(name: str) -> set[str]:
    return {t for t in slug(name or "").split("-") if t and t not in _STOP and not re.fullmatch(r"(19|20)\d\d", t)}


def _pht_date(s):
    d = parse_iso(s)
    return d.astimezone(timezone(timedelta(hours=8))).date() if d else None


def _prov(s):
    p = (s or "").strip().lower()
    return "metro manila" if p in ("ncr", "national capital region", "metro manila") else p


def similar_event(a: dict, b: dict) -> bool:
    """Same competition reported by two sources under different names? Conservative: same category,
    start dates at most one day apart, no conflicting province, and most distinctive name words shared."""
    if (a.get("category") or "") != (b.get("category") or ""):
        return False
    sa, sb = _pht_date(a.get("start")), _pht_date(b.get("start"))
    if not sa or not sb or abs((sa - sb).days) > 1:
        return False
    pa, pb = _prov(a.get("province")), _prov(b.get("province"))
    if pa and pb and pa != pb:
        return False
    ta, tb = name_tokens(a.get("name")), name_tokens(b.get("name"))
    if not ta or not tb:
        return False
    return len(ta & tb) / min(len(ta), len(tb)) >= 0.6


def _url_key(ev: dict) -> str:
    u = (ev.get("official_url") or "").strip().lower()
    return u.split("://", 1)[-1].split("#", 1)[0].rstrip("/") if u else ""


def compatible(a: dict, b: dict) -> bool:
    """Nothing contradicts a and b being one event: start dates (if both known) at most a day apart,
    provinces (if both known) the same."""
    sa, sb = _pht_date(a.get("start")), _pht_date(b.get("start"))
    if sa and sb and abs((sa - sb).days) > 1:
        return False
    pa, pb = _prov(a.get("province")), _prov(b.get("province"))
    return not (pa and pb and pa != pb)


def names_overlap(a: dict, b: dict, ratio: float = 0.5) -> bool:
    ta, tb = name_tokens(a.get("name")), name_tokens(b.get("name"))
    return bool(ta and tb) and len(ta & tb) / min(len(ta), len(tb)) >= ratio


def is_twin(keep: dict, new: dict) -> bool:
    """Is `new` the same event as `keep`?
      * same id; or
      * nothing contradicts it (dates, province) and: same name and category; or same official page and
        (same category or overlapping names) - one page can announce several events; or
      * similar_event (another name for the same competition), unless both were entered by hand."""
    if keep.get("id") and keep.get("id") == new.get("id"):
        return True
    if not compatible(keep, new):
        return False
    same_cat = (keep.get("category") or "") == (new.get("category") or "")
    if slug(keep.get("name") or "") and slug(keep.get("name") or "") == slug(new.get("name") or ""):
        return same_cat
    uk = _url_key(keep)
    if uk and uk == _url_key(new):
        return same_cat or names_overlap(keep, new)
    if keep.get("extraction") == "manual" and new.get("extraction") == "manual":
        return False
    return similar_event(keep, new)


def add_sources(target: dict, other: dict) -> None:
    have = {s.get("url") for s in target.get("sources") or []}
    target["sources"] = (target.get("sources") or []) + [s for s in other.get("sources") or [] if s.get("url") not in have]


def source_hosts(ev: dict) -> set[str]:
    from urllib.parse import urlsplit
    out = set()
    for s in ev.get("sources") or []:
        if s.get("url"):
            h = urlsplit(s["url"]).netloc.lower()
            out.add(h[4:] if h.startswith("www.") else h)
    return out


def confidence(ev: dict, now: datetime) -> dict:
    """0-100 score with the reasons behind it, shown on the intel panel."""
    r: list[tuple[int, str]] = []
    finished = is_finished(ev, now)
    ext = ev.get("extraction")
    if ext == "manual":
        r.append((30, "entered by a person from the source"))
    elif ext == "jsonld":
        r.append((30, "structured event data published on the page"))
    elif ext == "api":
        r.append((30, "structured data from a public event API"))
    elif ext == "llm":
        r.append((15, "read by AI from the page text, then checked"))
        if ev.get("ai_checks"):
            r.append((-5, "some AI-read details failed the checks and were dropped"))
    mv = parse_iso(ev.get("verified_manually"))
    if finished:
        if any(s.get("url") for s in ev.get("sources") or []):
            r.append((20, "documented by a published source"))
        if mv:
            r.append((10, "checked by a person"))
        if ev.get("results"):
            r.append((5, "results reported"))
    else:
        lv = parse_iso(ev.get("last_verified"))
        if lv and now - lv <= timedelta(hours=config.LINK_OK_WINDOW_H):
            r.append((20, "official link answered in the last 72 hours"))
        elif ev.get("link_failures"):
            r.append((-10, f"official link failed {ev['link_failures']} check(s)"))
        if mv and now - mv <= timedelta(days=config.MANUAL_OK_WINDOW_D):
            r.append((20, "checked by a person in the last 7 days"))
        if ev.get("deadline_is_estimate"):
            r.append((-10, "no registration deadline stated"))
    lc = ev.get("location_check")
    if lc == "MATCH":
        r.append((15, "pin is inside the province the source names"))
    elif lc == "MISMATCH":
        r.append((-20, "pin and named province disagree"))
    if ev.get("precision") == "EXACT":
        r.append((5, "venue located exactly"))
    n = len(source_hosts(ev))
    if n >= 2:
        r.append((min(20, 10 * (n - 1)), f"corroborated by {n} independent websites"))
    score = max(0, min(100, sum(p for p, _ in r)))
    return {"score": score, "level": "HIGH" if score >= 75 else "MEDIUM" if score >= 50 else "LOW",
            "reasons": [[p, t] for p, t in r]}
