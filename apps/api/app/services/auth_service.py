"""Password hashing, API key generation/verification, and JWT issuance.

Two distinct hashing strategies are used deliberately:

- **Passwords** are hashed with Argon2 (via ``argon2-cffi``, used directly
  rather than through ``passlib`` — ``passlib`` is effectively unmaintained
  and its bcrypt backend has a well-documented compatibility break with
  recent bcrypt releases; using the Argon2 reference library directly
  avoids that whole class of problem). Argon2's per-hash random salt means
  two calls with the same password never produce the same hash, which is
  fine for passwords: they're only ever verified against the one row for
  the email the caller supplied.
- **API keys and refresh tokens** are hashed with HMAC-SHA256 keyed by
  ``settings.api_key_pepper``. Unlike a password, an API key or refresh
  token must be looked up in the database by the raw secret alone (the
  caller doesn't also supply a username to narrow the search), so the hash
  has to be deterministic to support an indexed equality lookup on
  ``hashed_key`` / ``hashed_token``. HMAC-SHA256 gives that determinism
  while still requiring the server-side pepper to invert, so a stolen
  database dump alone is not enough to forge or reverse a key.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from app.core.config import settings

API_KEY_PREFIX = "nxs_live_"
_API_KEY_SECRET_BYTES = 32
_REFRESH_TOKEN_BYTES = 32

_password_hasher = PasswordHasher()


class InvalidTokenError(Exception):
    """Raised when a JWT is missing, malformed, expired, or signed with the wrong key."""


# --- Passwords ---------------------------------------------------------


def hash_password(password: str) -> str:
    """Hash a plaintext password with Argon2."""
    return _password_hasher.hash(password)


def verify_password(password: str, hashed_password: str) -> bool:
    """Check a plaintext password against an Argon2 hash. Never raises."""
    try:
        return _password_hasher.verify(hashed_password, password)
    except VerifyMismatchError:
        return False
    except Exception:  # noqa: BLE001 - any malformed-hash error is a verification failure
        return False


# --- Deterministic (HMAC) hashing for API keys and refresh tokens ------


def _hmac_hash(secret: str) -> str:
    """Deterministic, keyed hash used for values that must be looked up by
    exact match (API keys, refresh tokens) rather than verified against one
    known row the way a password is.
    """
    return hmac.new(
        settings.api_key_pepper.encode("utf-8"),
        secret.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


@dataclass(frozen=True)
class GeneratedApiKey:
    raw_key: str
    prefix: str
    hashed_key: str


def generate_api_key() -> GeneratedApiKey:
    """Generate a new API key. The raw key is only ever available here, at
    creation time; only its prefix (for display) and hash (for lookup) are
    meant to be persisted.
    """
    secret = secrets.token_urlsafe(_API_KEY_SECRET_BYTES)
    raw_key = f"{API_KEY_PREFIX}{secret}"
    display_prefix = raw_key[: len(API_KEY_PREFIX) + 4]
    return GeneratedApiKey(
        raw_key=raw_key, prefix=display_prefix, hashed_key=_hmac_hash(raw_key)
    )


def hash_api_key(raw_key: str) -> str:
    """Hash a presented raw API key for a lookup-by-hash query."""
    return _hmac_hash(raw_key)


@dataclass(frozen=True)
class GeneratedRefreshToken:
    raw_token: str
    hashed_token: str


def generate_refresh_token() -> GeneratedRefreshToken:
    """Generate a new opaque refresh token secret (not a JWT)."""
    raw_token = secrets.token_urlsafe(_REFRESH_TOKEN_BYTES)
    return GeneratedRefreshToken(raw_token=raw_token, hashed_token=_hmac_hash(raw_token))


def hash_refresh_token(raw_token: str) -> str:
    """Hash a presented raw refresh token for a lookup-by-hash query."""
    return _hmac_hash(raw_token)


def refresh_token_expiry() -> datetime:
    return datetime.now(UTC) + timedelta(days=settings.refresh_token_expire_days)


# --- JWT access tokens ---------------------------------------------------

TokenType = Literal["access"]


@dataclass(frozen=True)
class AccessTokenPayload:
    user_id: uuid.UUID
    organization_id: uuid.UUID


def create_access_token(*, user_id: uuid.UUID, organization_id: uuid.UUID) -> str:
    """Issue a short-lived signed access token for a session-authenticated user."""
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "org_id": str(organization_id),
        "type": "access",
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_expire_minutes),
        # A random per-issuance id. Not used for revocation (access tokens
        # are intentionally stateless and short-lived), but it guarantees
        # two tokens issued for the same user in the same second still
        # differ, which matters e.g. right after a refresh-token rotation.
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> AccessTokenPayload:
    """Verify and decode an access token. Raises InvalidTokenError for any
    invalid, expired, tampered, or wrong-type token.
    """
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError as exc:
        raise InvalidTokenError(str(exc)) from exc

    if payload.get("type") != "access":
        raise InvalidTokenError("token is not an access token")

    try:
        user_id = uuid.UUID(payload["sub"])
        organization_id = uuid.UUID(payload["org_id"])
    except (KeyError, ValueError) as exc:
        raise InvalidTokenError("token payload missing or malformed claims") from exc

    return AccessTokenPayload(user_id=user_id, organization_id=organization_id)
