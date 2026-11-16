"""D-012 F1: no dashboard response carries a profile's LinkedIn password.

``GET /api/profiles`` returns ``has_password: true|false`` in place of
``password``; the username stays. Every route under ``/api/profiles`` is
walked from Flask's own URL map, so a profile route added later is covered
without editing this file, and each response body is searched for the
password string itself.
"""

import pytest

from linkedin_automation import dashboard as linkedin_dashboard
from linkedin_automation import profile_manager as pm

SECRET = "Zq9+secretPW-7731xx"
OTHER_SECRET = "Kv2+otherPW-4410yy"


@pytest.fixture(autouse=True)
def _no_env_migration(monkeypatch):
    monkeypatch.setattr(pm, "auto_migrate_from_env", lambda: None)


@pytest.fixture
def seeded(api_client):
    """Two LinkedIn profiles with passwords and one publishing-only profile."""
    for name, user, pw in (("work", "w@example.com", SECRET),
                           ("side", "s@example.com", OTHER_SECRET)):
        resp = api_client.post("/api/profiles", json={
            "name": name, "username": user, "password": pw})
        assert resp.status_code == 200
    resp = api_client.post("/api/profiles", json={
        "name": "brand", "username": "", "password": ""})
    assert resp.status_code == 200
    # The secrets are really stored, so their absence below is not vacuous.
    assert pm.get_profile("work")["password"] == SECRET
    assert pm.get_profile("side")["password"] == OTHER_SECRET
    return api_client


def _assert_no_secret(resp):
    body = resp.get_data(as_text=True)
    assert SECRET not in body
    assert OTHER_SECRET not in body


def test_the_list_route_sends_has_password_not_the_password(seeded):
    resp = seeded.get("/api/profiles")
    assert resp.status_code == 200
    _assert_no_secret(resp)
    profiles = resp.get_json()["profiles"]
    for name in ("work", "side", "brand"):
        assert "password" not in profiles[name]
    assert profiles["work"]["has_password"] is True
    assert profiles["side"]["has_password"] is True
    assert profiles["brand"]["has_password"] is False


def test_the_list_route_keeps_the_username_and_default(seeded):
    seeded.post("/api/profiles/side/default")
    data = seeded.get("/api/profiles").get_json()
    assert data["profiles"]["work"]["username"] == "w@example.com"
    assert data["profiles"]["brand"]["username"] is None
    assert data["default"] == "side"


def _profile_rules():
    rules = []
    for rule in linkedin_dashboard.app.url_map.iter_rules():
        if rule.rule.startswith("/api/profiles"):
            for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
                rules.append((method, rule.rule))
    return sorted(rules)


def test_the_profile_route_enumeration_is_not_empty():
    """Guards the walk below against an empty parametrize set."""
    found = _profile_rules()
    assert ("GET", "/api/profiles") in found
    assert ("GET", "/api/profiles/<name>/config") in found
    assert len(found) >= 8


_BODIES = {
    "/api/profiles": {"name": "new", "username": "n@example.com",
                      "password": "fresh-pw"},
    "/api/profiles/<name>/config": {"max_comments_per_day": 5},
    "/api/profiles/<name>/automations": {"enabled": False},
}


@pytest.mark.parametrize("method,rule", _profile_rules())
@pytest.mark.parametrize("name", ["work", "brand"])
def test_no_profile_route_response_contains_a_password(seeded, method, rule, name):
    url = rule.replace("<name>", name)
    kwargs = {}
    if method in ("POST", "PUT", "PATCH"):
        kwargs["json"] = _BODIES.get(rule, {})
    resp = seeded.open(url, method=method, **kwargs)
    assert resp.status_code < 500, (method, url, resp.get_data(as_text=True))
    _assert_no_secret(resp)


def test_the_page_itself_contains_no_password(seeded):
    resp = seeded.get("/")
    assert resp.status_code == 200
    _assert_no_secret(resp)


def test_the_page_reads_has_password_not_password():
    """The profile dropdown's publishing-only label reads the new field."""
    import os
    path = os.path.join(os.path.dirname(linkedin_dashboard.__file__),
                        "templates", "dashboard.html")
    with open(path, encoding="utf-8") as f:
        html = f.read()
    assert "info.has_password" in html
    assert "info.password" not in html


def test_public_profile_handles_odd_records():
    assert linkedin_dashboard._public_profile(None) == {"has_password": False}
    assert linkedin_dashboard._public_profile({}) == {"has_password": False}
    assert linkedin_dashboard._public_profile(
        {"username": "u", "password": ""}) == {"username": "u", "has_password": False}
    assert linkedin_dashboard._public_profile(
        {"username": "u", "password": "x"}) == {"username": "u", "has_password": True}
