import { createContext, useContext } from "react";

/**
 * Every word the shared components say on their own.
 *
 * A component here is also a sentence-maker: a dialog's close button, a pager's
 * "Next", a spinner's default label. Those words cannot be passed by the screen
 * that uses the component without every one of them (22 files in the console)
 * repeating the same four messages, so they come through a context instead,
 * filled once at the root by the consumer from its own catalogue. This package
 * does not know which i18n library the consumer uses and does not import one.
 *
 * Without a provider the English below is used, so a consumer that has not
 * adopted this (and every test that renders a component bare) keeps working.
 * A prop on a component (`label`, `empty`, `noun`) still wins over the context:
 * the screen that knows its own words says them.
 */
export interface UiMessages {
  /** Dialog close button, announced and tooltip. */
  close: string;
  /** Toast dismiss button. */
  dismiss: string;
  /** Spinner label when the screen gives none; the component appends the ellipsis. */
  loading: string;
  /** Table body when there are no rows and the screen gave no words. */
  tableEmpty: string;
  /** The pager's landmark name. */
  paginationLabel: string;
  paginationPrevious: string;
  paginationNext: string;
  /** Plural noun for the count when the screen names none. */
  paginationRows: string;
  /** "No users": the pager's range when nothing matched. */
  paginationNone: (noun: string) => string;
  /** "of 812 users": what follows the bold "1–50" range. */
  paginationOf: (total: string, noun: string) => string;
}

export const DEFAULT_UI_MESSAGES: UiMessages = {
  close: "Close",
  dismiss: "Dismiss",
  loading: "Loading",
  tableEmpty: "Nothing to show.",
  paginationLabel: "Pagination",
  paginationPrevious: "Previous",
  paginationNext: "Next",
  paginationRows: "rows",
  paginationNone: (noun) => `No ${noun}`,
  paginationOf: (total, noun) => `of ${total} ${noun}`,
};

export const UiMessagesContext = createContext<UiMessages>(DEFAULT_UI_MESSAGES);

export function useUiMessages(): UiMessages {
  return useContext(UiMessagesContext);
}

export interface UiMessagesProviderProps {
  messages: UiMessages;
  children: React.ReactNode;
}

/**
 * Mount once, inside whatever re-renders when the language changes. The
 * messages are read at render, so a provider whose functions call into the
 * consumer's catalogue follows the active locale with no further wiring.
 */
export function UiMessagesProvider({ messages, children }: UiMessagesProviderProps) {
  return <UiMessagesContext.Provider value={messages}>{children}</UiMessagesContext.Provider>;
}
