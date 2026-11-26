"""Roles: who may call what (app.web.auth)."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.web import auth
from app.web.auth import allowed, parse_names, required_role, role_for

ADMINS = parse_names("IT-Admins, alice@example.com")
OPERATORS = parse_names("it-operations")


def test_no_lists_means_everyone_is_admin():
    assert role_for({"x-forwarded-email": "anyone@example.com"}, frozenset(), frozenset()) == "admin"


def test_role_by_email_group_and_default():
    assert role_for({"x-forwarded-email": "Alice@Example.com"}, ADMINS, OPERATORS) == "admin"
    assert role_for({"x-forwarded-groups": "staff,it-admins"}, ADMINS, OPERATORS) == "admin"
    assert role_for({"x-forwarded-groups": "it-operations"}, ADMINS, OPERATORS) == "operator"
    assert role_for({"x-forwarded-email": "bob@example.com"}, ADMINS, OPERATORS) == "viewer"
    assert role_for({}, ADMINS, OPERATORS) == "viewer"


def test_headers_the_proxy_does_not_strip_are_ignored():
    # X-Auth-Request-* are response headers of oauth2-proxy: on a request they are
    # whatever the client sent.
    forged = {
        "x-forwarded-email": "bob@example.com",
        "x-auth-request-groups": "it-admins",
        "x-auth-request-email": "alice@example.com",
        "x-auth-request-user": "alice@example.com",
        "x-auth-request-preferred-username": "alice@example.com",
    }
    assert role_for(forged, ADMINS, OPERATORS) == "viewer"


def test_admin_wins_over_operator():
    headers = {"x-forwarded-email": "alice@example.com", "x-forwarded-groups": "it-operations"}
    assert role_for(headers, ADMINS, OPERATORS) == "admin"


@pytest.mark.parametrize(
    ("method", "path", "needed"),
    [
        ("GET", "/", "viewer"),
        ("GET", "/reports/archive/3/pdf", "viewer"),
        ("GET", "/settings", "admin"),
        ("GET", "/settings/restore-server/benchmark-status", "admin"),
        ("POST", "/clusters/main/sync", "operator"),
        ("POST", "/clusters/main/restore-tests", "operator"),
        ("POST", "/restore/vm/7/test", "operator"),
        ("POST", "/restore/runs/12/cancel", "operator"),
        ("POST", "/reports/generate", "operator"),
        ("POST", "/reports/archive/3/delete", "admin"),
        ("POST", "/clusters/main/delete", "admin"),
        ("POST", "/clusters/main/backup-policy", "admin"),
        ("POST", "/clusters/main/enrollment/renew", "admin"),
        ("POST", "/restore/vm/7/policy", "admin"),
        ("POST", "/settings/restore-server", "admin"),
        ("POST", "/api/clusters", "admin"),
        ("POST", "/some/route/added/later", "admin"),  # closed until listed
        ("DELETE", "/anything", "admin"),
        ("POST", "/api/bootstrap/finalize", None),
        ("POST", "/api/bootstrap/pbs-finalize", None),
    ],
)
def test_required_role(method, path, needed):
    assert required_role(method, path) == needed


def test_allowed_ranks():
    assert allowed("viewer", None) and allowed("viewer", "viewer") and allowed("admin", "operator")
    assert not allowed("viewer", "operator") and not allowed("operator", "admin")


def _client(monkeypatch) -> TestClient:
    monkeypatch.setattr(auth, "ADMINS", ADMINS)
    monkeypatch.setattr(auth, "OPERATORS", OPERATORS)
    app = FastAPI()
    app.add_middleware(auth.RoleMiddleware)

    @app.post("/reports/archive/{report_id}/delete")
    def delete(report_id: int) -> dict:
        return {"deleted": report_id}

    @app.post("/clusters/{slug}/sync")
    def sync(slug: str) -> dict:
        return {"sync": slug}

    @app.get("/")
    def home() -> dict:
        return {"ok": True}

    return TestClient(app)


def test_middleware_blocks_and_lets_through(monkeypatch):
    client = _client(monkeypatch)
    viewer = {"x-forwarded-email": "bob@example.com"}
    operator = {"x-forwarded-email": "carol@example.com", "x-forwarded-groups": "it-operations"}
    admin = {"x-forwarded-email": "alice@example.com"}

    assert client.get("/", headers=viewer).status_code == 200
    assert client.post("/clusters/main/sync", headers=viewer).status_code == 403
    assert client.post("/clusters/main/sync", headers=operator).status_code == 200
    assert client.post("/reports/archive/1/delete", headers=operator).status_code == 403
    assert client.post("/reports/archive/1/delete", headers=admin).status_code == 200


def _bare_client(monkeypatch, allow_anonymous: bool) -> TestClient:
    monkeypatch.setattr(auth, "ADMINS", frozenset())
    monkeypatch.setattr(auth, "OPERATORS", frozenset())
    monkeypatch.setattr(auth, "ALLOW_ANONYMOUS", allow_anonymous)
    app = FastAPI()
    app.add_middleware(auth.RoleMiddleware)

    for path in ("/", "/settings", "/healthz", "/metrics", "/bootstrap/pbs-agent.sh", "/api/thing"):
        app.get(path)(lambda: {"ok": True})
    app.post("/api/bootstrap/finalize")(lambda: {"ok": True})
    app.post("/clusters/{slug}/delete")(lambda slug: {"ok": True})
    return TestClient(app)


def test_request_without_a_signed_in_user_is_refused(monkeypatch):
    """Roles off means every signed-in user is admin — not everyone who reaches the port."""
    client = _bare_client(monkeypatch, allow_anonymous=False)
    assert client.get("/").status_code == 401
    assert client.get("/settings").status_code == 401
    assert client.post("/clusters/main/delete").status_code == 401
    assert client.get("/api/thing").json()["detail"].startswith("Not signed in")
    # Groups alone are not a user, and the headers the proxy does not strip are not read.
    assert client.get("/", headers={"x-forwarded-groups": "it-admins"}).status_code == 401
    assert client.get("/", headers={"x-auth-request-email": "alice@example.com"}).status_code == 401
    assert client.get("/", headers={"x-forwarded-user": "alice"}).status_code == 200
    assert client.post("/clusters/main/delete", headers={"x-forwarded-email": "a@example.com"}).status_code == 200


def test_monitoring_and_bootstrap_need_no_user(monkeypatch):
    client = _bare_client(monkeypatch, allow_anonymous=False)
    for path in ("/healthz", "/metrics", "/bootstrap/pbs-agent.sh"):
        assert client.get(path).status_code == 200
    assert client.post("/api/bootstrap/finalize").status_code == 200


def test_local_development_can_run_without_a_proxy(monkeypatch):
    client = _bare_client(monkeypatch, allow_anonymous=True)
    assert client.get("/settings").status_code == 200
