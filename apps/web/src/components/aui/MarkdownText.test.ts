/**
 * The maths preprocessing, which is the part that breaks silently.
 *
 * A model writes `\[ … \]`, assistant-ui normalises that to `$$ … $$`, and
 * micromark then reads the first line of a `$$` block as its *meta* — so a
 * multi-line block loses its opening line and KaTeX renders red source.
 */

import { describe, expect, it } from "vitest";

import { blockMathOnOwnLines } from "./MarkdownText";

describe("blockMathOnOwnLines", () => {
  it("leaves single-line maths alone", () => {
    // The common case, and already correct: rewriting it would only churn.
    const text = "before $$17 \\times 23$$ after";
    expect(blockMathOnOwnLines(text)).toBe(text);
  });

  it("moves a multi-line block's delimiters onto their own lines", () => {
    const text = "$$\\begin{array}{r}\n 17 \\\\\n\\hline\n 391\n\\end{array}$$";
    expect(blockMathOnOwnLines(text)).toBe(
      "$$\n\\begin{array}{r}\n 17 \\\\\n\\hline\n 391\n\\end{array}\n$$",
    );
  });

  it("does not pair one block's closer with the next block's opener", () => {
    // The bug this function was written with. A left-to-right regex matches
    // `$$…$$` across the prose *between* two blocks, turning a sentence into
    // maths and stripping the following block's first line.
    const text = "Thus,\n$$\\boxed{391}$$\nAlternatively:\n$$\\begin{array}{r}\n 17\n\\end{array}$$";
    const out = blockMathOnOwnLines(text);
    expect(out).toContain("$$\\boxed{391}$$");
    expect(out).toContain("Alternatively:");
    // The array block keeps its opening line, which is the whole point.
    expect(out).toContain("$$\n\\begin{array}{r}");
  });

  it("passes an unterminated block through while it is still streaming", () => {
    const text = "answer:\n$$\\begin{array}{r}\n 17";
    expect(blockMathOnOwnLines(text)).toBe(text);
  });

  it("handles text with no maths at all", () => {
    expect(blockMathOnOwnLines("plain prose")).toBe("plain prose");
  });
});
