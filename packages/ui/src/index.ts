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
 * is no build step and no configuration: each component brings its own scoped
 * CSS, so the package works in any bundler that understands CSS Modules.
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

export { Meter } from "./components/Meter";
export type { MeterProps } from "./components/Meter";

export { Money, formatMoney } from "./components/Money";
export type { MoneyProps } from "./components/Money";

export { Notice } from "./components/Notice";
export type { NoticeProps } from "./components/Notice";

export { Pagination } from "./components/Pagination";
export type { PaginationProps } from "./components/Pagination";

export { Spinner } from "./components/Spinner";
export type { SpinnerProps } from "./components/Spinner";

export { Stat } from "./components/Stat";
export type { StatProps } from "./components/Stat";

export { Table } from "./components/Table";
export type { Column, TableProps } from "./components/Table";
