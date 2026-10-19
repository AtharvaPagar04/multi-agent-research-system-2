import type { ReactNode } from "react";

export function Panel({
  children,
  className = "",
  as: Tag = "section",
}: {
  children: ReactNode;
  className?: string;
  as?: "section" | "div" | "article" | "aside";
}) {
  return (
    <Tag
      className={`rounded-lg border border-zinc-800/90 bg-[#0B0B0B]/90 shadow-[0_0_0_1px_rgba(255,255,255,0.015),0_12px_40px_rgba(0,0,0,0.35)] ${className}`}
    >
      {children}
    </Tag>
  );
}

export function PanelHeader({
  title,
  description,
  actions,
  id,
}: {
  title: string;
  description?: string;
  actions?: ReactNode;
  id?: string;
}) {
  return (
    <div className="flex flex-col gap-3 border-b border-zinc-800/80 px-5 py-4 sm:flex-row sm:items-start sm:justify-between">
      <div className="min-w-0">
        <h2 id={id} className="text-sm font-medium tracking-wide text-zinc-100">
          {title}
        </h2>
        {description ? (
          <p className="mt-1 max-w-3xl text-[13px] leading-relaxed text-zinc-400">
            {description}
          </p>
        ) : null}
      </div>
      {actions ? <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div> : null}
    </div>
  );
}

export function PanelBody({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  return <div className={`px-5 py-4 ${className}`}>{children}</div>;
}

export function StatCard({
  label,
  value,
  hint,
}: {
  label: string;
  value: string;
  hint?: string;
}) {
  return (
    <div className="rounded-lg border border-zinc-800/80 bg-[#111111]/80 px-4 py-3">
      <div className="font-mono text-[11px] uppercase tracking-[0.14em] text-zinc-500">
        {label}
      </div>
      <div className="mt-2 font-mono text-xl text-zinc-100">{value}</div>
      {hint ? <div className="mt-1 text-[12px] text-zinc-500">{hint}</div> : null}
    </div>
  );
}
