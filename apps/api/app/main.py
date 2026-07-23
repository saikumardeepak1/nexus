"""FastAPI application entrypoint."""

from fastapi import FastAPI

from app.api.api_keys import router as api_keys_router
from app.api.auth import router as auth_router

app = FastAPI(title="Nexus API", version="0.1.0")

app.include_router(auth_router)
app.include_router(api_keys_router)


@app.get("/health")
async def health() -> dict[str, str]:
    """Unauthenticated liveness check used by Docker Compose and uptime monitors."""
    return {"status": "ok"}
