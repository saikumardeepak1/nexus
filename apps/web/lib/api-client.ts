import { clearTokens, getAccessToken, getRefreshToken, storeTokenPair } from "./token-storage";
import type {
  ChunkResponse,
  ConversationDetailResponse,
  ConversationListResponse,
  ConversationResponse,
  DocumentListResponse,
  DocumentResponse,
  LoginRequest,
  MessageCreateRequest,
  MessageDoneEvent,
  RefreshRequest,
  TokenPairResponse,
} from "./types";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "";

if (!API_URL && typeof window !== "undefined") {
  // Fail loudly in the browser console rather than silently issuing requests
  // to a relative path (which would hit the Next.js server, not the API).
  console.error("NEXT_PUBLIC_API_URL is not set; API requests will fail.");
}

export class ApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function parseErrorMessage(response: Response): Promise<string> {
  try {
    const body: unknown = await response.json();
    if (body && typeof body === "object" && "detail" in body) {
      const detail = (body as { detail: unknown }).detail;
      if (typeof detail === "string") return detail;
    }
  } catch {
    // Response body was not JSON; fall through to the generic message below.
  }
  return response.statusText || "Request failed";
}

// Dedupes concurrent refresh attempts: if several requests 401 at once, only
// one /v1/auth/refresh call is made and the rest await its result.
let refreshPromise: Promise<boolean> | null = null;

async function refreshAccessToken(): Promise<boolean> {
  const refreshToken = getRefreshToken();
  if (!refreshToken) return false;

  if (!refreshPromise) {
    refreshPromise = (async () => {
      try {
        const response = await fetch(`${API_URL}/v1/auth/refresh`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ refresh_token: refreshToken } satisfies RefreshRequest),
        });
        if (!response.ok) {
          clearTokens();
          return false;
        }
        const data = (await response.json()) as TokenPairResponse;
        storeTokenPair(data);
        return true;
      } catch {
        clearTokens();
        return false;
      } finally {
        refreshPromise = null;
      }
    })();
  }

  return refreshPromise;
}

interface RequestOptions extends RequestInit {
  /** Skip attaching the access token and skip the 401-refresh-retry flow. Used for login. */
  skipAuth?: boolean;
  /** Internal: marks a request as already retried once, so a second 401 doesn't loop. */
  _isRetry?: boolean;
}

/**
 * Typed fetch wrapper for the FastAPI backend. Attaches the stored access
 * token, and on a 401 attempts one silent refresh (POST /v1/auth/refresh)
 * before retrying the original request exactly once. If the refresh also
 * fails, tokens are cleared and an ApiError is thrown for the caller to
 * handle (the caller decides whether to redirect to /login).
 */
