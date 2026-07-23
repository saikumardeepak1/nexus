# Nexus

Enterprise Knowledge Intelligence Platform — hybrid retrieval (semantic + keyword) with reranking and citation-backed conversational answers over your own documents.

## Why Nexus

Internal documentation is hard to search and impossible to have a conversation with. Nexus ingests your PDFs and text docs, indexes them with both dense (semantic) and lexical (keyword) search, reranks the combined candidates, and answers questions with citations back to the exact source passage — instead of a generic chatbot guessing.

## Documentation

- [Product Requirements Document](docs/PRD.md)
- [Technical Design Document](docs/TDD.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Roadmap](docs/ROADMAP.md)
- **API reference** — interactive, generated from the running API: Swagger UI at `/docs`, ReDoc at `/redoc` (e.g. http://localhost:8000/docs once the stack is up). Every route has a description and every schema has an example payload — there's no separately maintained API doc to go stale.

## Status

Early development. See [Roadmap](docs/ROADMAP.md) and the [issue tracker](https://github.com/saikumardeepak1/nexus/issues) for current progress.

## Project structure

```
apps/api/    FastAPI backend: ingestion, hybrid retrieval, reranking, LangGraph RAG orchestration
apps/web/    Next.js dashboard + chat UI
infra/       Docker Compose and deployment config
docs/        Planning and architecture docs
```

## Getting started

Requires Docker, Python 3.12+, and Node 20+.

```bash
cp apps/api/.env.example apps/api/.env
# edit apps/api/.env — set a real JWT_SECRET and your GEMINI_API_KEY

docker compose -f infra/docker-compose.yml up --build

# api: http://localhost:8000 (docs at /docs)
# web: http://localhost:3000
```

That brings up `postgres`, `qdrant`, `redis`, a one-shot `migrate` service (applies Alembic migrations, then exits — `api` and `worker` both wait for it to finish before starting), `api`, `worker`, and `web`.

Embeddings (`BAAI/bge-small-en-v1.5`) and reranking (`BAAI/bge-reranker-base`) run locally via `sentence-transformers` inside the `api`/`worker` containers — no external key needed for ingestion or retrieval. `GEMINI_API_KEY` is only required for the final answer-generation step.

### Running services individually

```bash
# API
cd apps/api
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
uvicorn app.main:app --reload

# Worker (needs Postgres + Redis + Qdrant reachable, and migrations already applied)
cd apps/api && source .venv/bin/activate
python -m app.workers.worker

# Web
cd apps/web
cp .env.example .env.local
npm install
npm run dev
```

### Running tests

```bash
# API
cd apps/api && source .venv/bin/activate && pytest -q --cov=app

# Web
cd apps/web && npm run test          # unit/component tests
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the branch/PR workflow.

## Deployment

The `docker-compose.yml` in `infra/` is the primary deployment target for v1 — a single-host stack suitable for a small/medium production workload. See [docs/ROADMAP.md](docs/ROADMAP.md) for what's explicitly out of scope.

### 1. Configure environment

```bash
cp apps/api/.env.example apps/api/.env
```

Edit `apps/api/.env` and set:
- **`JWT_SECRET`** — a real random secret (`python -c "import secrets; print(secrets.token_urlsafe(32))"`).
- **`GEMINI_API_KEY`** — your Gemini API key. Required for answer generation; everything else (upload, ingestion, chunking, embedding, hybrid search, reranking) works without it.

`DATABASE_URL`, `REDIS_URL`, and `QDRANT_URL` in `.env` are for running the API outside Docker — `docker-compose.yml` overrides all three to point at the in-network service hostnames automatically.

### 2. Bring up the stack

```bash
docker compose -f infra/docker-compose.yml up --build -d
```

Check everything is healthy:

```bash
docker compose -f infra/docker-compose.yml ps
curl http://localhost:8000/health
```

### What this setup doesn't cover

- TLS termination — put a reverse proxy (nginx, Caddy, a cloud load balancer) in front of `api` and `web` for anything beyond local testing.
- Horizontal scaling — `worker` can be scaled with `docker compose up --scale worker=3`, since RQ workers just compete for jobs on the same queue.
- Managed/HA Postgres, Qdrant, and Redis — the compose file runs single-instance containers, fine for the workload this is designed for.

## License

MIT — see [LICENSE](LICENSE).
