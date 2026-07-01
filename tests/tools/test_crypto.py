"""Tests for ``tools/crypto.py`` - the Fernet at-rest encryption helpers.

Two scenarios are covered:

* configured (via the shared ``crypto_key`` fixture, which installs a fresh
  valid Fernet key on the module cache): ``encrypt``/``decrypt`` round-trip,
  tampered ciphertext decrypts to ``None``, and ``is_configured()`` is True.
* not configured (globals reset and the config key forced empty): the module
  reloads from config, finds no key, so ``is_configured()`` is False,
  ``decrypt`` returns ``None`` and ``encrypt`` raises ``RuntimeError``.

The module keeps a process-wide cache (``_fernet``/``_loaded``). A local
autouse fixture snapshots and restores those globals around every test so
state never leaks into other test files. The dev-box ``tokens.ini`` ships a
real key, so the "no key" path monkeypatches the config read to stay
deterministic rather than depending on config contents.
"""

import pytest
from cryptography.fernet import Fernet

from tools import crypto


@pytest.fixture(autouse=True)
def _restore_crypto_globals():
    """Snapshot and restore ``tools.crypto`` cache globals around each test."""
    saved_fernet = crypto._fernet
    saved_loaded = crypto._loaded
    try:
        yield
    finally:
        crypto._fernet = saved_fernet
        crypto._loaded = saved_loaded


@pytest.fixture
def no_key(monkeypatch):
    """Force ``tools.crypto`` into the 'no key configured' state, deterministically.

    Patches the config read to return an empty key and resets the cache so the
    next ``_get_fernet()`` genuinely reloads and resolves to ``None`` regardless
    of what the local ``tokens.ini`` actually contains.
    """
    monkeypatch.setattr(
        crypto.config_loader, "get", lambda section, option: "", raising=True
    )
    crypto._fernet = None
    crypto._loaded = False
    yield


# ---------------------------------------------------------------------------
# Configured: crypto_key fixture installs a valid Fernet key.
# ---------------------------------------------------------------------------


def test_is_configured_true(crypto_key):
    assert crypto.is_configured() is True


def test_encrypt_decrypt_round_trip(crypto_key):
    plaintext = "super-secret-oauth-token-\\o/-123"
    ciphertext = crypto.encrypt(plaintext)

    # Ciphertext is a distinct, non-empty string (not the plaintext).
    assert isinstance(ciphertext, str)
    assert ciphertext != plaintext
    assert ciphertext

    assert crypto.decrypt(ciphertext) == plaintext


def test_encrypt_uses_yielded_key(crypto_key):
    """The fixture yields the raw key backing the module's Fernet instance."""
    plaintext = "interop-check"
    ciphertext = crypto.encrypt(plaintext)

    # A Fernet built from the yielded key decrypts what the module encrypted.
    external = Fernet(crypto_key)
    assert external.decrypt(ciphertext.encode()).decode() == plaintext


def test_encrypt_is_nondeterministic_but_round_trips(crypto_key):
    """Fernet embeds a random IV, so two encryptions differ yet both decrypt back."""
    plaintext = "same-input"
    first = crypto.encrypt(plaintext)
    second = crypto.encrypt(plaintext)

    assert first != second
    assert crypto.decrypt(first) == plaintext
    assert crypto.decrypt(second) == plaintext


def test_decrypt_tampered_ciphertext_returns_none(crypto_key):
    """A modified token fails Fernet's HMAC check -> InvalidToken -> None."""
    ciphertext = crypto.encrypt("tamper-me")

    mid = len(ciphertext) // 2
    original = ciphertext[mid]
    replacement = "A" if original != "A" else "B"
    tampered = ciphertext[:mid] + replacement + ciphertext[mid + 1:]

    assert tampered != ciphertext
    assert crypto.decrypt(tampered) is None


def test_decrypt_garbage_returns_none(crypto_key):
    """Non-base64 / non-token junk decrypts to None rather than raising."""
    assert crypto.decrypt("this-is-not-a-valid-fernet-token") is None


# ---------------------------------------------------------------------------
# Not configured: globals reset, config key forced empty.
# ---------------------------------------------------------------------------


def test_is_configured_false_when_no_key(no_key):
    assert crypto.is_configured() is False


def test_decrypt_returns_none_when_no_key(no_key):
    assert crypto.decrypt("anything-at-all") is None


def test_encrypt_raises_runtime_error_when_no_key(no_key):
    with pytest.raises(RuntimeError):
        crypto.encrypt("nope")


def test_no_key_when_config_read_raises(monkeypatch):
    """If the config lookup raises, the code swallows it and resolves to no key."""

    def _boom(section, option):
        raise KeyError("AniList")

    monkeypatch.setattr(crypto.config_loader, "get", _boom, raising=True)
    crypto._fernet = None
    crypto._loaded = False

    assert crypto.is_configured() is False
    assert crypto.decrypt("x") is None
    with pytest.raises(RuntimeError):
        crypto.encrypt("x")
