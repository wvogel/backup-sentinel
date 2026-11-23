"""Stored secrets: layers, keys, and never handing out ciphertext."""

import pytest
from cryptography.fernet import Fernet

from app.services import crypto

KEY_A = Fernet.generate_key().decode()
KEY_B = Fernet.generate_key().decode()


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    monkeypatch.setattr(crypto, "SECRET_KEY", KEY_A)
    monkeypatch.setattr(crypto, "SECRET_KEYS_OLD", ())


def _seal(key: str, value: str) -> str:
    return Fernet(key.encode()).encrypt(value.encode()).decode()


def test_round_trip_is_one_proper_layer():
    token = crypto.encrypt("secret")
    assert crypto.looks_encrypted(token)
    assert crypto.unwrap(token) == ("secret", True)
    assert crypto.decrypt(token) == "secret"


def test_plaintext_from_before_encryption_passes_through_but_is_not_proper():
    assert crypto.unwrap("0f8e7c1a-plain-token") == ("0f8e7c1a-plain-token", False)
    assert crypto.decrypt("0f8e7c1a-plain-token") == "0f8e7c1a-plain-token"
    assert crypto.decrypt("") == ""


def test_a_token_of_an_unknown_key_is_not_plaintext():
    foreign = _seal(KEY_B, "secret")
    assert crypto.looks_encrypted(foreign)
    with pytest.raises(crypto.UndecryptableError):
        crypto.unwrap(foreign)
    assert crypto.decrypt(foreign) == ""  # never the ciphertext itself


def test_previous_key_opens_and_marks_for_rewriting(monkeypatch):
    monkeypatch.setattr(crypto, "SECRET_KEYS_OLD", (KEY_B,))
    assert crypto.unwrap(_seal(KEY_B, "secret")) == ("secret", False)
    assert crypto.unwrap(_seal(KEY_A, "secret")) == ("secret", True)


def test_several_layers_of_mixed_keys_open_completely(monkeypatch):
    monkeypatch.setattr(crypto, "SECRET_KEYS_OLD", (KEY_B,))
    wrapped = _seal(KEY_A, _seal(KEY_B, _seal(KEY_A, "secret")))
    assert crypto.unwrap(wrapped) == ("secret", False)
    assert crypto.unwrap(_seal(KEY_A, _seal(KEY_A, "secret"))) == ("secret", False)


def test_a_layer_in_the_middle_without_its_key_stops_everything():
    with pytest.raises(crypto.UndecryptableError):
        crypto.unwrap(_seal(KEY_A, _seal(KEY_B, "secret")))


def test_an_invalid_old_key_is_ignored(monkeypatch):
    monkeypatch.setattr(crypto, "SECRET_KEYS_OLD", ("not-a-key",))
    assert crypto.decrypt(crypto.encrypt("secret")) == "secret"


def test_digest_per_key(monkeypatch):
    one = crypto.keyed_digests("p", "m")
    monkeypatch.setattr(crypto, "SECRET_KEYS_OLD", (KEY_B,))
    two = crypto.keyed_digests("p", "m")
    assert len(one) == 1 and len(two) == 2 and two[0] == one[0] != two[1]
    assert crypto.keyed_digest("p", "m") == one[0]
