import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, apiFetch, deleteDocument, listDocuments, login, uploadDocument } from "./api-client";

const originalFetch = global.fetch;

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const sampleUser = { id: "1", organization_id: "org-1", email: "a@b.com", role: "admin" };

describe("api-client", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  afterEach(() => {
    global.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  it("login stores the returned token pair in localStorage", async () => {
    global.fetch = vi.fn().mockResolvedValue(
      jsonResponse({
        access_token: "access-1",
        refresh_token: "refresh-1",
        token_type: "bearer",
        expires_in: 900,
        user: sampleUser,
      }),
    );

    await login({ email: "a@b.com", password: "password123" });

    expect(window.localStorage.getItem("nexus.access_token")).toBe("access-1");
    expect(window.localStorage.getItem("nexus.refresh_token")).toBe("refresh-1");
  });

  it("throws an ApiError with the backend's detail message on a failed login", async () => {
    global.fetch = vi.fn().mockResolvedValue(jsonResponse({ detail: "Invalid email or password" }, 401));

    await expect(login({ email: "a@b.com", password: "wrong" })).rejects.toMatchObject({
      message: "Invalid email or password",
      status: 401,
    });
  });

  it("retries a request exactly once after a silent refresh on a 401", async () => {
    window.localStorage.setItem("nexus.access_token", "expired");
    window.localStorage.setItem("nexus.refresh_token", "refresh-1");

    const fetchMock = vi
      .fn()
      // 1: the original protected request comes back unauthorized
      .mockResolvedValueOnce(jsonResponse({ detail: "Not authenticated" }, 401))
      // 2: the silent refresh succeeds with a new pair
      .mockResolvedValueOnce(
        jsonResponse({
          access_token: "access-2",
          refresh_token: "refresh-2",
          token_type: "bearer",
          expires_in: 900,
          user: sampleUser,
        }),
      )
      // 3: the retried request succeeds
      .mockResolvedValueOnce(jsonResponse({ ok: true }));
    global.fetch = fetchMock;

    const result = await apiFetch<{ ok: boolean }>("/v1/documents");

    expect(result).toEqual({ ok: true });
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(window.localStorage.getItem("nexus.access_token")).toBe("access-2");
  });

  it("clears tokens and throws when the refresh also fails", async () => {
    window.localStorage.setItem("nexus.access_token", "expired");
    window.localStorage.setItem("nexus.refresh_token", "expired-refresh");

    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ detail: "Not authenticated" }, 401))
      .mockResolvedValueOnce(jsonResponse({ detail: "Invalid or expired refresh token" }, 401));
    global.fetch = fetchMock;

    await expect(apiFetch("/v1/documents")).rejects.toBeInstanceOf(ApiError);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(window.localStorage.getItem("nexus.access_token")).toBeNull();
    expect(window.localStorage.getItem("nexus.refresh_token")).toBeNull();
  });

  it("listDocuments GETs /v1/documents", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ documents: [] }));
    global.fetch = fetchMock;

    const result = await listDocuments();

    expect(result).toEqual({ documents: [] });
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toContain("/v1/documents");
    expect(init.method ?? "GET").toBe("GET");
  });

  it("uploadDocument POSTs the file as multipart form data without forcing a JSON content-type", async () => {
    const document = {
      id: "doc-1",
      organization_id: "org-1",
      filename: "policy.pdf",
      status: "queued",
      page_count: null,
      error_detail: null,
      created_at: "2026-07-23T00:00:00Z",
    };
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(document, 201));
    global.fetch = fetchMock;

    const file = new File(["hello"], "policy.pdf", { type: "application/pdf" });
    const result = await uploadDocument(file);

    expect(result).toEqual(document);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toContain("/v1/documents");
    expect(init.method).toBe("POST");
    expect(init.body).toBeInstanceOf(FormData);
    const headers = new Headers(init.headers);
    expect(headers.get("Content-Type")).toBeNull();
  });

  it("deleteDocument DELETEs /v1/documents/{id}", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    global.fetch = fetchMock;

    await deleteDocument("doc-1");

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toContain("/v1/documents/doc-1");
    expect(init.method).toBe("DELETE");
  });
});
