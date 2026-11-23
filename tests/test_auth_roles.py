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
    operator = {"x-forwarded-groups": "it-operations"}
    admin = {"x-forwarded-email": "alice@example.com"}

    assert client.get("/", headers=viewer).status_code == 200
    assert client.post("/clusters/main/sync", headers=viewer).status_code == 403
    assert client.post("/clusters/main/sync", headers=operator).status_code == 200
    assert client.post("/reports/archive/1/delete", headers=operator).status_code == 403
    assert client.post("/reports/archive/1/delete", headers=admin).status_code == 200
