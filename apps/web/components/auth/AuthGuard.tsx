"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { getAccessToken } from "@/lib/token-storage";

type AuthStatus = "checking" | "authenticated";

/**
 * Client-side route gate. Tokens live in localStorage (see the tradeoff note
 * in lib/token-storage.ts), which is invisible to Next.js middleware/edge
 * functions, so the redirect happens here after mount rather than at the
 * server/middleware layer. Renders nothing but a loading state until the
 * check runs, so protected content never flashes for an unauthenticated
 * visitor.
 */
export function AuthGuard({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const [status, setStatus] = useState<AuthStatus>("checking");

  useEffect(() => {
    if (getAccessToken()) {
      setStatus("authenticated");
    } else {
      router.replace("/login");
    }
  }, [router]);

  if (status === "checking") {
    return (
      <div className="flex min-h-screen items-center justify-center text-sm text-muted-foreground">
        Loading...
      </div>
    );
  }

  return <>{children}</>;
}
