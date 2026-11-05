"""Site discovery, link ranking, robots.txt wildcards, AI output checks, duplicate detection, confidence."""
from datetime import datetime, timedelta, timezone

import requests

from pipeline import extract
from pipeline.fetch import Http
from pipeline.store import confidence, iso, name_tokens, similar_event

NOW = datetime.now(timezone.utc)
Y = datetime.now().year


# ------------------------------------------------------------------ feeds and sites
def test_discover_feeds_skips_comment_feeds_and_resolves_links():
    html = """<head>
      <link rel="alternate" type="application/rss+xml" title="Uni &raquo; Comments Feed" href="/comments/feed/">
      <link rel="alternate" type="application/rss+xml" title="Uni &raquo; Feed" href="/feed/">
      <link rel="alternate" type="application/atom+xml" href="https://uni.example/atom.xml">
      <link rel="stylesheet" href="/x.css"></head>"""
    assert extract.discover_feeds(html, "https://uni.example/") == ["https://uni.example/feed/", "https://uni.example/atom.xml"]
    assert extract.discover_feeds("<html></html>", "https://uni.example/") == []


def test_is_wordpress():
    assert extract.is_wordpress('<link rel="https://api.w.org/" href="/wp-json/">')
    assert extract.is_wordpress('<img src="/wp-content/uploads/a.png">')
    assert extract.is_wordpress('<meta name="generator" content="WordPress 6.9">')
    assert not extract.is_wordpress("<html><body>Drupal site</body></html>")


