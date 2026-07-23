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
