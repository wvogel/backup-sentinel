"""The restore helper agent: bearer token and a config it builds itself."""

import pytest

from app.services import restore_helper_agent as agent
from app.services.restore_server import agent_token

TOKEN = "a" * 64


@pytest.fixture(autouse=True)
def token_file(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "TOKEN_FILE", str(tmp_path / "token"))


def test_first_token_claims_then_only_that_one_works():
    assert agent.authorized(f"Bearer {TOKEN}")
    assert agent.authorized(f"Bearer {TOKEN}")
    assert not agent.authorized("Bearer " + "b" * 64)


def test_missing_or_short_token_is_refused_and_claims_nothing():
    assert not agent.authorized(None)
    assert not agent.authorized("")
    assert not agent.authorized("Bearer short")
    assert not agent.authorized(TOKEN)  # no "Bearer " prefix
    assert agent.authorized(f"Bearer {TOKEN}")  # still unclaimed


def test_config_is_built_from_addresses_only():
    conf = agent.dnsmasq_config("10.0.0.200", "255.255.255.0")
    assert "dhcp-range=10.0.0.200,10.0.0.200,255.255.255.0,infinite" in conf
    assert "interface=sbx" in conf


@pytest.mark.parametrize(
    "guest_ip",
    ["10.0.0.200\ndhcp-script=/bin/sh", "10.0.0.200,x", "", "conf-file=/etc/passwd", "::1"],
)
def test_config_refuses_anything_but_an_ipv4_address(guest_ip):
    with pytest.raises(ValueError):
        agent.dnsmasq_config(guest_ip, "255.255.255.0")
    with pytest.raises(ValueError):
        agent.dnsmasq_config("10.0.0.200", guest_ip)


def test_app_token_is_stable_per_helper_and_long_enough():
    assert agent_token("10.9.0.10") == agent_token("10.9.0.10")
    assert agent_token("10.9.0.10") != agent_token("10.9.0.11")
    assert len(agent_token("10.9.0.10")) >= 32


def test_app_token_differs_from_run_to_run():
    """The management addresses are reused; a token read off the wire is not."""
    assert agent_token("10.9.0.10", "run-7") == agent_token("10.9.0.10", "run-7")
    assert agent_token("10.9.0.10", "run-7") != agent_token("10.9.0.10", "run-8")
    assert agent_token("10.9.0.10", "run-7") != agent_token("10.9.0.10")
    assert agent_token("10.9.0.10", "") == agent_token("10.9.0.10")


class _AgentCalls:
    """Stands in for urlopen: records the tokens, refuses the ones not `claimed`."""

    def __init__(self, claimed: str) -> None:
        self.claimed, self.tokens = claimed, []

    def __call__(self, req, timeout=None):
        import io
        import urllib.error

        token = req.get_header("Authorization").removeprefix("Bearer ")
        self.tokens.append(token)
        if token != self.claimed:
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", None, io.BytesIO(b"{}"))
        return io.BytesIO(b'{"ok": true}')


def _server(scope: str):
    from app.services.restore_server import RestoreServer

    server = RestoreServer({}, pve=None, ssh=None)
    server.agent_scope = scope
    return server


def test_agent_is_called_with_the_token_of_its_run(monkeypatch):
    import urllib.request

    calls = _AgentCalls(agent_token("10.9.0.10", "run-7"))
    monkeypatch.setattr(urllib.request, "urlopen", calls)
    assert _server("run-7")._agent_post("10.9.0.10", "/probe", {}) == {"ok": True}
    assert calls.tokens == [agent_token("10.9.0.10", "run-7")]


def test_helper_from_before_the_update_still_answers(monkeypatch):
    import urllib.request

    calls = _AgentCalls(agent_token("10.9.0.10"))  # claimed with the address alone
    monkeypatch.setattr(urllib.request, "urlopen", calls)
    assert _server("run-7")._agent_post("10.9.0.10", "/probe", {}) == {"ok": True}
    assert calls.tokens == [agent_token("10.9.0.10", "run-7"), agent_token("10.9.0.10")]


def test_wrong_token_is_an_error_not_a_retry_loop(monkeypatch):
    import urllib.request

    from app.services.restore_server import RestoreTaskError

    calls = _AgentCalls("someone-else")
    monkeypatch.setattr(urllib.request, "urlopen", calls)
    with pytest.raises(RestoreTaskError) as exc:
        _server("run-7")._agent_post("10.9.0.10", "/probe", {})
    assert "HTTP 401" in str(exc.value) and len(calls.tokens) == 2


def test_worker_scopes_the_agent_to_its_run():
    from types import SimpleNamespace

    from app.services import restore_worker

    server = SimpleNamespace()
    restore_worker._scope_agent(server, 42)
    assert server.agent_scope == "run-42"
