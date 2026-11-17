"""With ALLOW_SIGNUP, anyone may sign up; a new account is always a plain user, never admin
or curator. Sign-up is off by default."""

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.testclient import TestClient

from sparql_llm.agent import auth, main
from sparql_llm.config import Settings, settings

PASSWORD = "pw-" + "x" * 20


@pytest.fixture
def site(tmp_path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "users.db"
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "auth_db_path", str(db_path))
    monkeypatch.setattr(settings, "projects", [])
    monkeypatch.setattr(settings, "admin_email", "admin@example.org")
    monkeypatch.setattr(settings, "admin_password", PASSWORD)
    monkeypatch.setattr(settings, "cookie_secure", False)
    monkeypatch.setattr(settings, "allow_signup", True)

    # The auth engine binds to the install's database file at import: point it at this one.
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path.as_posix()}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for module in (auth, main):
        monkeypatch.setattr(module, "engine", engine)
        monkeypatch.setattr(module, "async_session_maker", maker)

    def boom(*args, **kwargs):
        raise AssertionError("network call")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", boom)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", boom)

    with TestClient(main.app) as c:
        c.maker = maker
        yield c


def _user(site, email: str):
    async def get():
        async with site.maker() as session:
            return (await session.execute(select(auth.User).where(auth.User.email == email))).scalar_one_or_none()

    return site.portal.call(get)


def _register(site, email="new@example.org", password=PASSWORD, confirm=None, **extra):
    data = {"email": email, "password": password, "confirm_password": confirm or password, "next": "/", **extra}
    return site.post("/register", data=data, follow_redirects=False)


def test_login_page_links_to_registration(site):
    assert 'href="/register' in site.get("/login").text
    assert site.get("/register").status_code == 200


def test_signup_is_off_by_default():
    assert Settings.model_fields["allow_signup"].default is False


def test_with_signup_off_there_is_no_link_and_no_registration(site, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "allow_signup", False)
    assert 'href="/register' not in site.get("/login").text
    assert site.get("/register").status_code == 404
    assert _register(site).status_code == 404
    assert _user(site, "new@example.org") is None


def test_register_creates_a_plain_user_and_signs_in(site):
    resp = _register(site)
    assert resp.status_code == 302
    assert auth.cookie_transport.cookie_name in resp.cookies
    user = _user(site, "new@example.org")
    assert user is not None and user.is_active
    assert not user.is_superuser and not user.is_curator
    # Signed in, but no admin page for a plain user.
    assert site.get("/", follow_redirects=False).status_code == 200
    assert site.get("/admin", follow_redirects=False).headers.get("location") == "/"


def test_role_flags_in_the_form_are_ignored(site):
    _register(site, email="sneaky@example.org", is_superuser="true", is_curator="true", role="admin")
    user = _user(site, "sneaky@example.org")
    assert not user.is_superuser and not user.is_curator


def test_duplicate_email_is_refused(site):
    assert _register(site, email="admin@example.org").status_code == 409


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"confirm": "something-else-entirely"}, "do not match"),
        ({"password": "short", "confirm": "short"}, "at least 8"),
        ({"email": "not-an-email"}, "valid email"),
    ],
)
def test_bad_input_is_refused(site, kwargs, message):
    resp = _register(site, **kwargs)
    assert resp.status_code == 400
    assert message in resp.text
    assert _user(site, kwargs.get("email", "new@example.org")) is None
