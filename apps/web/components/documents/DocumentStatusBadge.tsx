import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

const STATUS_STYLES: Record<string, string> = {
  queued: "border-transparent bg-slate-100 text-slate-700",
  processing: "border-transparent bg-blue-100 text-blue-800",
  ready: "border-transparent bg-emerald-100 text-emerald-800",
  failed: "border-transparent bg-red-100 text-red-800",
};

const STATUS_LABELS: Record<string, string> = {
  queued: "Queued",
  processing: "Processing",
  ready: "Ready",
  failed: "Failed",
};

const FALLBACK_STYLE = "border-transparent bg-muted text-muted-foreground";

type DocumentStatusBadgeProps = {
  status: string;
};

/**
 * Colored status badge for a document's ingestion status. Falls back to a
 * neutral style (rather than crashing) for any status value the frontend
 * doesn't recognize, so an unanticipated backend status still renders
 * something instead of breaking the row.
 */
export function DocumentStatusBadge({ status }: DocumentStatusBadgeProps) {
  const label = STATUS_LABELS[status] ?? status;
  return (
    <Badge className={cn(STATUS_STYLES[status] ?? FALLBACK_STYLE)} data-status={status}>
      {label}
    </Badge>
  );
}