def test_feed_items_rss_atom_relative_links_and_alerts_redirect():
    rss = f"""<?xml version="1.0"?><rss version="2.0"><channel>
      <item><title>𝗛𝗮𝗰𝗸𝗮𝘁𝗵𝗼𝗻 {Y}</title><link>/news/a</link><pubDate>Mon, 02 Nov {Y} 03:00:00 GMT</pubDate>
        <description>&lt;p&gt;Teams of &lt;b&gt;three&lt;/b&gt;&lt;/p&gt;</description></item>
      <item><title>Alert</title><link>https://www.google.com/url?rct=j&amp;url=https://pna.example/b&amp;ct=ga</link></item>
    </channel></rss>"""
    items = extract.feed_items(rss.encode(), "https://uni.example/feed/")
    assert items[0]["title"] == f"Hackathon {Y}" and items[0]["link"] == "https://uni.example/news/a"
    assert items[0]["summary"] == "Teams of three" and items[0]["published"].day == 2
    assert items[1]["link"] == "https://pna.example/b" and items[1]["published"] is None
    atom = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry><title>CTF night</title>
      <link href="https://o.example/ctf"/><updated>2026-10-01T00:00:00Z</updated></entry></feed>"""
    a = extract.feed_items(atom)
    assert a[0]["link"] == "https://o.example/ctf" and a[0]["published"].month == 10


def test_unicode_bold_titles_are_matched():
    assert extract.is_relevant("𝗥𝗧𝗨 𝗦𝘁𝘂𝗱𝗲𝗻𝘁𝘀 𝗶𝗻 𝗲𝗚𝗼𝘃𝗣𝗛 𝗛𝗮𝗰𝗸𝗮𝘁𝗵𝗼𝗻 𝟮𝟬𝟮𝟲")


def test_link_score_ranks_announcements_and_recent_posts_first():
    s = lambda title, url, days=None: extract.link_score(title, url, NOW - timedelta(days=days) if days is not None else None, NOW)
    open_call = s("Registration now open: Cebu Hackathon", "https://u.example/news/cebu", 2)
    result = s("Students win regional CTF", "https://u.example/news/ctf", 30)
    in_url_only = s("Read more", f"https://u.example/{NOW:%Y/%m}/capture-the-flag-results/")   # date from the URL
    weak = s("Five life hacks for students", "https://u.example/news/hacks", 3)
    old = s("Registration open: Old Hackathon", "https://u.example/news/old", 300)
    assert open_call > result > weak and in_url_only > weak
    assert old < result and old < 0 + 50 + 15     # an old post sinks below fresh ones
    assert s("Hackathon recap", "https://u.example/2026/02/30/x") == s("Hackathon recap", "https://u.example/x")   # impossible date ignored


# ------------------------------------------------------------------ robots.txt
class FakeSite(requests.Session):
    def __init__(self, robots):
        super().__init__()
        self.robots = robots

    def request(self, method, url, **kw):
        r = requests.Response()
        r.status_code, r.url = 200, url
        r._content = (self.robots if url.endswith("/robots.txt") else "ok").encode()
        return r


def test_robots_wildcards_are_obeyed():
    h = Http(user_agent="PH-HackIntel/1.0 (+test)", delay=0,
             session=FakeSite("User-agent: *\nDisallow: /*?s=\nDisallow: /*.pdf$\nDisallow: /private/\n"))
    assert h.allowed("https://uni.example/news/hackathon")
    assert not h.allowed("https://uni.example/?s=hackathon&feed=rss2")
    assert not h.allowed("https://uni.example/files/rules.pdf")
    assert h.allowed("https://uni.example/files/rules.pdfx")
    assert not h.allowed("https://uni.example/private/x")
    assert h.get("https://uni.example/?s=ctf") is None and "disallows" in h.why("https://uni.example/?s=ctf")


def test_robots_group_for_our_bot_wins():
    h = Http(user_agent="PH-HackIntel/1.0 (+test)", delay=0,
             session=FakeSite("User-agent: PH-HackIntel\nDisallow: /*search\n\nUser-agent: *\nDisallow: /\n"))
    assert h.allowed("https://n.example/news")
    assert not h.allowed("https://n.example/x/search?q=1")


# ------------------------------------------------------------------ AI output checks
def test_date_mentioned_formats():
    t = "Join us on Dec. 3rd! Finals Nov 27-28. Deadline: 12/01/2026. Gaganapin sa 5 Disyembre. Sept 9 kickoff. 2026-12-20."
    for d in ("2026-12-03", "2026-11-27", "2026-11-28", "2026-12-01", "2026-12-05", "2026-09-09", "2026-12-20"):
        assert extract.date_mentioned(d, t), d
    assert not extract.date_mentioned("2026-12-13", "Event on December 3")
    assert not extract.date_mentioned("2026-12-03", "Event on December 13 and December 30")
    assert not extract.date_mentioned("2026-12-03", "Event on November 3")
    assert not extract.date_mentioned(None, t) and not extract.date_mentioned("soon", t)
    assert extract.date_mentioned("2026-11-27", "Finals: 27-28 November 2026")             # day-first range start
    assert not extract.date_mentioned("2026-12-03", "Last year's edition, December 3, 2025")  # wrong year next to it
    assert extract.date_mentioned("2026-12-03", "December 3, 2026") and extract.date_mentioned("2026-11-28", "Nov 27-28, 2026")


def base(**kw):
    return {"is_hackathon": True, "in_scope_philippines": True, "name": "Test Hackathon", **kw}


def test_ai_dates_not_in_page_are_dropped():
    text = "Test Hackathon happens December 5 at the gym."
    ev = extract.from_llm(base(start=f"{Y}-12-05", registration_deadline=f"{Y}-11-20"), "https://p.example", text)
    assert ev["start"].startswith(f"{Y}-12-05")
    assert ev["deadline"] == ev["start"] and ev["deadline_is_estimate"] is True     # Nov 20 is not in the text
    assert any("not stated in the page text" in c for c in ev["ai_checks"])


def test_ai_implausible_year_and_inverted_dates():
    text = "On December 5 and December 1, with registration until December 9."
    ev = extract.from_llm(base(start="1999-12-05"), "u", text)
    assert ev["start"] is None and "implausible year" in ev["ai_checks"][0]
    ev = extract.from_llm(base(start=f"{Y}-12-05", end=f"{Y}-12-01", registration_deadline=f"{Y}-12-09"), "u", text)
    assert ev["end"] is None and ev["deadline_is_estimate"] is True
    assert "end before start: end dropped" in ev["ai_checks"] and "deadline after the event: deadline dropped" in ev["ai_checks"]


def test_ai_garbage_fields_are_ignored():
    ev = extract.from_llm(base(mode="sometimes", city=42, themes=["AI", 7], name="  𝗫 Hack  "), "u", "")
    assert ev["mode"] == "unspecified" and ev["city"] is None and ev["themes"] == ["AI"] and ev["name"] == "X Hack"
    assert extract.from_llm(base(themes=3, venue=["x"]), "u", "")["themes"] == []
    assert extract.from_llm(base(name="x" * 200), "u", "") is None
    assert extract.from_llm({"in_scope_philippines": True, "name": "No flag"}, "u", "") is None


# ------------------------------------------------------------------ duplicates and confidence
def ev(name, start, prov="Iloilo", cat="hackathon"):
    return {"name": name, "start": start, "province": prov, "category": cat}


def test_similar_event():
    a = ev("Iloilo Civic Hackathon 2099", "2099-12-05T08:00:00+08:00")
    assert similar_event(a, ev("Civic Hack Iloilo", "2099-12-05"))
    assert similar_event(a, ev("Iloilo Civic Hackathon", "2099-12-06", prov=None))
    assert not similar_event(a, ev("Iloilo Civic Hackathon", "2099-12-08"))           # different dates
    assert not similar_event(a, ev("Iloilo Civic Hackathon", "2099-12-05", prov="Cebu"))
    assert not similar_event(a, ev("Iloilo Civic Hackathon", "2099-12-05", cat="ctf"))
    assert not similar_event(ev("Cebu AI Hackathon", "2099-01-01", "Cebu"), ev("Cebu Fintech Hackathon", "2099-01-01", "Cebu"))
    assert not similar_event(a, ev("Hackathon 2099", "2099-12-05"))                   # nothing distinctive
    assert name_tokens("HackForGov 2026 Region 13 CTF Regional Qualifying Round") == {"hackforgov", "13"}


def test_confidence_levels_and_reasons():
    now = datetime(2026, 11, 3, tzinfo=timezone.utc)
    manual = {"extraction": "manual", "deadline": "2099-01-01T23:59:00+08:00", "last_verified": iso(now - timedelta(hours=1)),
              "verified_manually": iso(now - timedelta(days=1)), "location_check": "MATCH",
              "sources": [{"url": "https://a.example/x"}, {"url": "https://www.b.example/y"}]}
    c = confidence(manual, now)
    assert c["level"] == "HIGH" and c["score"] == 95 and any("2 independent" in r[1] for r in c["reasons"])
    ai = {"extraction": "llm", "deadline": "2099-01-01T00:00:00+08:00", "deadline_is_estimate": True,
          "location_check": "MATCH", "ai_checks": ["x"], "last_verified": iso(now)}
    c = confidence(ai, now)
    assert c["level"] == "LOW" and c["score"] == 35
    past = {"extraction": "llm", "start": "2026-10-01", "location_check": "MATCH", "results": "Team A won",
            "sources": [{"url": "https://u.example/news"}]}
    assert confidence(past, now)["score"] == 55


def test_ai_places_not_in_page_are_dropped():
    text = "The CTF happens December 5 at the Quezon Convention Center, Lucena City."
    ev = extract.from_llm(base(start=f"{Y}-12-05", venue="Quezon Convention Center", city="Lucena", province="Quezon",
                               ), "u", text)
    assert (ev["venue"], ev["city"], ev["province"]) == ("Quezon Convention Center", "Lucena", "Quezon")
    ev = extract.from_llm(base(start=f"{Y}-12-05", venue="SM City Lipa", city="Lipa City", province="Batangas"), "u", text)
    assert ev["venue"] is None and ev["city"] is None and ev["province"] is None
    assert len([c for c in ev["ai_checks"] if "not in the page text" in c]) == 3


def test_messy_jsonld_does_not_crash():
    import json
    html = '<script type="application/ld+json">' + json.dumps({"@type": "Event", "name": ["X Hackathon"],
        "description": {"@value": "A hackathon"}, "url": ["https://a.example"], "organizer": [{"name": ["Org"]}],
        "location": {"@type": "Place", "name": ["Hall"], "address": [{"addressLocality": ["Cebu City"], "addressCountry": {"name": "PH"}}]},
        "startDate": "2099-01-01"}) + '</script>'
    ev = extract.jsonld_events(html, "https://p.example")[0]
    assert ev["name"] == "X Hackathon" and ev["venue"] == "Hall" and ev["city"] == "Cebu City" and ev["country"] == "PH"
    assert ev["organizer"] is None and ev["official_url"] == "https://p.example"


class Redirecting(requests.Session):
    def __init__(self, hops):
        super().__init__()
        self.hops, self.seen = hops, []

    def request(self, method, url, **kw):
        self.seen.append(url)
        r = requests.Response()
        r.url = url
        if url.endswith("/robots.txt"):
            r.status_code, r._content = 200, b"User-agent: *\nDisallow: /private\n"
        elif url in self.hops:
            r.status_code, r.headers["Location"], r._content = 302, self.hops[url], b""
        else:
            r.status_code, r._content = 200, b"ok"
        return r


def test_redirects_are_checked_hop_by_hop():
    s = Redirecting({"https://uni.example/": "https://www.facebook.com/uni", "https://a.example/old": "/private/x",
                     "https://b.example/old": "/new"})
    h = Http(delay=0, session=s)
    h.blocked = {"facebook.com"}
    assert h.get("https://uni.example/") is None and "does not allow automated access" in h.why("https://uni.example/")
    assert not any("facebook" in u for u in s.seen)
    assert h.get("https://a.example/old") is None and "robots.txt disallows" in h.why("https://a.example/old")
    r = h.get("https://b.example/old")
    assert r.status_code == 200 and r.url == "https://b.example/new"
    ok, why = h.check_link("https://uni.example/")
    assert ok is None and why.startswith("inconclusive")
    assert h.get("https://b.example/api", params={"start": 1}).url == "https://b.example/api?start=1"


def test_acronyms_count_as_shared_name_words():
    from pipeline.store import name_match_ratio
    long = "HackForGov 2026 Negros Island Region CTF Regional Competition"
    assert name_match_ratio(long, "HackForGov 5 NIR Regional Competition") >= 0.6
    assert similar_event({"name": long, "start": "2026-10-12", "province": "Negros Occidental", "category": "ctf"},
                         {"name": "HackForGov 5 NIR Regional Competition", "start": "2026-10-12T00:00:00+08:00",
                          "province": "Negros Occidental", "category": "ctf"})
    assert name_match_ratio("HackForGov 2026 Region 13 CTF", "HackForGov 2026 NIR CTF") < 0.6
    assert name_match_ratio("Cebu AI Hackathon", "Cebu Fintech Hackathon") < 0.6


def test_page_published_date_from_metadata():
    assert extract.page_published('<meta property="article:published_time" content="2018-11-10T08:00:00+08:00">').year == 2018
    assert extract.page_published('<script type="application/ld+json">{"@type": "NewsArticle", "datePublished": "2021-05-21"}</script>').year == 2021
    assert extract.page_published('<p><time datetime="2026-10-01">Oct 1</time></p>').month == 10
    assert extract.page_published("<p>no date</p>") is None
