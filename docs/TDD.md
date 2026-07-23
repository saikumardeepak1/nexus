# Technical Design Document: Nexus

## Status
Draft v1.0 — Foundation phase. Companion to [PRD.md](PRD.md) and [ARCHITECTURE.md](ARCHITECTURE.md).

## 1. Overview
Nexus is a multi-package repository with three deployable units — an API service, a web dashboard, and a background worker — sharing one Postgres database, one Qdrant collection set, and one Redis instance. No SDK package (unlike Helios); Nexus is consumed directly via its web UI and REST API.

## 2. Repository layout
```
nexus/
  apps/
    api/                FastAPI backend (ingestion, retrieval, RAG orchestration, auth, worker entrypoint)
      app/
        api/             route modules
        models/          SQLAlchemy models
        schemas/         Pydantic schemas
        services/        business logic (ingestion, chunking, embedding, retrieval, reranking, generation)
        graph/            LangGraph RAG graph definition (retrieve -> rerank -> generate)
        core/            config, security, db session
        workers/         RQ job definitions
      alembic/           migrations
      tests/
    web/                 Next.js dashboard + chat UI
      app/               App Router routes
      components/
      lib/
      tests/
  infra/
    docker-compose.yml
  docs/
  .github/workflows/
```

## 3. Backend design

### 3.1 Tech stack
Python 3.12, FastAPI, SQLAlchemy 2.0 (async engine, asyncpg driver), Alembic, Pydantic v2, RQ + Redis for background jobs, `argon2-cffi` for password hashing, `PyJWT` for JWT, `qdrant-client` for vector storage, `sentence-transformers` for local embedding/reranking, `langgraph` for RAG orchestration, `google-genai` for Gemini generation, `pypdf` for PDF parsing.

### 3.2 Services layered under `app/services/`
- `ingestion_service`: validates and persists uploaded documents, enqueues a `process_document` job.
- `chunking_service`: parses raw document bytes (PDF via `pypdf`, plain text directly) and splits into overlapping chunks sized for retrieval.
- `embedding_service`: wraps `sentence-transformers` (`BAAI/bge-small-en-v1.5`) to embed chunks at ingestion time and queries at retrieval time — same model both directions so query/document vectors are comparable.
- `vector_store_service`: Qdrant collection management, upsert, dense k-NN search.
- `lexical_search_service`: Postgres full-text search over `chunks.content_tsv` (GIN index), `ts_rank`-scored.
- `hybrid_search_service`: runs dense + lexical search concurrently, merges and normalizes scores into one ranked candidate list (reciprocal rank fusion).
- `reranking_service`: wraps a local `sentence-transformers` cross-encoder (`BAAI/bge-reranker-base`) to re-score hybrid candidates against the query, returns top-K.
- `generation_service`: builds the citation-constrained prompt from reranked chunks + conversation history, calls Gemini, streams the response, parses citation markers against the chunks actually provided.
- `auth_service`: API key issuance/verification, JWT session issuance/verification (same design as Helios).

### 3.3 RAG orchestration (`app/graph/`)
A LangGraph `StateGraph` with three nodes: `retrieve` (calls `hybrid_search_service`), `rerank` (calls `reranking_service`), `generate` (calls `generation_service`). State carries the query, conversation history, retrieved/reranked chunks, and the streamed answer. Conversation memory is loaded into graph state from the `messages` table before the graph runs (windowed to the most recent N turns for v1) rather than relying on LangGraph's own checkpointer, so conversation history stays queryable/persisted the same way the rest of the app's data is.

### 3.4 API surface (high level, detailed OpenAPI generated at `/docs`)
- `POST /v1/documents` — upload a document, API-key or JWT session authenticated.
- `GET /v1/documents`, `GET /v1/documents/{id}` — ingestion status and metadata.
- `DELETE /v1/documents/{id}` — remove a document and its chunks/vectors.
- `POST /v1/conversations` — start a conversation.
- `POST /v1/conversations/{id}/messages` — send a message, streams the generated answer (SSE) with citations.
- `GET /v1/conversations`, `GET /v1/conversations/{id}` — conversation history.
- `POST /v1/auth/register` - bootstraps a new organization plus its first (admin) user, returns a token pair.
- `POST /v1/auth/login`, `POST /v1/auth/refresh` — JWT session auth for dashboard users.
- `POST /v1/api-keys`, `DELETE /v1/api-keys/{id}` - issue (raw key shown once) or revoke an API key, JWT session authenticated.
- `GET /health` — unauthenticated liveness check.

### 3.5 Async processing
Document upload writes the raw file and a `Document` row synchronously (fast, so the upload call returns quickly), then enqueues `process_document(document_id)` onto Redis. That job does parsing, chunking, embedding, and both the Qdrant upsert and the Postgres chunk write, then flips `Document.status` to `ready`/`failed`. This keeps the upload endpoint's latency independent of document size and model inference time.

