/**
 * Request/response shapes mirrored from apps/api/app/schemas/auth.py.
 * Keep these in sync with the backend schemas if that file changes.
 */

export interface LoginRequest {
  email: string;
  password: string;
}

export interface RefreshRequest {
  refresh_token: string;
}

export interface UserResponse {
  id: string;
  organization_id: string;
  email: string;
  role: string;
}

export interface TokenPairResponse {
  access_token: string;
  refresh_token: string;
  token_type: string;
  expires_in: number;
  user: UserResponse;
}

/**
 * Request/response shapes mirrored from apps/api/app/schemas/document.py.
 */

export type DocumentStatus = "queued" | "processing" | "ready" | "failed";

export interface DocumentResponse {
  id: string;
  organization_id: string;
  filename: string;
  status: DocumentStatus;
  page_count: number | null;
  error_detail: string | null;
  created_at: string;
}

export interface DocumentListResponse {
  documents: DocumentResponse[];
}

/**
 * Request/response shapes mirrored from apps/api/app/schemas/conversation.py.
 */

export interface ConversationCreateRequest {
  title?: string | null;
}

export interface ConversationResponse {
  id: string;
  organization_id: string;
  user_id: string;
  title: string | null;
  created_at: string;
}

export interface ConversationListResponse {
  conversations: ConversationResponse[];
}

/**
 * A persisted citation as returned by GET /v1/conversations/{id}: no
 * `marker`/`text_position` (see apps/api/app/models/citation.py, which only
 * stores chunk_id and relevance_score). Distinguished from `CitationEvent`
 * (below), which is what the SSE `done` event carries for a message that
 * just streamed, so lib/citations.ts can tell the two apart at runtime.
 */
export interface CitationResponse {
  id: string;
  chunk_id: string;
  relevance_score: number;
}

export interface MessageResponse {
  id: string;
  conversation_id: string;
  role: "user" | "assistant";
  content: string;
  created_at: string;
  /**
   * `CitationResponse[]` for every message the API itself returned (GET
   * /v1/conversations/{id}). The chat UI also builds a client-side
   * `MessageResponse` the instant a message finishes streaming (before any
   * refetch), and that one's citations come straight from the SSE `done`
   * event as `CitationEvent[]` -- richer (it has `marker`), not poorer, so
   * widening this field to accept either shape here (rather than
   * downgrading fresh data to fit the narrower persisted shape) keeps the
   * accurate, already-resolved marker mapping intact until this
   * conversation is reloaded from the API. See lib/citations.ts for how a
   * consumer tells the two apart.
   */
  citations: Array<CitationResponse | CitationEvent>;
}

export interface ConversationDetailResponse extends ConversationResponse {
  messages: MessageResponse[];
}

export interface MessageCreateRequest {
  content: string;
}

/**
 * One citation as carried by the SSE `done` event (see
 * apps/api/app/api/conversations.py's `send_message`), including `marker`
 * (the `[n]` number as it appeared in the streamed text) and `text_position`,
 * which the persisted `CitationResponse` above does not have.
 */
export interface CitationEvent {
  chunk_id: string;
  relevance_score: number;
  marker: number;
  text_position: number;
}

export interface MessageDoneEvent {
  message_id: string;
  citations: CitationEvent[];
}

/**
 * Request/response shapes mirrored from apps/api/app/schemas/chunk.py.
 */

export interface ChunkResponse {
  id: string;
  document_id: string;
  filename: string;
  content: string;
  page_number: number | null;
}
