/**
 * Shared design tokens and primitives.
 *
 * Scope is deliberately narrow — colour, spacing, type, and the handful of
 * primitives a data-dense page needs. ADR 0023 warns about the failure mode this
 * package invites: it is being designed before its second consumer (the Phase 3
 * chat app) exists, and a component library built for an imagined consumer
 * becomes the thing everyone works around. So nothing chat-shaped lives here, and
 * nothing arrives until a real screen needs it.
 *
 * Consumers import `@llmp/ui/tokens.css` once, then components as needed. There
 * is no build step: each component is compiled by the consumer's bundler. Since
 * ADR 0047 the components are styled with Tailwind utilities against the tokens
 * file's `@theme inline` mapping, so a consumer of the *components* runs
 * Tailwind (one Vite plugin, one CSS import); a consumer of only the *tokens*
 * needs nothing beyond the CSS import.
 */

export { Badge } from "./components/Badge";
export type { BadgeProps } from "./components/Badge";

export { Button } from "./components/Button";
export type { ButtonProps, ButtonVariant } from "./components/Button";

export { Card } from "./components/Card";
export type { CardProps } from "./components/Card";

export { Dialog } from "./components/Dialog";
export type { DialogProps } from "./components/Dialog";

export { Select } from "./components/Field";
export type { SelectProps } from "./components/Field";

export { Input } from "./components/Input";
export type { InputProps } from "./components/Input";

export {
  MenuCheckboxItem,
  MenuContent,
  MenuItem,
  MenuLabel,
  MenuRoot,
  MenuSection,
  MenuSeparator,
  MenuTrigger,
} from "./components/Menu";
export type { MenuContentProps, MenuSectionProps } from "./components/Menu";

export { Meter } from "./components/Meter";
export type { MeterProps } from "./components/Meter";

export { DISPLAY_DECIMALS, Money, formatMoney } from "./components/Money";
export type { FormatMoneyOptions, MoneyProps } from "./components/Money";

export {
  MoneyPrecisionContext,
  MoneyPrecisionProvider,
  useExactMoney,
} from "./components/MoneyPrecision";
export type { MoneyPrecisionProviderProps } from "./components/MoneyPrecision";

export { Notice } from "./components/Notice";
export type { NoticeProps } from "./components/Notice";

export { Pagination } from "./components/Pagination";
export type { PaginationProps } from "./components/Pagination";

export { Skeleton } from "./components/Skeleton";
export type { SkeletonProps } from "./components/Skeleton";

export { Spinner } from "./components/Spinner";
export type { SpinnerProps } from "./components/Spinner";

export { Stat } from "./components/Stat";
export type { StatProps } from "./components/Stat";

export { Table } from "./components/Table";
export type { Column, TableProps } from "./components/Table";

export { ToastProvider, useToast } from "./components/Toast";

export { Tooltip } from "./components/Tooltip";
export type { TooltipProps } from "./components/Tooltip";
