import type { Metadata } from "next";

import { DocumentList } from "@/components/documents/DocumentList";
import { DocumentUploadForm } from "@/components/documents/DocumentUploadForm";

export const metadata: Metadata = {
  title: "Documents | Nexus",
};

export default function DocumentsPage() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">Document library</h1>
        <p className="text-sm text-muted-foreground">
          Upload a PDF or text file and track its ingestion status.
        </p>
      </div>
      <DocumentUploadForm />
      <DocumentList />
    </div>
  );
}
