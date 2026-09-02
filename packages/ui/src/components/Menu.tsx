import { Menu as BaseMenu } from "@base-ui/react/menu";
import type { ComponentProps, ReactNode } from "react";
import { cx } from "../cx";

/*
 * These wrappers accept a plain-string `className` only. Base UI's own
 * `className` may be a function of component state, but these parts already
 * encode the state styling (`data-[highlighted]`, `data-[popup-open]`); a
 * caller that genuinely needs state-dependent classes composes the underlying
 * `@base-ui/react/menu` parts directly, which is what they are there for.
 */
type StringClassProps<P> = Omit<P, "className"> & { className?: string };

export const MenuRoot = BaseMenu.Root;
export const MenuTrigger = BaseMenu.Trigger;

export interface MenuContentProps extends StringClassProps<ComponentProps<typeof BaseMenu.Popup>> {
  /** Distance from the trigger, in px. */
  sideOffset?: number;
  /** Which edge of the trigger to line up against. Defaults to the end. */
  align?: ComponentProps<typeof BaseMenu.Positioner>["align"];
}

/**
 * The floating body of a menu: Portal → Positioner → Popup, pre-styled.
 *
 * Exported as parts rather than one closed component because the trigger is
 * nearly always bespoke (the Shell's identity button, a row's action button)
 * and composing it from the caller's markup is the whole point of a headless
 * menu. What is common — the surface, the shadow, the motion — lives here.
 */
export function MenuContent({
  className,
  sideOffset = 6,
  align = "end",
  children,
  ...rest
}: MenuContentProps) {
  return (
    <BaseMenu.Portal>
      <BaseMenu.Positioner sideOffset={sideOffset} align={align} className="z-50 outline-none">
        <BaseMenu.Popup
          className={cx(
            "min-w-56 rounded-md border border-line bg-raised p-1.5 shadow-md outline-none",
            "transition-opacity duration-100",
            "data-[ending-style]:opacity-0 data-[starting-style]:opacity-0",
            className,
          )}
          {...rest}
        >
          {children}
        </BaseMenu.Popup>
      </BaseMenu.Positioner>
    </BaseMenu.Portal>
  );
}

export function MenuItem({
  className,
  ...rest
}: StringClassProps<ComponentProps<typeof BaseMenu.Item>>) {
  return (
    <BaseMenu.Item
      className={cx(
        "flex cursor-pointer items-center gap-2 rounded-sm px-3 py-2 text-sm text-ink",
        "outline-none data-[highlighted]:bg-sunken",
        className,
      )}
      {...rest}
    />
  );
}

export function MenuCheckboxItem({
  className,
  children,
  ...rest
}: StringClassProps<ComponentProps<typeof BaseMenu.CheckboxItem>>) {
  return (
    <BaseMenu.CheckboxItem
      className={cx(
        "flex cursor-pointer items-center gap-2 rounded-sm px-3 py-2 text-sm text-ink",
        "outline-none data-[highlighted]:bg-sunken",
        className,
      )}
      {...rest}
    >
      {/* The indicator slot is a fixed width whether or not it draws, so the
          checked and unchecked labels line up. */}
      <BaseMenu.CheckboxItemIndicator className="flex w-4 shrink-0 items-center justify-center text-accent">
        <svg
          aria-hidden="true"
          viewBox="0 0 16 16"
          className="size-3.5"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <path d="m3 8.5 3.5 3.5L13 4.5" />
        </svg>
      </BaseMenu.CheckboxItemIndicator>
      {children}
    </BaseMenu.CheckboxItem>
  );
}

/** A quiet divider between groups of items. */
export function MenuSeparator({
  className,
  ...rest
}: StringClassProps<ComponentProps<typeof BaseMenu.Separator>>) {
  return <BaseMenu.Separator className={cx("-mx-1.5 my-1.5 h-px bg-line-quiet", className)} {...rest} />;
}

/** A non-interactive line naming a group; the menu equivalent of a `<label>`. */
export function MenuLabel({
  className,
  ...rest
}: StringClassProps<ComponentProps<typeof BaseMenu.GroupLabel>>) {
  return (
    <BaseMenu.GroupLabel
      className={cx("px-3 py-2 text-xs text-ink-faint", className)}
      {...rest}
    />
  );
}

export interface MenuSectionProps {
  label?: ReactNode;
  children: ReactNode;
}

/** A labelled group of items. `label` renders through `MenuLabel`. */
export function MenuSection({ label, children }: MenuSectionProps) {
  return (
    <BaseMenu.Group>
      {label && <MenuLabel>{label}</MenuLabel>}
      {children}
    </BaseMenu.Group>
  );
}
