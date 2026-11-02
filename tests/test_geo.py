from pipeline import geo


def test_every_gazetteer_entry_lies_in_its_province():
    bad = []
    for v in geo.venues():
        p = geo.lookup(v["lat"], v["lon"])
        if p is None or not geo.province_matches(v["province"], p):
            bad.append((v["name"], v["province"], None if p is None else geo.province_display(p)))
    assert not bad, f"gazetteer entries outside their stated province: {bad}"


def test_open_sea_and_abroad_are_rejected():
    assert geo.lookup(13.0, 119.0) is None      # West Philippine Sea
    assert geo.lookup(1.35, 103.82) is None     # Singapore


def test_resolve_city_from_gazetteer():
    ev = geo.resolve({"mode": "onsite", "city": "Kalibo", "province": "Aklan"}, None)
    assert ev["location_check"] == "MATCH" and ev["precision"] == "CITY" and ev["location_source"] == "gazetteer"


def test_resolve_rejects_wrong_province():
    ev = geo.resolve({"mode": "onsite", "city": "Kalibo", "province": "Cebu"}, None)
    assert ev["lat"] is None and ev["location_check"] == "UNRESOLVED"


def test_resolve_rejects_source_coordinates_in_wrong_province():
    ev = geo.resolve({"mode": "onsite", "lat": 10.3157, "lon": 123.8854, "province": "Iloilo", "city": ""}, None)
    assert ev["lat"] is None


def test_online_has_no_location():
    ev = geo.resolve({"mode": "online", "city": "Manila"}, None)
    assert ev["lat"] is None and ev["location_check"] == "N/A"


def test_alias_and_metro_manila():
    ev = geo.resolve({"mode": "onsite", "city": "BGC", "province": "NCR"}, None)
    assert ev["location_check"] == "MATCH" and ev["province"] == "NCR"
