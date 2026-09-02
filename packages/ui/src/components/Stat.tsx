import type { ReactNode } from "react";
import { cx } from "../cx";

export interface StatProps {
  label: ReactNode;
  value: ReactNode;
  /** Smaller line under the value: a period, a comparison, a count. */
  detail?: ReactNode;
  tone?: "neutral" | "ok" | "warn" | "danger";
}

const TONES: Record<NonNullable<StatProps["tone"]>, string> = {
  neutral: "text-ink",
  ok: "text-ok",
  warn: "text-warn",
  danger: "text-danger",
};

/** One headline figure. Money and counts render with tabular figures. */
export function Stat({ label, value, detail, tone = "neutral" }: StatProps) {
  return (
    <div className="flex flex-col gap-1">
      <div className="text-xs font-medium tracking-[0.01em] text-ink-muted">{label}</div>
      <div className={cx("text-xl font-semibold leading-tight tabular-nums", TONES[tone])}>
        {value}
      </div>
      {detail && <div className="text-sm text-ink-muted">{detail}</div>}
    </div>
  );
}
