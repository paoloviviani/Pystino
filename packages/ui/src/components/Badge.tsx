import type { ReactNode } from "react";
import styles from "./Badge.module.css";

export interface BadgeProps {
  tone?: "neutral" | "accent" | "ok" | "warn" | "danger";
  children: ReactNode;
}

export function Badge({ tone = "neutral", children }: BadgeProps) {
  return <span className={[styles.badge, styles[tone]].join(" ")}>{children}</span>;
}
