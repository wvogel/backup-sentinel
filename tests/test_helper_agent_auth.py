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
