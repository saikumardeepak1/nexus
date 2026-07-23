"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";

import { Button } from "@/components/ui/button";
import { logout } from "@/lib/api-client";
import { cn } from "@/lib/utils";

const NAV_ITEMS = [
  { href: "/documents", label: "Documents" },
  { href: "/chat", label: "Chat" },
] as const;

export function AppNav() {
  const pathname = usePathname();
  const router = useRouter();

  function handleLogout() {
    logout();
    router.replace("/login");
  }

  return (
    <nav aria-label="Primary" className="flex h-full flex-col justify-between border-r p-4">
      <div className="space-y-1">
        <p className="mb-4 px-2 text-lg font-semibold tracking-tight">Nexus</p>
        {NAV_ITEMS.map((item) => {
          const isActive = pathname?.startsWith(item.href) ?? false;
          return (
            <Link
              key={item.href}
              href={item.href}
              aria-current={isActive ? "page" : undefined}
              className={cn(
                "block rounded-md px-2 py-1.5 text-sm font-medium transition-colors",
                isActive
                  ? "bg-secondary text-secondary-foreground"
                  : "text-muted-foreground hover:bg-secondary/60 hover:text-secondary-foreground",
              )}
            >
              {item.label}
            </Link>
          );
        })}
      </div>
      <Button variant="outline" size="sm" onClick={handleLogout}>
        Log out
      </Button>
    </nav>
  );
}
