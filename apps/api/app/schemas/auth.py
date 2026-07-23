"""Request/response schemas for /v1/auth routes."""

import uuid

from pydantic import BaseModel, EmailStr, Field


class RegisterRequest(BaseModel):
    """Creates a brand-new organization plus its first (admin) user."""

    organization_name: str = Field(min_length=1, max_length=255)
    email: EmailStr
    password: str = Field(min_length=8, max_length=255)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=255)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1)


class UserResponse(BaseModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    email: EmailStr
    role: str

    model_config = {"from_attributes": True}


class TokenPairResponse(BaseModel):
    """Returned by register, login, and refresh. ``refresh_token`` rotates on
    every /v1/auth/refresh call; the token used to obtain it is invalidated.
    """

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserResponse
