"""Only this site (and a local dev server) acts with the login cookie; production cookies are Secure.

SameSite=Lax stops ordinary cross-site posts, but every *.unibe.ch host is the same site:
CORS and the Origin check are what keep a sibling page from acting as a logged-in user.
"""

import pytest
from starlette.testclient import TestClient

from sparql_llm.agent import auth, main
from sparql_llm.config import SettingsError, settings

SIBLING = "https://sibling.dsl.unibe.ch"


@pytest.fixture
def client():
    with TestClient(main.app, base_url="https://swiss-elites.dsl.unibe.ch") as c:
        yield c


def test_cors_does_not_hand_credentials_to_a_sibling_site(client: TestClient):
    resp = client.options(
        "/chat", headers={"Origin": SIBLING, "Access-Control-Request-Method": "POST"}
    )
    assert resp.headers.get("access-control-allow-origin") != SIBLING


def test_cors_still_serves_a_local_dev_page(client: TestClient):
    origin = "http://localhost:5173"
    resp = client.options("/chat", headers={"Origin": origin, "Access-Control-Request-Method": "POST"})
    assert resp.headers.get("access-control-allow-origin") == origin


def test_a_cookie_write_from_a_sibling_site_is_refused(client: TestClient):
    client.cookies.set(auth.cookie_transport.cookie_name, "any-token")
    resp = client.post("/logout", headers={"Origin": SIBLING})
    assert resp.status_code == 403


def test_a_cookie_write_from_this_site_goes_through(client: TestClient):
    client.cookies.set(auth.cookie_transport.cookie_name, "any-token")
    resp = client.post("/logout", headers={"Origin": "https://swiss-elites.dsl.unibe.ch"})
    assert resp.status_code != 403


def test_the_trusted_public_name_counts_behind_another_host(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "app_public_host", "swiss-elites.dsl.unibe.ch")
    assert main.trusted_origin("https://swiss-elites.dsl.unibe.ch", "127.0.0.1:8000")
    assert not main.trusted_origin("http://swiss-elites.dsl.unibe.ch", "127.0.0.1:8000")
    assert not main.trusted_origin(SIBLING, "127.0.0.1:8000")
    assert not main.trusted_origin("null", "127.0.0.1:8000")


def test_cookies_are_secure_when_served_under_a_public_name(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "cookie_secure", None)
    monkeypatch.setattr(settings, "app_public_host", "swiss-elites.dsl.unibe.ch")
    assert auth.cookie_secure() is True
    monkeypatch.setattr(settings, "app_public_host", "")
    assert auth.cookie_secure() is False
    monkeypatch.setattr(settings, "cookie_secure", True)
    assert auth.cookie_secure() is True


@pytest.mark.parametrize("secret", ["CHANGE_ME_IN_PRODUCTION_USE_A_LONG_RANDOM_STRING", "short"])
def test_auth_refuses_a_placeholder_or_short_secret(monkeypatch: pytest.MonkeyPatch, secret: str):
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "auth_secret", secret)
    with pytest.raises(SettingsError):
        auth.check_auth_secret()


def test_auth_accepts_a_long_secret_and_ignores_it_without_auth(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "auth_secret", "x" * 64)
    auth.check_auth_secret()
    monkeypatch.setattr(settings, "auth_enabled", False)
    monkeypatch.setattr(settings, "auth_secret", "short")
    auth.check_auth_secret()
