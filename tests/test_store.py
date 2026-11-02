from datetime import datetime, timedelta, timezone

from pipeline.store import iso, publish_decision

NOW = datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)
BASE = {"deadline": "2099-01-01T23:59:00+08:00", "mode": "onsite", "lat": 10.7, "lon": 122.56}


def test_fresh_manual_check_outranks_failed_link_checks():
    ev = {**BASE, "link_failures": 5, "verified_manually": iso(NOW - timedelta(days=2))}
    assert publish_decision(ev, NOW) == (True, "ok")


def test_failed_link_checks_hide_event_without_manual_check():
    ev = {**BASE, "link_failures": 3, "last_verified": iso(NOW - timedelta(hours=1))}
    ok, why = publish_decision(ev, NOW)
    assert not ok and "failed 3 checks" in why


def test_stale_manual_check_with_failures_is_hidden():
    ev = {**BASE, "link_failures": 3, "verified_manually": iso(NOW - timedelta(days=9))}
    assert publish_decision(ev, NOW)[0] is False


def test_tls_chain_error_is_inconclusive_not_a_failure():
    import requests
    from pipeline.fetch import Http

    class Boom(requests.Session):
        def request(self, *a, **k):
            raise requests.exceptions.SSLError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate")

    h = Http(delay=0, session=Boom())
    ok, why = h.check_link("https://example.org/x")
    assert ok is None and why.startswith("inconclusive")

    class Down(requests.Session):
        def request(self, *a, **k):
            raise requests.exceptions.ConnectionError("Name or service not known")

    ok, why = Http(delay=0, session=Down()).check_link("https://example.org/y")
    assert ok is False and "ConnectionError" in why


def test_event_without_pin_is_not_published():
    ev = {"deadline": "2099-01-01T23:59:00+08:00", "mode": "online", "verified_manually": iso(NOW)}
    ok, why = publish_decision(ev, NOW)
    assert not ok and "online only" in why


def test_classify_ctf_and_hackathon():
    from pipeline.extract import classify
    assert classify({"name": "Visayas Capture the Flag 2026"}) == "ctf"
    assert classify({"name": "HackForGov 5 Regional Finals"}) == "ctf"
    assert classify({"name": "Cebu Cybersecurity Competition"}) == "ctf"
    assert classify({"name": "Iloilo Civic Hackathon"}) == "hackathon"
    assert classify({"name": "Anything", "category": "ctf"}) == "ctf"
