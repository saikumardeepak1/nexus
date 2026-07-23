"""Unit tests for app.services.auth_service: password hashing, API key
generation/hashing, refresh token generation/hashing, and JWT access token
issuance/verification. No database needed.
"""

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.core.config import settings
from app.services import auth_service


def test_hash_password_then_verify_succeeds() -> None:
    hashed = auth_service.hash_password("correct horse battery staple")
    assert auth_service.verify_password("correct horse battery staple", hashed) is True


def test_verify_password_rejects_wrong_password() -> None:
    hashed = auth_service.hash_password("correct horse battery staple")
    assert auth_service.verify_password("wrong password", hashed) is False


def test_verify_password_never_raises_on_malformed_hash() -> None:
    # A corrupted or non-Argon2 value in hashed_password (e.g. a DB row
    # written by something else) must fail verification, not crash.
    assert auth_service.verify_password("anything", "not-a-real-argon2-hash") is False


def test_hash_password_is_not_the_plaintext_and_is_salted_per_call() -> None:
    first = auth_service.hash_password("same-password")
    second = auth_service.hash_password("same-password")
    assert first != "same-password"
    # Argon2 embeds a random salt per hash, so two hashes of the same
    # password never collide even though both verify successfully.
    assert first != second
    assert auth_service.verify_password("same-password", first) is True
    assert auth_service.verify_password("same-password", second) is True


def test_generate_api_key_has_nxs_live_prefix() -> None:
    generated = auth_service.generate_api_key()
    assert generated.raw_key.startswith("nxs_live_")
    assert generated.prefix.startswith("nxs_live_")
    assert generated.raw_key.startswith(generated.prefix)
    assert generated.hashed_key != generated.raw_key


def test_hash_api_key_is_deterministic_and_matches_generation() -> None:
    generated = auth_service.generate_api_key()
    # A presented raw key must hash to exactly what was stored at creation,
    # since lookup is by hash equality.
    assert auth_service.hash_api_key(generated.raw_key) == generated.hashed_key


def test_hash_api_key_different_keys_hash_differently() -> None:
    first = auth_service.generate_api_key()
    second = auth_service.generate_api_key()
    assert first.hashed_key != second.hashed_key


def test_generate_refresh_token_hash_matches_lookup_hash() -> None:
    generated = auth_service.generate_refresh_token()
    assert auth_service.hash_refresh_token(generated.raw_token) == generated.hashed_token


def test_create_and_decode_access_token_round_trips() -> None:
    user_id = uuid.uuid4()
    org_id = uuid.uuid4()
    token = auth_service.create_access_token(user_id=user_id, organization_id=org_id)

    payload = auth_service.decode_access_token(token)
    assert payload.user_id == user_id
    assert payload.organization_id == org_id


def test_decode_access_token_rejects_tampered_signature() -> None:
    token = auth_service.create_access_token(user_id=uuid.uuid4(), organization_id=uuid.uuid4())
    last_char = token[-1]
    flipped_char = "x" if last_char != "x" else "y"
    tampered = token[:-1] + flipped_char

    with pytest.raises(auth_service.InvalidTokenError):
        auth_service.decode_access_token(tampered)


def test_decode_access_token_rejects_wrong_secret() -> None:
    forged = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "org_id": str(uuid.uuid4()),
            "type": "access",
            "iat": datetime.now(UTC),
            "exp": datetime.now(UTC) + timedelta(minutes=5),
        },
        "not-the-real-secret",
        algorithm=settings.jwt_algorithm,
    )

    with pytest.raises(auth_service.InvalidTokenError):
        auth_service.decode_access_token(forged)


def test_decode_access_token_rejects_expired_token() -> None:
    expired = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "org_id": str(uuid.uuid4()),
            "type": "access",
            "iat": datetime.now(UTC) - timedelta(minutes=30),
            "exp": datetime.now(UTC) - timedelta(minutes=1),
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )

    with pytest.raises(auth_service.InvalidTokenError):
        auth_service.decode_access_token(expired)


def test_decode_access_token_rejects_non_access_token_type() -> None:
    refresh_shaped = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "org_id": str(uuid.uuid4()),
            "type": "refresh",
            "iat": datetime.now(UTC),
            "exp": datetime.now(UTC) + timedelta(minutes=5),
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )

    with pytest.raises(auth_service.InvalidTokenError):
        auth_service.decode_access_token(refresh_shaped)


def test_decode_access_token_rejects_malformed_claims() -> None:
    malformed = jwt.encode(
        {
            "sub": "not-a-uuid",
            "org_id": str(uuid.uuid4()),
            "type": "access",
            "iat": datetime.now(UTC),
            "exp": datetime.now(UTC) + timedelta(minutes=5),
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )

    with pytest.raises(auth_service.InvalidTokenError):
        auth_service.decode_access_token(malformed)
