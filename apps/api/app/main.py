"""FastAPI application entrypoint."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.api_keys import router as api_keys_router
from app.api.auth import router as auth_router
from app.api.chunks import router as chunks_router
from app.api.conversations import router as conversations_router
from app.api.documents import router as documents_router
from app.core.config import settings
from app.core.logging import configure_logging
from app.core.middleware import CorrelationIdMiddleware

# Configured before the FastAPI app is constructed so every log line emitted
# during app setup, and every log line emitted by any route handler or
# service afterward, is JSON with a correlation id when one is set (see
# app/core/logging.py and docs/TDD.md section 8).
configure_logging()

app = FastAPI(
    title="Nexus API",
    version="0.1.0",
    description=(
        "Enterprise knowledge intelligence platform: upload documents, ask "
        "questions over them, and get citation-backed answers from hybrid "
        "(semantic + keyword) retrieval with reranking. This page is "
        "generated directly from the running API, so it is always current "
        "with the deployed behavior."
    ),
    openapi_tags=[
        {
            "name": "auth",
            "description": (
                "Organization/user bootstrap, login, and JWT session refresh."
            ),
        },
        {
            "name": "api-keys",
            "description": "Issue and revoke programmatic API keys for an organization.",
        },
        {
            "name": "documents",
            "description": (
                "Upload documents and inspect their ingestion status "
                "(queued, processing, ready, failed)."
            ),
        },
        {
            "name": "conversations",
            "description": (
                "Multi-turn, citation-backed conversations over an organization's "
                "ingested documents, including the streaming answer endpoint."
            ),
        },
        {
            "name": "chunks",
            "description": "Resolve a citation back to the source chunk it references.",
        },
    ],
)

# The dashboard (apps/web) calls this API from the browser on a different
# origin (different port in local dev), so a JSON POST like /v1/auth/login
# triggers a CORS preflight. Without this, the browser blocks the request
# before it ever reaches a route handler.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        origin.strip() for origin in settings.cors_allowed_origins.split(",") if origin.strip()
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Added after CORSMiddleware: Starlette's add_middleware makes whichever
# middleware was added most recently the outermost layer, so
# CorrelationIdMiddleware wraps CORSMiddleware rather than the other way
# around. That puts it as early as possible in the request's life, before
# every other layer this app has, so the correlation id is already set by
# the time any route handler or service runs, and every log line the
# request produces anywhere downstream carries it.
app.add_middleware(CorrelationIdMiddleware)

app.include_router(auth_router)
app.include_router(api_keys_router)
app.include_router(documents_router)
app.include_router(conversations_router)
app.include_router(chunks_router)


@app.get("/health")
async def health() -> dict[str, str]:
    """Unauthenticated liveness check used by Docker Compose and uptime monitors."""
    return {"status": "ok"}
