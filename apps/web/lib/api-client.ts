import { clearTokens, getAccessToken, getRefreshToken, storeTokenPair } from "./token-storage";
import type {
  DocumentListResponse,
  DocumentResponse,
  LoginRequest,
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

export { getAccessToken, getStoredUser } from "./token-storage";
