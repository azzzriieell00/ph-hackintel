import json

from pipeline import extract


def page(obj):
    return f'<html><head><script type="application/ld+json">{json.dumps(obj)}</script></head><body></body></html>'


def test_jsonld_onsite_with_deadline_bare_date_is_end_of_day_pht():
    evs = extract.jsonld_events(page({
        "@context": "https://schema.org", "@type": "Event", "name": "Cebu Hackathon",
        "startDate": "2099-01-10T09:00:00+08:00",
        "location": {"@type": "Place", "name": "Hall A", "address": {"addressLocality": "Cebu City", "addressRegion": "Cebu", "addressCountry": "PH"}},
        "offers": {"validThrough": "2099-01-05"}}), "https://org.example/e")
    assert len(evs) == 1
    e = evs[0]
    assert e["mode"] == "onsite" and e["city"] == "Cebu City" and e["country"] == "PH"
    assert e["deadline"] == "2099-01-05T23:59:00+08:00" and e["deadline_is_estimate"] is False


def test_jsonld_without_deadline_uses_start_and_flags_estimate():
    e = extract.jsonld_events(page({"@type": "Event", "name": "X Hackathon", "startDate": "2099-02-01"}), "https://o.example")[0]
    assert e["deadline"].startswith("2099-02-01T00:00") and e["deadline_is_estimate"] is True


def test_jsonld_virtual_and_mixed_modes_and_graph():
    data = {"@graph": [
        {"@type": "Event", "name": "Online Hack", "location": {"@type": "VirtualLocation", "url": "https://x"}},
        {"@type": ["Thing", "EducationEvent"], "name": "Mixed Hack",
         "eventAttendanceMode": "https://schema.org/MixedEventAttendanceMode", "location": {"@type": "Place", "name": "Y"}},
        {"@type": "Organization", "name": "Not an event"}]}
    evs = {e["name"]: e for e in extract.jsonld_events(page(data), "https://o.example")}
    assert evs["Online Hack"]["mode"] == "online"
    assert evs["Mixed Hack"]["mode"] == "hybrid"
    assert "Not an event" not in evs


def test_broken_jsonld_is_ignored():
    html = '<script type="application/ld+json">{not json</script>'
    assert extract.jsonld_events(html, "https://o.example") == []


def test_keyword_matching_uses_word_boundaries():
    assert extract.is_relevant("Join the DICT CTF this weekend")
    assert extract.is_relevant("Regional Hackathon 2026")
    assert not extract.is_relevant("ctfd platform docs and abstracts")


def test_llm_output_may_not_invent_links():
    text = "Hackathon in Iloilo. Register at https://real.example/form"
    ok = extract.from_llm({"is_hackathon": True, "in_scope_philippines": True, "name": "H",
                           "registration_url": "https://real.example/form"}, "https://p.example", text)
    assert ok["registration_url"] == "https://real.example/form"
    invented = extract.from_llm({"is_hackathon": True, "in_scope_philippines": True, "name": "H",
                                 "registration_url": "https://made-up.example/x"}, "https://p.example", text)
    assert invented["registration_url"] == "https://p.example"


def test_llm_output_out_of_scope_is_dropped():
    assert extract.from_llm({"is_hackathon": True, "in_scope_philippines": False, "name": "H"}, "u", "") is None
    assert extract.from_llm({"is_hackathon": False, "in_scope_philippines": True, "name": "H"}, "u", "") is None
