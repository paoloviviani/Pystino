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
 */

import {
  MarkdownTextPrimitive,
  unstable_memoizeMarkdownComponents as memoizeMarkdownComponents,
} from "@assistant-ui/react-markdown";
import { memo } from "react";
import remarkGfm from "remark-gfm";

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

export const MarkdownText = memo(function MarkdownText() {
  // `defer` lets the renderer batch parses while tokens arrive, rather than
  // re-parsing the whole document on every delta.
  return (
    <MarkdownTextPrimitive
      className="aui-md"
      remarkPlugins={[remarkGfm]}
      components={components}
      defer
    />
  );
});
