import type { ReactNode } from "react";

export interface PageHeaderProps {
  title: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
  /** A section of a page that has its own heading above it: an h2, not the title. */
  level?: "page" | "section";
}

export function PageHeader({ title, subtitle, actions, level = "page" }: PageHeaderProps) {
  return (
    <header className="flex flex-wrap items-end justify-between gap-4">
      <div>
        {level === "section" ? (
          <h2 className="text-heading font-semibold leading-tight">{title}</h2>
        ) : (
          <h1 className="text-title font-bold leading-tight tracking-[-0.02em]">{title}</h1>
        )}
        {subtitle && <p className="mt-1 max-w-[60ch] text-body text-ink-muted">{subtitle}</p>}
      </div>
      {actions && <div className="flex items-center gap-2">{actions}</div>}
    </header>
  );
}
