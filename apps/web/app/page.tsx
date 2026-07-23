import { StatusBadge } from "@/components/StatusBadge";

export default function HomePage() {
  return (
    <main className="flex min-h-screen flex-col items-center justify-center gap-4 p-8">
      <h1 className="text-3xl font-semibold">Nexus</h1>
      <p className="max-w-md text-center text-sm text-gray-500">
        Enterprise knowledge intelligence platform. Upload documents and get citation-backed
        answers grounded in your own corpus.
      </p>
      <StatusBadge label="Scaffolding online" />
    </main>
  );
}
