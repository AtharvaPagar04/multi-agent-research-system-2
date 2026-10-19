type Segment = {
  label: string;
  value: number;
  className: string;
};

export function DistributionBar({
  segments,
  total,
}: {
  segments: Segment[];
  total: number;
}) {
  const safeTotal = total > 0 ? total : 0;
  const visible = segments.filter((segment) => segment.value > 0);

  if (safeTotal === 0 || visible.length === 0) {
    return (
      <div className="h-2 w-full overflow-hidden rounded-full border border-zinc-800 bg-zinc-950">
        <div className="h-full w-full bg-zinc-900" />
      </div>
    );
  }

  return (
    <div
      className="flex h-2 w-full overflow-hidden rounded-full border border-zinc-800 bg-zinc-950"
      role="img"
      aria-label={segments
        .map((segment) => `${segment.label}: ${segment.value}`)
        .join(", ")}
    >
      {visible.map((segment) => (
        <div
          key={segment.label}
          className={segment.className}
          style={{ width: `${(segment.value / safeTotal) * 100}%` }}
          title={`${segment.label}: ${segment.value}`}
        />
      ))}
    </div>
  );
}
