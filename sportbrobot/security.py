"""Password hashing (stdlib scrypt), Fernet encryption, MCP token helpers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from cryptography.fernet import Fernet, InvalidToken

from .config import get_settings

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1

MCP_TOKEN_PREFIX = "sbb_"


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P
    )
    return "scrypt${}${}${}${}${}".format(
        _SCRYPT_N,
        _SCRYPT_R,
        _SCRYPT_P,
        base64.b64encode(salt).decode(),
        base64.b64encode(digest).decode(),
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        digest = hashlib.scrypt(
            password.encode(), salt=salt, n=int(n), r=int(r), p=int(p)
        )
        return hmac.compare_digest(digest, expected)
    except (ValueError, TypeError):
        return False


def _fernet() -> Fernet:
    return Fernet(get_settings().fernet_key.encode())


def encrypt_text(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_text(ciphertext: str) -> str:
    """Decrypt; raises cryptography.fernet.InvalidToken on tampered/wrong-key data."""
    return _fernet().decrypt(ciphertext.encode()).decode()


def generate_mcp_token() -> str:
    return MCP_TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_mcp_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


__all__ = [
    "hash_password",
    "verify_password",
    "encrypt_text",
    "decrypt_text",
    "generate_mcp_token",
    "hash_mcp_token",
    "InvalidToken",
    "MCP_TOKEN_PREFIX",
]
