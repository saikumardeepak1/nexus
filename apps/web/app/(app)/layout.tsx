import { AuthGuard } from "@/components/auth/AuthGuard";
import { AppNav } from "@/components/shell/AppNav";

export default function AppShellLayout({ children }: { children: React.ReactNode }) {
  return (
    <AuthGuard>
      <div className="grid min-h-screen grid-cols-[220px_1fr]">
        <AppNav />
        <main className="p-8">{children}</main>
      </div>
    </AuthGuard>
  );
}
