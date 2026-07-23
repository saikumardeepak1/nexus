"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useRef, useState, type FormEvent } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ApiError, uploadDocument } from "@/lib/api-client";
import { DOCUMENTS_QUERY_KEY } from "@/lib/documents";
import type { DocumentListResponse } from "@/lib/types";

export function DocumentUploadForm() {
  const queryClient = useQueryClient();
  const [file, setFile] = useState<File | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const mutation = useMutation({
    mutationFn: (uploadFile: File) => uploadDocument(uploadFile),
    onSuccess: (document) => {
      // Write the newly-created document (status=queued) straight into the
      // shared cache rather than invalidating and refetching, so it shows
      // up immediately without a network round trip -- and so it appears
      // even if the list's polling happens to be paused (nothing to poll
      // for yet, or everything else already reached a terminal state).
      queryClient.setQueryData<DocumentListResponse>(DOCUMENTS_QUERY_KEY, (old) => {
        const documents = old?.documents ?? [];
        if (documents.some((existing) => existing.id === document.id)) return old;
        return { documents: [document, ...documents] };
      });
      setFile(null);
      if (inputRef.current) inputRef.current.value = "";
    },
  });

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!file) return;
    mutation.mutate(file);
  }

  const errorMessage =
    mutation.error instanceof ApiError
      ? mutation.error.message
      : mutation.isError
        ? "Something went wrong. Please try again."
        : null;

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-3 sm:flex-row sm:items-end">
      <div className="flex-1 space-y-2">
        <Label htmlFor="document-file">Upload a document</Label>
        <Input
          id="document-file"
          name="file"
          type="file"
          ref={inputRef}
          accept=".pdf,.txt,application/pdf,text/plain"
          onChange={(event) => setFile(event.target.files?.[0] ?? null)}
        />
      </div>
      <Button type="submit" disabled={!file || mutation.isPending}>
        {mutation.isPending ? "Uploading..." : "Upload"}
      </Button>
      {errorMessage ? (
        <p role="alert" className="text-sm font-medium text-destructive">
          {errorMessage}
        </p>
      ) : null}
    </form>
  );
}
