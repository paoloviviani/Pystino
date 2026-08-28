import { readFileSync } from "node:fs";
import { unified } from "unified";
import remarkParse from "remark-parse";
import remarkMath from "remark-math";
import remarkGfm from "remark-gfm";
import remarkRehype from "remark-rehype";
import rehypeKatex from "rehype-katex";
import rehypeStringify from "rehype-stringify";
import { escapeCurrencyDollars, normalizeMathDelimiters } from "@assistant-ui/react-markdown";

const raw = readFileSync("/w/answer.txt", "utf8");
const text = normalizeMathDelimiters(escapeCurrencyDollars(raw));
const html = String(await unified()
  .use(remarkParse).use(remarkGfm).use(remarkMath)
  .use(remarkRehype).use(rehypeKatex).use(rehypeStringify)
  .process(text));
const err = html.match(/katex-error[^>]*title="([^"]*)"/g);
console.log("katex errors:", err ? err.length : 0);
if (err) err.slice(0, 3).forEach((e) => console.log("  ", e.slice(0, 220)));
console.log("display spans rendered:", (html.match(/katex-display/g) || []).length);
