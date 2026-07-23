# Architecture: Nexus

## System diagram

```mermaid
graph TB
    User["Employee / knowledge worker"] -->|"browser"| Web["Next.js dashboard + chat UI"]

    subgraph Nexus["Nexus Platform"]
        Web -->|"JWT session auth<br/>REST + streaming"| API["FastAPI API service"]
        API -->|"upload document"| DB[(PostgreSQL)]
        API -->|"enqueue process_document"| Redis[(Redis<br/>queue)]
        Redis -->|"job dequeue"| Worker["RQ worker"]
        Worker -->|"parse, chunk"| Worker
        Worker -->|"embed (BGE-small, local)"| Worker
        Worker -->|"upsert dense vectors"| Qdrant[(Qdrant<br/>vector store)]
        Worker -->|"write chunks + tsvector"| DB
        API -->|"hybrid query: dense"| Qdrant
        API -->|"hybrid query: lexical FTS"| DB
        API -->|"rerank candidates<br/>(BGE cross-encoder, local)"| API
        API -->|"generate cited answer"| Gemini["Gemini API<br/>(external)"]
        API -->|"read/write conversations, messages"| DB
    end
```

## Component responsibilities

| Component | Responsibility |
|---|---|
| API service | Auth, document upload, hybrid retrieval, reranking, RAG orchestration (LangGraph), conversation REST/streaming API, OpenAPI docs |
| Worker | Async document processing: parsing, chunking, embedding, vector upsert, full-text index write |
| Web dashboard | Document library, ingestion status, chat interface with streaming answers and inline citations |
| PostgreSQL | System of record for all entities, plus full-text search index (lexical retrieval half of hybrid search) |
| Qdrant | Dense vector store (semantic retrieval half of hybrid search) |
| Redis | Job queue between API and worker |
| Gemini (external) | Final citation-backed answer generation from the reranked context |

## Entity relationship (core tables)

```mermaid
erDiagram
    ORGANIZATION ||--o{ USER : has
    ORGANIZATION ||--o{ API_KEY : has
    ORGANIZATION ||--o{ DOCUMENT : owns
    DOCUMENT ||--o{ CHUNK : split_into
    ORGANIZATION ||--o{ CONVERSATION : has
    CONVERSATION ||--o{ MESSAGE : contains
    MESSAGE ||--o{ CITATION : cites

    ORGANIZATION {
        uuid id PK
        string name
        timestamp created_at
    }
    USER {
        uuid id PK
        uuid organization_id FK
        string email
        string hashed_password
        string role
    }
    API_KEY {
        uuid id PK
        uuid organization_id FK
        string prefix
        string hashed_key
        timestamp created_at
        timestamp revoked_at
    }
    DOCUMENT {
        uuid id PK
        uuid organization_id FK
        string filename
        string status
        int page_count
        timestamp created_at
    }
    CHUNK {
        uuid id PK
        uuid document_id FK
        int chunk_index
        text content
        tsvector content_tsv
        string qdrant_point_id
        int page_number
    }
    CONVERSATION {
        uuid id PK
        uuid organization_id FK
        uuid user_id FK
        string title
        timestamp created_at
    }
    MESSAGE {
        uuid id PK
        uuid conversation_id FK
        string role
        text content
        timestamp created_at
    }
    CITATION {
        uuid id PK
        uuid message_id FK
        uuid chunk_id FK
        float relevance_score
    }
```

## Request flow: ingesting a document

1. User uploads a PDF/text file via the dashboard.
2. API validates the file, persists a `Document` row (`status=queued`), stores the raw file, returns immediately.
3. API enqueues `process_document(document_id)` onto Redis.
4. Worker picks up the job: parses the file (`pypdf` for PDF), chunks the text, computes an embedding per chunk (`BAAI/bge-small-en-v1.5`, run locally via `sentence-transformers`), upserts each chunk's vector into Qdrant, writes `Chunk` rows to Postgres (with `content_tsv` populated for full-text search), and updates `Document.status` to `ready` (or `failed` with an error detail).
5. Dashboard polls/refetches document status and reflects `ready` once processing completes.

## Request flow: answering a question

1. User sends a message in a conversation via the chat UI.
2. API resolves conversation history (prior turns) and runs the LangGraph RAG graph:
   - **Retrieve node**: run dense search (Qdrant, embed the query with the same BGE-small model) and lexical search (Postgres FTS) in parallel, merge into one hybrid candidate set.
   - **Rerank node**: score each candidate against the query with a local BGE cross-encoder (`BAAI/bge-reranker-base`), keep the top-K.
   - **Generate node**: send the reranked chunks plus conversation history to Gemini, prompted to answer using only the provided context and to cite chunk sources.
3. API persists the assistant `Message` plus `Citation` rows linking to the chunks actually used, and streams the answer to the client as it generates.
4. Chat UI renders the streamed answer with inline citation markers linking back to the source document/passage.

## Deployment topology (v1)

Single-host Docker Compose: `api`, `worker`, `web`, `postgres`, `qdrant`, `redis` containers on one Docker network, `web` and `api` exposed to the host. `GEMINI_API_KEY` is the only required external secret; embedding and reranking models run locally in the `api`/`worker` containers. See the deployment guide in the root README once the scaffolding PR lands.
