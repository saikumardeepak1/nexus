# Product Requirements Document: Nexus

## Status
Draft v1.0 — Foundation phase

## Summary
Nexus is an enterprise knowledge intelligence platform: teams upload their internal documents (PDFs, text, wikis-as-text) and get a citation-backed conversational interface over that corpus. Instead of employees searching scattered docs or hallucination-prone chatbots making things up, Nexus retrieves the actual source passages, reranks them for relevance, and generates answers that cite exactly which document and passage they came from.

## Problem statement
Organizations sit on large bodies of internal documentation (policies, runbooks, product docs, research reports) that are hard to search and even harder to ask follow-up questions of:
- **Keyword search fails on intent** — employees know what they want but not the exact terms the document uses.
- **No conversational interface** — most internal doc systems are search-and-click, not ask-and-answer.
- **Trust gap with generic LLM chat** — pasting a question into a general chatbot risks a fabricated answer with no way to verify it against a source document.
- **No multi-turn context** — follow-up questions ("what about for the EU region?") require re-stating the whole question from scratch in most tools.

## Goals
1. Let a team ingest a batch of documents (PDF/text) and have them searchable within minutes.
2. Provide hybrid retrieval (semantic + keyword) that outperforms pure vector search or pure keyword search alone.
3. Rerank retrieved passages before generation so the most relevant chunks drive the answer.
4. Generate answers that cite the specific document and chunk they draw from, so every claim is traceable to a source.
5. Support multi-turn conversations where follow-up questions retain context from earlier turns.
6. Be self-hostable via Docker Compose, with only one required external dependency (a Gemini API key for final answer generation) — embeddings and reranking run locally.

## Non-goals (v1)
- Multi-modal ingestion (images, audio, video) — text and PDF only.
- Real-time document sync from external systems (Confluence, Notion, Google Drive connectors) — manual upload only.
- Fine-tuning or hosting the embedding/reranking/generation models — Nexus uses pretrained local models and a hosted generation API.
- Multi-region / high-availability deployment topology.
- Role-based per-document access control beyond organization-level isolation (every user in an org sees every doc in that org's corpus for v1).

## Target users
- **Internal platform/knowledge teams** who own a corpus of docs and want to make it queryable without building retrieval infrastructure themselves.
- **Employees/end users** who need fast, trustworthy answers grounded in company documentation instead of digging through wikis or asking a generic chatbot.
- **Engineering leadership** evaluating a RAG reference implementation with hybrid search, reranking, and citation-backed generation done correctly.

## Core features

### Ingestion
- Document upload (PDF, plain text) with validation and storage.
- Async processing pipeline: parse, chunk, embed, index — off the request path.
- Chunking strategy that preserves enough context per chunk for standalone retrieval.
- Per-document ingestion status (queued, processing, ready, failed) visible in the dashboard.

### Retrieval
- Dense (semantic) search via local embeddings stored in Qdrant.
- Keyword (lexical) search via Postgres full-text search.
- Hybrid search combining both signals into a single ranked candidate set.
- Cross-encoder reranking of the hybrid candidate set before it reaches generation.

### Conversational RAG
- Citation-backed answer generation: every answer references the document/chunk it draws from.
- Multi-turn conversation memory: follow-up questions are answered with prior turns as context.
- Streaming responses in the chat UI.

### Dashboard
- Document library: upload, ingestion status, delete.
- Chat interface: ask questions, see cited sources inline, browse conversation history.

## Success criteria (v1)
- A 50-page PDF is fully ingested (parsed, chunked, embedded, indexed) in under 2 minutes.
- Hybrid search + reranking returns the relevant chunk in the top 3 results for a representative internal eval set of question/answer pairs derived from the ingested corpus.
- Every generated answer includes at least one citation resolving to a real chunk of a real ingested document.
- A follow-up question in the same conversation is answered using prior-turn context without the user re-stating it.
- Entire stack (`api`, `web`, `worker`, `postgres`, `qdrant`, `redis`) starts with a single `docker compose up`, with only `GEMINI_API_KEY` required as an external secret.

## Open questions
- Whether to expose a tunable hybrid-search weighting (dense vs. lexical) to end users or keep it a fixed internal constant — starting fixed, revisit if eval results show corpus-dependent variance.
- Whether conversation memory should be windowed (last N turns) or summarized once it grows long — starting windowed for simplicity, summarization is a documented future improvement.
