"""FastAPI application entrypoint."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.api_keys import router as api_keys_router
from app.api.auth import router as auth_router
from app.api.documents import router as documents_router
from app.core.config import settings

app = FastAPI(title="Nexus API", version="0.1.0")

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

app.include_router(auth_router)
app.include_router(api_keys_router)
app.include_router(documents_router)


@app.get("/health")
async def health() -> dict[str, str]:
    """Unauthenticated liveness check used by Docker Compose and uptime monitors."""
    return {"status": "ok"}
