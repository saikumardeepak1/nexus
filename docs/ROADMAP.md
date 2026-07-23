# Roadmap: Nexus

Tracked as GitHub milestones and issues on this repository. This doc is the human-readable mirror.

## Milestone 1: Foundation
- Project scaffolding & CI/CD pipeline
- Database schema design (Postgres + Alembic)
- API authentication (API keys + JWT sessions)
- Frontend app shell & design system setup

## Milestone 2: Document Ingestion & Retrieval
- Document upload endpoint & storage
- PDF/text parsing & chunking service
- Async ingestion pipeline (Redis/RQ worker)
- Embedding service (local BGE-small)
- Qdrant vector store integration
- Postgres full-text search integration
- Hybrid search service (dense + lexical fusion)
- Reranking service (local BGE cross-encoder)
- Document management dashboard UI

## Milestone 3: Conversational RAG
- LangGraph RAG orchestration graph
- Gemini generation service with citation-backed answers
- Conversation memory & multi-turn context
- Chat UI with streaming responses & citations

## Milestone 4: Platform Hardening
- Structured logging & app-level observability
- Test coverage: unit + integration + retrieval eval
- Deployment guide & production Docker Compose hardening
- API documentation (OpenAPI + docs site)

## Sequencing
Milestones are built roughly in order, but issues within a milestone may interleave where dependencies allow (e.g. embedding service and Qdrant integration can proceed in parallel once chunking lands, and both must land before hybrid search). Each issue ships as its own feature branch and PR — see `CONTRIBUTING.md` for the branch naming and PR conventions used in this repo.

`GEMINI_API_KEY` is provided by Deepak locally, not committed; the generation service and its live-call smoke test are built against it once available, but ingestion, chunking, embedding, hybrid search, and reranking are all built and fully tested without it.

## Explicitly out of scope for v1
- External document source connectors (Confluence, Notion, Drive)
- Multi-modal ingestion (images, audio, video)
- Per-document access control below the organization level
- Multi-region/HA deployment
- Managed cloud hosting (Docker Compose self-host only)
