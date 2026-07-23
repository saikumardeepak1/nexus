type StatusBadgeProps = {
  label: string;
};

export function StatusBadge({ label }: StatusBadgeProps) {
  return (
    <span className="rounded-full bg-emerald-100 px-3 py-1 text-sm font-medium text-emerald-800">
      {label}
    </span>
  );
}
