import type { TokenPairResponse, UserResponse } from "./types";

/**
 * Token storage tradeoff (see PR description for the full writeup):
 *
 * The access/refresh token pair is kept in localStorage rather than an
 * httpOnly cookie. An httpOnly cookie is the more secure option (immune to
 * XSS-driven token theft), but setting one requires a server that can issue
 * it, and this app has no Next.js backend-for-frontend yet: the web app
 * talks straight to the FastAPI service over NEXT_PUBLIC_API_URL, so there is
 * no same-origin route handler to set/read a cookie from. localStorage is
 * the pragmatic choice for this issue, with the known risk that any XSS on
 * this origin can read both tokens. Follow-up work should introduce a
 * Next.js route handler proxy that sets an httpOnly, SameSite=strict cookie
 * and have middleware gate routes off of it instead of this client-side
 * check.
 */

const ACCESS_TOKEN_KEY = "nexus.access_token";
const REFRESH_TOKEN_KEY = "nexus.refresh_token";
const USER_KEY = "nexus.user";

function isBrowser(): boolean {
  return typeof window !== "undefined";
}

export function getAccessToken(): string | null {
  if (!isBrowser()) return null;
  return window.localStorage.getItem(ACCESS_TOKEN_KEY);
}

export function getRefreshToken(): string | null {
  if (!isBrowser()) return null;
  return window.localStorage.getItem(REFRESH_TOKEN_KEY);
}

export function getStoredUser(): UserResponse | null {
  if (!isBrowser()) return null;
  const raw = window.localStorage.getItem(USER_KEY);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as UserResponse;
  } catch {
    return null;
  }
}

export function storeTokenPair(tokenPair: TokenPairResponse): void {
  if (!isBrowser()) return;
  window.localStorage.setItem(ACCESS_TOKEN_KEY, tokenPair.access_token);
  window.localStorage.setItem(REFRESH_TOKEN_KEY, tokenPair.refresh_token);
  window.localStorage.setItem(USER_KEY, JSON.stringify(tokenPair.user));
}

export function clearTokens(): void {
  if (!isBrowser()) return;
  window.localStorage.removeItem(ACCESS_TOKEN_KEY);
  window.localStorage.removeItem(REFRESH_TOKEN_KEY);
  window.localStorage.removeItem(USER_KEY);
}
