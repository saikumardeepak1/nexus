# Nexus

Enterprise Knowledge Intelligence Platform: hybrid retrieval (semantic + keyword) with reranking and citation-backed conversational answers over your own documents.

## Why Nexus

Internal documentation is hard to search and impossible to have a conversation with. Nexus ingests your PDFs and text docs, indexes them with both dense (semantic) and lexical (keyword) search, reranks the combined candidates, and answers questions with citations back to the exact source passage, instead of a generic chatbot guessing.

## Documentation

- [Product Requirements Document](docs/PRD.md)
- [Technical Design Document](docs/TDD.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Roadmap](docs/ROADMAP.md)
- **API reference**: interactive, generated from the running API. Swagger UI at `/docs`, ReDoc at `/redoc` (e.g. http://localhost:8000/docs once the stack is up). Every route has a description and every schema has an example payload, so there's no separately maintained API doc to go stale.

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
# edit apps/api/.env: set a real JWT_SECRET and your GEMINI_API_KEY

docker compose -f infra/docker-compose.yml up --build

# api: http://localhost:8000 (docs at /docs)
# web: http://localhost:3000
```

That brings up `postgres`, `qdrant`, `redis`, a one-shot `migrate` service (applies Alembic migrations, then exits; `api` and `worker` both wait for it to finish before starting), `api`, `worker`, and `web`. See [Deployment](#deployment) below for a full explanation of that startup order, the env vars that matter for anything beyond local use, and what this setup doesn't cover.

Embeddings (`BAAI/bge-small-en-v1.5`) and reranking (`BAAI/bge-reranker-base`) run locally via `sentence-transformers` inside the `api`/`worker` containers: no external key needed for ingestion or retrieval. `GEMINI_API_KEY` is only required for the final answer-generation step.

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

The `docker-compose.yml` in `infra/` is the primary deployment target for v1: a single-host stack suitable for a small/medium production workload. It runs `postgres`, `qdrant`, `redis`, a one-shot `migrate` service, `api`, `worker`, and `web`. See [docs/ROADMAP.md](docs/ROADMAP.md) for the longer-term roadmap and "What this setup doesn't cover" below for what's explicitly out of scope today.

### 1. Configure environment

```bash
cp apps/api/.env.example apps/api/.env
```

`docker compose` reads variable overrides from a `.env` file in the directory it's run from (or the project directory passed via `--project-directory`), and from whatever is already in your shell environment, not from `apps/api/.env`, which is only consumed by the API process itself when running outside Docker. For a Docker deployment, export the variables below in your shell, or put them in a `.env` file next to `infra/docker-compose.yml`, before running `docker compose up`.

Every setting below has a working default so the stack boots with zero configuration, which is convenient for local development and exactly why you must not skip this step in a real deployment: every default is a publicly known placeholder value. The full list of settings with insecure `dev-*`/placeholder defaults lives in [`apps/api/app/core/config.py`](apps/api/app/core/config.py); the ones that matter for a production deployment are:

| Variable | Default | Why it matters |
|---|---|---|
| `JWT_SECRET` | `dev-secret-change-me-32-bytes-min` | Signs session JWTs. Anyone who knows this value (it's in the public repo) can forge a valid session for any user. Generate a real one: `python -c "import secrets; print(secrets.token_urlsafe(32))"`. |
| `API_KEY_PEPPER` | `dev-api-key-pepper-change-me` | HMAC pepper used to hash API keys and refresh tokens before storing them. Same exposure as `JWT_SECRET` if left at the default: a known pepper makes stored hashes reversible-by-guessing for anyone who also gets the database. Generate the same way as `JWT_SECRET`, as a separate value. |
| `GEMINI_API_KEY` | empty | Your Gemini API key. Required only for the final answer-generation step; upload, ingestion, chunking, embedding, hybrid search, and reranking all run locally and work without it. Leave it unset and the app still runs, it just can't answer questions yet. |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | `nexus` / `nexus` / `nexus` | Postgres superuser credentials for the `postgres` container. If you override `POSTGRES_PASSWORD`, also update `DATABASE_URL` (below) to match: compose cannot substitute one variable's value inside another's default, so the two have to be kept in sync by hand. |
| `DATABASE_URL` | `postgresql+asyncpg://nexus:nexus@postgres:5432/nexus` | Full connection string used by `migrate`, `api`, and `worker`. Only needs overriding if you changed the Postgres credentials above. |
| `CORS_ALLOWED_ORIGINS` | `http://localhost:3000` | Comma-separated list of origins the browser is allowed to call the API from. Defaults to the web app's local dev origin; if `web` is served from anywhere else, requests from the browser are blocked until this is updated to match. |
| `NEXT_PUBLIC_API_URL` | `http://localhost:8000` | The URL the browser (not the Docker network) uses to reach the API. Baked into the `web` build at image build time, not read at container runtime (it's a Next.js `NEXT_PUBLIC_*` variable), so changing it requires `docker compose up --build`, not just a restart. Keep it and `CORS_ALLOWED_ORIGINS` pointed at each other: this is where the web app is served from, that's who the API needs to allow. |

None of these are prompted for or validated at startup; every one silently keeps its insecure default if you don't set it. There's no fail-fast check today, so this list is the closest thing to one, read it before deploying anywhere reachable outside your own machine.

### 2. Bring up the stack

```bash
docker compose -f infra/docker-compose.yml up --build -d
```

Startup ordering is enforced by `depends_on` conditions, not just declaration order: `postgres`, `redis`, and `qdrant` each have a healthcheck, and `migrate` waits for `postgres` to report healthy before it runs `alembic upgrade head` and exits. `api` and `worker` both wait on `migrate` reaching a successful exit (`condition: service_completed_successfully`) as well as `postgres`/`redis`/`qdrant` being healthy, so neither one can start serving requests or pulling jobs against a database that hasn't been migrated yet. `web` waits on `api` being healthy (not just started) before it starts.

Check everything is healthy:

```bash
docker compose -f infra/docker-compose.yml ps
curl http://localhost:8000/health
```

`docker compose ps` should show `migrate` as `Exited (0)` and every other service as `running (healthy)`.

### Viewing logs

```bash
docker compose -f infra/docker-compose.yml logs -f            # all services
docker compose -f infra/docker-compose.yml logs -f api worker # just the RAG backend
```

Each container currently logs plain text to stdout, which `docker compose logs` captures as-is; if structured JSON logging (see [docs/TDD.md](docs/TDD.md) section 8) has landed by the time you're reading this, the same commands still work, each line is just a JSON object instead of plain text, and is easy to pipe into `jq` or a log aggregator.

### Persistent data

`postgres_data`, `qdrant_data`, and `document_storage` are all named Docker volumes (declared at the bottom of `docker-compose.yml`), so document metadata, vectors, and the raw uploaded files all survive `docker compose down` and container restarts. They are only removed by an explicit `docker compose down -v`.

### Hardening notes

- `postgres`, `qdrant`, and `redis` publish their ports bound to `127.0.0.1` only (`127.0.0.1:5432:5432`, etc.), not `0.0.0.0`, so they're reachable for local debugging (`psql`, `redis-cli`) but not from off the host. `api` and `web` publish on all interfaces since they're meant to be reachable, put a reverse proxy in front of them (see below) rather than further restricting these.
- `postgres`, `qdrant`, `redis`, `api`, `worker`, and `web` all run with `restart: unless-stopped`, so the stack comes back up on its own after a host reboot or a container crash. `migrate` intentionally has no restart policy: it's meant to run once and exit 0, not loop.
- Neither Redis nor Qdrant has authentication enabled in this setup; they rely entirely on not being reachable off-host (the point of the `127.0.0.1` port binding above). If you ever split this across multiple hosts, you need to add real auth/TLS to both before doing so.

### What this setup doesn't cover

- **TLS termination**: put a reverse proxy (nginx, Caddy, a cloud load balancer) in front of `api` and `web` for anything beyond local testing. Nothing in this compose file terminates TLS.
- **Horizontal scaling**: `worker` can be scaled with `docker compose up --scale worker=3`, since RQ workers just compete for jobs on the same queue. `api` and `web` are not designed to be scaled behind a load balancer by this compose file; that requires the reverse proxy above and is out of scope for now.
- **Managed/HA Postgres, Qdrant, and Redis**: the compose file runs single-instance containers with no replication or failover. Fine for the workload this is designed for. If you need HA, point `DATABASE_URL`/`QDRANT_URL`/`REDIS_URL` at managed instances instead of the bundled containers.

## License

MIT, see [LICENSE](LICENSE).
