import { cx } from "../cx";

export interface SkeletonProps {
  className?: string;
}

/**
 * A placeholder block for content that is loading in place.
 *
 * Paired with `Spinner` by role: the spinner says "this panel is working",
 * the skeleton says "this exact region will be a table of this shape".
 * Wire frames beat spinners wherever the layout is already known.
 */
export function Skeleton({ className }: SkeletonProps) {
  return <div aria-hidden="true" className={cx("animate-pulse rounded-md bg-sunken", className)} />;
}