export async function apiFetch<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { skipAuth, _isRetry, headers: incomingHeaders, ...rest } = options;

  const headers = new Headers(incomingHeaders);
  // FormData bodies (document upload) must not get a Content-Type set here:
  // the browser needs to set its own `multipart/form-data; boundary=...`
  // value, which is impossible to replicate by hand, so uploads would break
  // silently if this fell through to the JSON default below.
  if (rest.body && !(rest.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  if (!skipAuth) {
    const accessToken = getAccessToken();
    if (accessToken) headers.set("Authorization", `Bearer ${accessToken}`);
  }

  const response = await fetch(`${API_URL}${path}`, { ...rest, headers });

  if (response.status === 401 && !skipAuth && !_isRetry) {
    const refreshed = await refreshAccessToken();
    if (refreshed) {
      return apiFetch<T>(path, { ...options, _isRetry: true });
    }
    clearTokens();
    throw new ApiError("Your session has expired. Please sign in again.", 401);
  }

  if (!response.ok) {
    throw new ApiError(await parseErrorMessage(response), response.status);
  }

  if (response.status === 204) {
    return undefined as T;
  }

  return (await response.json()) as T;
}

export async function login(payload: LoginRequest): Promise<TokenPairResponse> {
  const data = await apiFetch<TokenPairResponse>("/v1/auth/login", {
    method: "POST",
    body: JSON.stringify(payload),
    skipAuth: true,
  });
  storeTokenPair(data);
  return data;
}

export function logout(): void {
  clearTokens();
}

export async function listDocuments(): Promise<DocumentListResponse> {
  return apiFetch<DocumentListResponse>("/v1/documents");
}

export async function uploadDocument(file: File): Promise<DocumentResponse> {
  const formData = new FormData();
  formData.append("file", file);
  return apiFetch<DocumentResponse>("/v1/documents", {
    method: "POST",
    body: formData,
  });
}

export async function deleteDocument(documentId: string): Promise<void> {
  await apiFetch<void>(`/v1/documents/${documentId}`, { method: "DELETE" });
}

export async function listConversations(): Promise<ConversationListResponse> {
  return apiFetch<ConversationListResponse>("/v1/conversations");
}

export async function createConversation(title?: string): Promise<ConversationResponse> {
  return apiFetch<ConversationResponse>("/v1/conversations", {
    method: "POST",
    body: JSON.stringify({ title: title ?? null }),
  });
}

export async function getConversation(
  conversationId: string,
): Promise<ConversationDetailResponse> {
  return apiFetch<ConversationDetailResponse>(`/v1/conversations/${conversationId}`);
}

export async function getChunk(chunkId: string): Promise<ChunkResponse> {
  return apiFetch<ChunkResponse>(`/v1/chunks/${chunkId}`);
}

export interface StreamMessageHandlers {
  /** Called once per `event: delta` frame, in arrival order, with just that
   * frame's incremental text (not the accumulated total) -- the caller
   * appends it to whatever it is already showing. */
  onDelta: (text: string) => void;
  /** Called exactly once, when the terminal `event: done` frame arrives. */
  onDone: (data: MessageDoneEvent) => void;
}

function parseSseEvent(rawEvent: string): { event: string; data: string } {
  let event = "";
  let data = "";
  for (const line of rawEvent.split("\n")) {
    if (line.startsWith("event: ")) {
      event = line.slice("event: ".length);
    } else if (line.startsWith("data: ")) {
      data = line.slice("data: ".length);
    }
  }
  return { event, data };
}

/**
 * POST /v1/conversations/{id}/messages and consume its real SSE stream,
 * forwarding each `event: delta` frame to `handlers.onDelta` the moment it
 * is decoded (not buffered until the stream ends) and finalizing via
 * `handlers.onDone` on the terminal `event: done` frame.
 *
 * A manual `fetch` + `ReadableStream` reader, not the browser's
 * `EventSource` API: `EventSource` only issues GET requests and cannot
 * attach a custom `Authorization` header, both of which this endpoint
 * requires (a POST with a JSON body, session-JWT authenticated). A raw
 * fetch reader is the practical way to get genuine incremental SSE
 * consumption under those two constraints.
 */
export async function streamMessage(
  conversationId: string,
  content: string,
  handlers: StreamMessageHandlers,
  options: { signal?: AbortSignal } = {},
): Promise<void> {
  await streamMessageAttempt(conversationId, content, handlers, options, false);
}

async function streamMessageAttempt(
  conversationId: string,
  content: string,
  handlers: StreamMessageHandlers,
  options: { signal?: AbortSignal },
  isRetry: boolean,
): Promise<void> {
  const headers = new Headers({ "Content-Type": "application/json" });
  const accessToken = getAccessToken();
  if (accessToken) headers.set("Authorization", `Bearer ${accessToken}`);

  const response = await fetch(`${API_URL}/v1/conversations/${conversationId}/messages`, {
    method: "POST",
    headers,
    body: JSON.stringify({ content } satisfies MessageCreateRequest),
    signal: options.signal,
  });

  if (response.status === 401 && !isRetry) {
    const refreshed = await refreshAccessToken();
    if (refreshed) {
      return streamMessageAttempt(conversationId, content, handlers, options, true);
    }
    clearTokens();
    throw new ApiError("Your session has expired. Please sign in again.", 401);
  }

  if (!response.ok || !response.body) {
    throw new ApiError(await parseErrorMessage(response), response.status);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let boundary = buffer.indexOf("\n\n");
    while (boundary !== -1) {
      const rawEvent = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      const { event, data } = parseSseEvent(rawEvent);
      if (event && data) {
        const parsed: unknown = JSON.parse(data);
        if (event === "delta") {
          handlers.onDelta((parsed as { text: string }).text);
        } else if (event === "done") {
          handlers.onDone(parsed as MessageDoneEvent);
        }
      }
      boundary = buffer.indexOf("\n\n");
    }
  }
}

export { getAccessToken, getStoredUser } from "./token-storage";
