"""Application configuration, read from environment variables (or a .env file locally)."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the API and worker processes.

    Every field has a local-development default so the app boots without a
    .env file present; production deployments should override all of them
    via real environment variables, especially jwt_secret.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://nexus:nexus@localhost:5432/nexus"
    redis_url: str = "redis://localhost:6379/0"
    qdrant_url: str = "http://localhost:6333"
    jwt_secret: str = "dev-secret-change-me-32-bytes-min"
    gemini_api_key: str = ""

    # Auth (see docs/TDD.md section 3.6 for the design this implements).
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 7
    # HMAC pepper used to deterministically hash API keys and refresh tokens
    # so a presented secret can be looked up by exact hash match. Separate
    # from jwt_secret so rotating one does not silently rotate the other.
    api_key_pepper: str = "dev-api-key-pepper-change-me"

    # Comma-separated list of origins allowed to call the API from a browser
    # (see app/main.py CORSMiddleware setup). Defaults to the web app's dev
    # origin from infra/docker-compose.yml.
    cors_allowed_origins: str = "http://localhost:3000"


settings = Settings()
