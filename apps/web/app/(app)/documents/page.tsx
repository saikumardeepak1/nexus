import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "Documents | Nexus",
};

export default function DocumentsPage() {
  return (
    <div className="flex min-h-[60vh] flex-col items-center justify-center gap-2 text-center">
      <h1 className="text-xl font-semibold">Document library</h1>
      <p className="max-w-sm text-sm text-muted-foreground">
        No documents yet. Upload and ingestion status will land here in a future update.
      </p>
    </div>
  );
}
