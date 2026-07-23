"""FastAPI application entrypoint."""

from fastapi import FastAPI

app = FastAPI(title="Nexus API", version="0.1.0")


@app.get("/health")
async def health() -> dict[str, str]:
    """Unauthenticated liveness check used by Docker Compose and uptime monitors."""
    return {"status": "ok"}
