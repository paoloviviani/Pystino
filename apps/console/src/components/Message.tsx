import { Fragment } from "react";
import type { ReactNode } from "react";
import type { MessagePart } from "../paraglide/runtime.js";

/**
 * Renders a message that carries markup.
 *
 * A message with a link, a `<code>` span or a bold phrase in it is written as
 * one string with the tag inside it — `{#link}Back to your usage{/link}` — so
 * that a translator can move the tag anywhere in the sentence. Paraglide
 * compiles such a message into parts rather than a string, and this turns the
 * parts into React, which the app has to own: only this side knows that `link`
 * is react-router's `Link` and that `code` means the typography for a command.
 *
 * The alternative — splitting the sentence into fragments the screen lays out
 * in English order — is the mistake this exists to prevent, because it makes
 * word order a property of the JSX instead of the catalogue.
 */

/** One renderer per tag a message may use, keyed by the tag name in the message. */
export type MarkupRenderers = Record<string, (children: ReactNode) => ReactNode>;

/**
 * `inputs` is required for a message that has inputs and absent for one that
 * has none, which is what `keyof` is for: the compiler types every message
 * function's inputs as optional, so a screen could otherwise compile a
 * sentence with a missing placeholder in it and find out from a reader.
 */
export type MessageProps<TInputs> = {
  /** A compiled message: `m["not_found.body"]`. */
  message: { parts: (inputs?: TInputs, options?: never) => MessagePart[] };
  markup: MarkupRenderers;
} & (keyof TInputs extends never ? { inputs?: TInputs } : { inputs: TInputs });

export function Message<TInputs>({ message, markup, inputs }: MessageProps<TInputs>) {
  const parts = message.parts(inputs);
  const nodes: ReactNode[] = [];
  // The tags currently open, innermost last: each collects the text between its
  // own start and end so it can be handed to the renderer as children.
  const open: { name: string; children: ReactNode[] }[] = [];

  const emit = (node: ReactNode) => {
    const innermost = open[open.length - 1];
    if (innermost) innermost.children.push(node);
    else nodes.push(node);
  };

  for (const part of parts) {
    if (part.type === "text") {
      emit(part.value);
      continue;
    }
    const render = markup[part.name];
    // A tag this screen does not render is dropped rather than thrown on: the
    // words around it are still the message, and the English catalogue has
    // been checked, so a missing renderer is a bug in the screen and not a
    // reason to take the page down.
    if (render === undefined) continue;
    if (part.type === "markup-end") {
      const index = open.findLastIndex((tag) => tag.name === part.name);
      if (index === -1) continue;
      const [closed] = open.splice(index);
      if (closed === undefined) continue;
      emit(render(closed.children));
      continue;
    }
    if (part.type === "markup-standalone") {
      emit(render(undefined));
      continue;
    }
    open.push({ name: part.name, children: [] });
  }

  // Tags a translation left open. Their text is still the reader's, so it is
  // shown unwrapped rather than swallowed.
  for (const tag of [...open].reverse()) nodes.push(markup[tag.name]?.(tag.children) ?? tag.children);

  return <Fragment>{nodes}</Fragment>;
}
