"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { DocumentStatusBadge } from "@/components/documents/DocumentStatusBadge";
import { Button } from "@/components/ui/button";
import { ApiError, deleteDocument, listDocuments } from "@/lib/api-client";
import { DOCUMENTS_QUERY_KEY, documentsRefetchInterval } from "@/lib/documents";
import type { DocumentListResponse } from "@/lib/types";

export function DocumentList() {
  const queryClient = useQueryClient();
  const [deletingId, setDeletingId] = useState<string | null>(null);

  const query = useQuery({
    queryKey: DOCUMENTS_QUERY_KEY,
    queryFn: listDocuments,
    refetchInterval: (currentQuery) => documentsRefetchInterval(currentQuery.state.data?.documents),
  });

  const deleteMutation = useMutation({
    mutationFn: (documentId: string) => deleteDocument(documentId),
    onMutate: (documentId: string) => setDeletingId(documentId),
    onSuccess: (_result, documentId) => {
      queryClient.setQueryData<DocumentListResponse>(DOCUMENTS_QUERY_KEY, (old) =>
        old ? { documents: old.documents.filter((document) => document.id !== documentId) } : old,
      );
    },
    onSettled: () => setDeletingId(null),
  });

  if (query.isLoading) {
    return <p className="text-sm text-muted-foreground">Loading documents...</p>;
  }

  if (query.isError) {
    const message =
      query.error instanceof ApiError ? query.error.message : "Failed to load documents.";
    return (
      <p role="alert" className="text-sm font-medium text-destructive">
        {message}
      </p>
    );
  }

  const documents = query.data?.documents ?? [];

  if (documents.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">
        No documents yet. Upload one to get started.
      </p>
    );
  }

  return (
    <ul className="divide-y rounded-md border">
      {documents.map((document) => (
        <li key={document.id} className="flex items-center justify-between gap-4 px-4 py-3">
          <div className="min-w-0">
            <p className="truncate text-sm font-medium">{document.filename}</p>
            {document.status === "failed" && document.error_detail ? (
              <p
                className="mt-1 text-xs text-destructive"
                title={document.error_detail}
              >
                {document.error_detail}
              </p>
            ) : null}
          </div>
          <div className="flex shrink-0 items-center gap-3">
            <DocumentStatusBadge status={document.status} />
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => deleteMutation.mutate(document.id)}
              disabled={deletingId === document.id}
              aria-label={`Delete ${document.filename}`}
            >
              {deletingId === document.id ? "Deleting..." : "Delete"}
            </Button>
          </div>
        </li>
      ))}
    </ul>
  );
}