### 3.6 Auth design
Reuses Helios's two-scheme design for consistency across the lab:
- `require_api_key`: reads `Authorization: Bearer nxs_live_...`, looks up the hashed key, resolves to an `Organization`. Available for programmatic document upload.
- `require_session`: reads a JWT from `Authorization: Bearer <jwt>`, resolves to a `User` scoped to an `Organization`. Used on dashboard routes.

API keys are generated with an `nxs_live_` prefix, shown once at creation, stored as a salted hash. JWTs are short-lived access tokens (15 minutes) with a refresh-token rotation flow (7-day opaque refresh tokens, persisted hashed in a `refresh_tokens` table, invalidated on every use so each token is redeemable exactly once). Every document, chunk, conversation, and message is scoped to an `organization_id`; all queries filter on it, so one org can never retrieve another org's corpus or conversations.

Two different hashing strategies are used for two different needs: passwords are hashed with Argon2 (via `argon2-cffi`) since they are only ever verified against one known row; API keys and refresh tokens are hashed with a server-peppered HMAC-SHA256 instead, since they must be looked up directly by hash equality (the caller doesn't supply a username alongside the secret to narrow the search), which requires a deterministic hash rather than Argon2's per-hash random salt. `POST /v1/auth/register` is the only path that creates the first `Organization`/`User` pair for a new tenant.

## 4. Frontend design
Next.js 14 App Router, TypeScript, Tailwind, shadcn/ui components, TanStack Query for server state. Chat UI consumes the SSE stream from `POST /v1/conversations/{id}/messages` and renders tokens incrementally, with citation markers resolved to a source panel showing the originating document and chunk text.

## 5. Data model
See [ARCHITECTURE.md](ARCHITECTURE.md) for the entity relationship diagram. Core tables: `organizations`, `users`, `api_keys`, `documents`, `chunks`, `conversations`, `messages`, `citations`.

## 6. Testing strategy
- **Unit tests**: services (`chunking_service`, `hybrid_search_service` score fusion, `reranking_service`, `generation_service` prompt construction) tested in isolation with fixture inputs.
- **Integration tests**: API routes tested against a real (test) Postgres and a real (test) Qdrant instance via `httpx.AsyncClient`, using pytest fixtures that spin up a transactional Postgres session and a scratch Qdrant collection per test.
- **Retrieval eval**: a small fixture corpus with known question/expected-chunk pairs, asserting the expected chunk appears in top-K after hybrid search + reranking — this is the test that actually validates retrieval quality, not just that the code runs.
- **Frontend unit tests**: component tests via Vitest + Testing Library.
- **API contract tests**: OpenAPI schema validated against example requests/responses in CI.
- Coverage tracked via `pytest-cov`, reported in CI job summary.
- Gemini generation calls are mocked in CI (no live API key available there); the real integration is smoke-tested manually once `GEMINI_API_KEY` is set locally, per the plan in [ROADMAP.md](ROADMAP.md).

## 7. CI/CD
GitHub Actions, one workflow (`ci.yml`) with parallel jobs: `api-test` (ruff, mypy, pytest against real Postgres + Qdrant service containers), `web-test` (eslint, tsc, vitest, next build), `docker-build` (build all Dockerfiles to verify they build cleanly). All required to pass before merge.

## 8. App-level observability
Same dogfooded structured-logging design as Helios, reused for consistency across the lab.

- **JSON logs everywhere**: `app/core/logging.py#configure_logging` replaces the root logger's handler with one JSON line per record (`timestamp`, `level`, `logger`, `message`, plus `correlation_id` when set) — both the API process and the RQ worker call it at startup.
- **Correlation IDs**: a `contextvars.ContextVar` holds the current request or job's id, set via `logging.setLogRecordFactory` (not a handler-level filter, for the same reason documented in Helios's TDD: submodule loggers with their own handlers, like RQ's and pytest's `caplog`, would miss a filter-based approach). `CorrelationIdMiddleware` sets it per API request; `process_document` sets a `job-<hex>` id per job.

## 9. Deployment
Docker Compose is the primary deployment target for v1: `docker-compose.yml` defines `api`, `worker`, `web`, `postgres`, `qdrant`, `redis`. Documented in [deployment guide](../README.md#deployment) once written. Environment configuration via `.env` (see `.env.example`); `GEMINI_API_KEY` is the only required external secret.

## 10. Tradeoffs and future improvements
- Qdrant dense vectors + Postgres FTS (not Qdrant sparse vectors) for the lexical half of hybrid search: keeps one fewer moving part in Qdrant's config and reuses Postgres infrastructure already needed for the rest of the app.
- Local BGE models (embedding + reranking) over hosted embedding APIs: zero additional external dependency/cost beyond Gemini, at the cost of running inference in the API/worker containers — acceptable at this corpus scale.
- Windowed conversation memory (last N turns) over summarization for v1: simpler, sufficient for typical multi-turn usage; summarization once conversations grow long is a documented future upgrade.
- Reusing Helios's exact auth pattern rather than designing a new one: consistency across the lab's repos, and it's already a proven design.
