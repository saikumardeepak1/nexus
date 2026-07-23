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
    jwt_secret: str = "dev-secret-change-me"
    gemini_api_key: str = ""


settings = Settings()
