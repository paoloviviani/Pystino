/**
 * A fenced code block, as assistant-ui's stylesheet expects it.
 *
 * The contract lives in `@assistant-ui/react-markdown`: a fenced block is
 * rendered as `CodeHeader` above `SyntaxHighlighter`, supplied per language via
 * `componentsByLanguage` (or as defaults on `components`). The compiled
 * stylesheet styles exactly this pairing — `aui-code-header-root` is a bar with
 * rounded top corners and no bottom border, `aui-shiki-base pre` a body with
 * rounded bottom corners and a muted ground. Render one without the other and
 * the shapes do not meet; render neither (which is what this app did) and a
 * fenced block arrives as a bare `<pre>` with every line wearing the inline-code
 * pill.
 *
 * The highlighter is shiki: real grammars, TextMate-accurate, and the reason
 * `aui-shiki-base` exists — their own examples highlight with it. Highlighting
 * is asynchronous, so the component renders the plain block first and swaps in
 * the highlighted markup when it lands; with `defer` on the markdown primitive
 * the re-highlights are batched behind typing and scrolling, and a slow grammar
 * costs nothing but the swap.
 */

import type { CodeHeaderProps, SyntaxHighlighterProps } from "@assistant-ui/react-markdown";
import { CheckIcon, CopyIcon } from "lucide-react";
import { useEffect, useState } from "react";
import { bundledLanguages, codeToHtml } from "shiki";

/** Highlight and remember the markup for one code string. `codeToHtml` caches
    its highlighter internally, so repeated calls warm rather than rebuild. */
function useHighlighted(code: string, language: string): string | null {
  const [html, setHtml] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const lang = language in bundledLanguages ? language : "text";
    codeToHtml(code, { lang, theme: "github-light" }).then(
      (highlighted) => {
        if (!cancelled) setHtml(highlighted);
      },
      () => {
        // A grammar that fails to load must not blank the block.
        if (!cancelled) setHtml(null);
      },
    );
    return () => {
      cancelled = true;
    };
  }, [code, language]);

  return html;
}

/** Copy affordance on the header bar. The code arrives as a prop, so this is
    a plain clipboard write — no selection, no runtime lookup. */
function CopyCodeButton({ code }: { code: string }) {
  const [copied, setCopied] = useState(false);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard denied (insecure origin, permission): a button that did
      // nothing is worse than one that says so.
      setCopied(false);
    }
  };

  return (
    <button type="button" className="aui-button-icon" aria-label="Copy code" onClick={copy}>
      {copied ? <CheckIcon /> : <CopyIcon />}
    </button>
  );
}

export function AuiCodeHeader({ language, code }: CodeHeaderProps) {
  return (
    <div className="aui-code-header-root">
      <span className="aui-code-header-language">{language ?? "text"}</span>
      <CopyCodeButton code={code} />
    </div>
  );
}

export function AuiSyntaxHighlighter({ code, language }: SyntaxHighlighterProps) {
  const html = useHighlighted(code, language);

  if (html === null) {
    // Before shiki answers — and forever, for a grammar it refuses — the block
    // is still a complete, copyable `<pre>`. The swap must be an improvement,
    // never a replacement.
    return (
      <div className="aui-shiki-base">
        <pre>
          <code>{code}</code>
        </pre>
      </div>
    );
  }

  return (
    // Shiki's output is its own well-formed markup; there is nothing in it we
    // did not ask it to produce.
    <div className="aui-shiki-base" dangerouslySetInnerHTML={{ __html: html }} />
  );
}
