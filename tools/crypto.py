"""Symmetric encryption helpers for secrets stored at rest (e.g. user OAuth tokens).

The Fernet key (``[AniList] fernetKey`` in ``config/tokens.ini``) lives OUTSIDE the
database, so a database dump alone cannot decrypt anything. Fernet provides
authenticated encryption (AES-128-CBC + HMAC), so tampered ciphertext is rejected.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from tools.config_loader import config_loader

_fernet: Fernet | None = None
_loaded = False


def _get_fernet() -> Fernet | None:
    """Build (and cache) the Fernet instance from the configured key, or None."""
    global _fernet, _loaded
    if not _loaded:
        _loaded = True
        try:
            key = config_loader.get("AniList", "fernetKey").strip()
        except Exception:
            key = ""
        _fernet = Fernet(key.encode()) if key else None
    return _fernet


def is_configured() -> bool:
    """True when a usable encryption key is configured."""
    return _get_fernet() is not None


def encrypt(plaintext: str) -> str:
    """Encrypt a string and return URL-safe base64 ciphertext."""
    fernet = _get_fernet()
    if fernet is None:
        raise RuntimeError("No encryption key configured ([AniList] fernetKey in tokens.ini).")
    return fernet.encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str | None:
    """Decrypt Fernet ciphertext. Returns None if the key is missing or the token is invalid/tampered."""
    fernet = _get_fernet()
    if fernet is None:
        return None
    try:
        return fernet.decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        return None
