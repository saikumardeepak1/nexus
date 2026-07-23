"""Request/response schemas for /v1/auth routes."""

import uuid

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class RegisterRequest(BaseModel):
    """Creates a brand-new organization plus its first (admin) user."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "organization_name": "Acme Corp",
                    "email": "ada@acme.example",
                    "password": "correct-horse-battery-staple",
                }
            ]
        }
    )

    organization_name: str = Field(min_length=1, max_length=255)
    email: EmailStr
    password: str = Field(min_length=8, max_length=255)


class LoginRequest(BaseModel):
    """Credentials for an existing user, exchanged for a token pair."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"email": "ada@acme.example", "password": "correct-horse-battery-staple"}
            ]
        }
    )

    email: EmailStr
    password: str = Field(min_length=1, max_length=255)


class RefreshRequest(BaseModel):
    """A previously issued refresh token, to be exchanged for a new token pair."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [{"refresh_token": "8f2b6c1e9a4d4f0b8e6c2a1d9f3b5e7c"}]
        }
    )

    refresh_token: str = Field(min_length=1)


class UserResponse(BaseModel):
    """The authenticated user's own public fields, embedded in every token
    pair response.
    """

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "examples": [
                {
                    "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                    "organization_id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
                    "email": "ada@acme.example",
                    "role": "admin",
                }
            ]
        },
    )

    id: uuid.UUID
    organization_id: uuid.UUID
    email: EmailStr
    role: str


class TokenPairResponse(BaseModel):
    """Returned by register, login, and refresh. ``refresh_token`` rotates on
    every /v1/auth/refresh call; the token used to obtain it is invalidated.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
                    "refresh_token": "8f2b6c1e9a4d4f0b8e6c2a1d9f3b5e7c",
                    "token_type": "bearer",
                    "expires_in": 900,
                    "user": {
                        "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                        "organization_id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
                        "email": "ada@acme.example",
                        "role": "admin",
                    },
                }
            ]
        }
    )

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserResponse
