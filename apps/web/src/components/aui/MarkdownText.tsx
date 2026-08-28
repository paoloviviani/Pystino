/**
 * A message body, rendered as markdown.
 *
 * The class names are assistant-ui's own — `aui-md-h1`, `aui-md-pre` and the
 * rest — because `@assistant-ui/styles` is their components compiled out of
 * Tailwind, and the compiled rules are keyed to exactly those names. Naming an
 * element anything else means it arrives unstyled, which is what the whole of
 * this chat looked like before: their behaviour, none of their appearance.
 *
 * `remark-gfm` for tables, strikethrough and task lists — the parts of markdown
 * a model actually emits that CommonMark does not cover.
 *
 * **Maths is not optional here.** A reasoning model writes LaTeX constantly:
 * ask one for 17 × 23 and the answer comes back full of `$ 17 \times 23 $` and
 * `$$ … $$`, which without KaTeX renders as exactly those characters. Three
 * pieces make it work, and the middle one is the part that is easy to miss:
 *
 * - `remark-math` finds `$…$` and `$$…$$`, and `rehype-katex` typesets them;
 * - **`preprocess` normalises what models actually emit first.** They write
 *   `\(…\)` and `\[…\]` at least as often as dollars, and remark-math knows
 *   only the dollar form. assistant-ui ships the rewrite, and it is
 *   streaming-safe — it runs over the whole accumulated text each time rather
 *   than over a fragment that might cut a delimiter in half;
 * - `escapeCurrencyDollars` runs before it, so "it costs $5 to $7" is not read
 *   as a maths span swallowing the words between. On a platform whose console
 *   is full of prices, that one earns its place.
 */

import {
  MarkdownTextPrimitive,
  escapeCurrencyDollars,
  normalizeMathDelimiters,
  unstable_memoizeMarkdownComponents as memoizeMarkdownComponents,
} from "@assistant-ui/react-markdown";
import { memo } from "react";
import rehypeKatex from "rehype-katex";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";

const components = memoizeMarkdownComponents({
  h1: (props) => <h1 className="aui-md-h1" {...props} />,
  h2: (props) => <h2 className="aui-md-h2" {...props} />,
  h3: (props) => <h3 className="aui-md-h3" {...props} />,
  h4: (props) => <h4 className="aui-md-h4" {...props} />,
  h5: (props) => <h5 className="aui-md-h5" {...props} />,
  h6: (props) => <h6 className="aui-md-h6" {...props} />,
  p: (props) => <p className="aui-md-p" {...props} />,
  // `noreferrer` as well as `noopener`: a model's output is not a source we
  // want leaking this console's URL to.
  a: (props) => <a className="aui-md-a" target="_blank" rel="noopener noreferrer" {...props} />,
  blockquote: (props) => <blockquote className="aui-md-blockquote" {...props} />,
  ul: (props) => <ul className="aui-md-ul" {...props} />,
  ol: (props) => <ol className="aui-md-ol" {...props} />,
  li: (props) => <li className="aui-md-li" {...props} />,
  hr: (props) => <hr className="aui-md-hr" {...props} />,
  table: (props) => <table className="aui-md-table" {...props} />,
  th: (props) => <th className="aui-md-th" {...props} />,
  td: (props) => <td className="aui-md-td" {...props} />,
  tr: (props) => <tr className="aui-md-tr" {...props} />,
  sup: (props) => <sup className="aui-md-sup" {...props} />,
  pre: (props) => <pre className="aui-md-pre" {...props} />,
  code: (props) => <code className="aui-md-inline-code" {...props} />,
});

/**
 * Put a multi-line display block's delimiters on their own lines.
 *
 * The case assistant-ui's helpers do not cover, and not academic — it is what a
 * model emits the moment it shows long multiplication or a matrix. `\[ … \]`
 * normalises to `$$ … $$` with the content starting on the opening line, and
 * micromark reads that first line as the block's **meta**, exactly as it would
 * a code fence's language. So `$$\begin{array}{r}` silently loses the
 * `\begin{array}{r}`, and KaTeX is handed a body that opens with `\hline` and
 * fails with "valid only within array environment".
 *
 * Measured on the three shapes:
 *
 *   $$17 \times 23$$                    one line          → parses
 *   $$\begin{array}… \end{array}$$       content on opener → fails
 *   $$\n\begin{array}… \end{array}\n$$   own lines         → parses
 *
 * **Split rather than match.** The obvious `/\$\$(.*?)\$\$/` is wrong and was
 * written here first: scanning left to right it happily pairs the *closing*
 * `$$` of one block with the *opening* `$$` of the next, and rewrites the
 * sentence between them as maths — which is how a paragraph of prose ends up
 * inside a fence and the block after it loses its first line. Splitting on the
 * delimiter keeps the pairing positional, which is what it actually is.
 *
 * Streaming-safe: an unterminated trailing `$$` has no partner, so it is passed
 * through untouched rather than rewritten into something that changes again
 * when the rest arrives.
 */
export function blockMathOnOwnLines(text: string): string {
  const parts = text.split("$$");
  if (parts.length < 3) return text;

  let out = parts[0] ?? "";
  for (let i = 1; i < parts.length; i += 2) {
    const body = parts[i] ?? "";
    const after = parts[i + 1];
    if (after === undefined) {
      // Odd number of delimiters: the last block is still arriving.
      out += `$$${body}`;
      break;
    }
    out += body.includes("\n") ? `$$\n${body.trim()}\n$$` : `$$${body}$$`;
    out += after;
  }
  return out;
}

/** Currency first, then delimiters, then the block shape remark-math can read. */
const preprocess = (text: string) =>
  blockMathOnOwnLines(normalizeMathDelimiters(escapeCurrencyDollars(text)));

export const MarkdownText = memo(function MarkdownText() {
  // `defer` lets the renderer batch parses while tokens arrive, rather than
  // re-parsing the whole document on every delta.
  return (
    <MarkdownTextPrimitive
      className="aui-md"
      preprocess={preprocess}
      remarkPlugins={[remarkGfm, remarkMath]}
      rehypePlugins={[rehypeKatex]}
      components={components}
      defer
    />
  );
});
