from datetime import datetime, timedelta, timezone

from pipeline.store import iso, publish_decision

NOW = datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)
BASE = {"deadline": "2099-01-01T23:59:00+08:00", "mode": "online"}


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
