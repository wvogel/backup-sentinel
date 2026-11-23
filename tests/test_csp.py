"""The Content-Security-Policy forbids inline scripts and styles — and the pages
must not need them: no inline script, handler, style block or style attribute."""

import re
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from app.main import SecurityHeadersMiddleware
from app.web.common import PREVIEW_CSP

ROOT = Path(__file__).resolve().parent.parent
# Pages the browser gets under the app's policy. Mail and PDF templates are
# made for mail clients and the PDF renderer and are never served under it.
PAGES = sorted((ROOT / "templates").glob("*.html"))


def _client() -> TestClient:
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/")
    def page() -> HTMLResponse:
        return HTMLResponse("<p>page</p>")

    @app.get("/preview")
    def preview() -> HTMLResponse:
        return HTMLResponse("<p style='margin:0'>preview</p>", headers={"Content-Security-Policy": PREVIEW_CSP})

    return TestClient(app)


def _directive(policy: str, name: str) -> str:
    for part in policy.split(";"):
        words = part.split()
        if words and words[0] == name:
            return " ".join(words[1:])
    return ""


def test_policy_allows_only_the_apps_own_scripts_and_styles():
    policy = _client().get("/").headers["content-security-policy"]
    assert "unsafe-inline" not in policy and "unsafe-eval" not in policy
    assert _directive(policy, "script-src") == "'self'"
    assert _directive(policy, "style-src") == "'self'"
    assert _directive(policy, "default-src") == "'self'"


def test_previews_keep_their_own_policy_without_any_script():
    policy = _client().get("/preview").headers["content-security-policy"]
    assert policy == PREVIEW_CSP
    assert _directive(policy, "default-src") == "'none'"  # so no script-src: scripts are forbidden
    assert "script-src" not in policy


def test_there_are_pages_to_check():
    assert len(PAGES) > 10


def test_pages_have_no_inline_script():
    for page in PAGES:
        for tag in re.findall(r"<script\b[^>]*>", page.read_text()):
            assert "src=" in tag or 'type="application/json"' in tag, f"{page.name}: {tag}"


def test_pages_have_no_inline_handler():
    for page in PAGES:
        found = re.findall(r"<[^>]*\son[a-z]+\s*=\s*[\"']", page.read_text())
        assert not found, f"{page.name}: {found[0][:120]}"


def test_pages_have_no_inline_style():
    for page in PAGES:
        text = page.read_text()
        assert "<style" not in text, page.name
        found = re.findall(r"<[^>]*\sstyle\s*=", text)
        assert not found, f"{page.name}: {found[0][:120]}"


def test_scripts_write_no_style_attribute_or_handler_into_html():
    for script in sorted((ROOT / "static" / "js").glob("*.js")):
        text = script.read_text()
        assert not re.search(r"\sstyle\s*=\s*\\?[\"']", text), script.name
        assert not re.search(r"\son(click|change|input|submit)\s*=\s*\\?[\"']", text), script.name
