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

    # Local filesystem directory raw uploaded documents are written to (see
    # app/services/ingestion_service.py). Relative by default so running the
    # API/tests directly on a host (outside Docker) just uses a directory
    # under the current working directory. infra/docker-compose.yml
    # overrides this to /data/documents, backed by a named volume shared
    # between the api and worker containers so the worker can read what the
    # api wrote.
    documents_storage_path: str = "./.data/documents"

    # Chunking (see app/services/chunking_service.py). Sizes are in
    # characters, not tokens: the local embedding model (BGE-small, 512
    # token max sequence length) comfortably fits a 700-character chunk of
    # English prose (roughly 120-150 tokens), leaving headroom before the
    # model's limit even for dense text. 700/100 gives ~14% overlap, enough
    # for a sentence or two of shared context between adjacent chunks
    # without duplicating so much text that retrieval returns near-identical
    # neighbors.
    chunk_size: int = 700
    chunk_overlap: int = 100

    # Embedding (see app/services/embedding_service.py). Run fully locally
    # via sentence-transformers, no external API key needed. Same model is
    # used for both document chunk embedding at ingestion time and query
    # embedding at retrieval time, so the two live in the same vector space.
    embedding_model_name: str = "BAAI/bge-small-en-v1.5"

    # Reranking (see app/services/reranking_service.py). A cross-encoder,
    # not the bi-encoder used for embeddings: it scores a (query, passage)
    # pair jointly, which is more accurate than comparing separately-embedded
    # vectors but only feasible against a small hybrid-search candidate set
    # rather than a whole corpus.
    reranker_model_name: str = "BAAI/bge-reranker-base"

    # Generation (see app/services/generation_service.py). The only external,
    # hosted model call in the whole retrieval pipeline; everything upstream
    # (embedding, reranking) runs locally. `gemini-3.6-flash` is the current
    # general-purpose flash-tier model, chosen for the same reason a flash
    # model was chosen at every prior Gemini generation: it is priced and
    # sized for high-volume, low-latency RAG generation rather than the
    # heavier reasoning-focused pro tier this workload does not need.
    gemini_model_name: str = "gemini-3.6-flash"


settings = Settings()
