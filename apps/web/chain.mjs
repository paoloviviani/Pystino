import { readFileSync } from "node:fs";
import { escapeCurrencyDollars, normalizeMathDelimiters } from "@assistant-ui/react-markdown";
const blockMathOnOwnLines = (text) =>
  text.replace(/\$\$([^$]*\n[^$]*)\$\$/g, (_m, body) => `$$\n${body.trim()}\n$$`);
const raw = readFileSync("/w/answer2.txt", "utf8");
const out = blockMathOnOwnLines(normalizeMathDelimiters(escapeCurrencyDollars(raw)));
const i = out.indexOf("array");
console.log("--- around the array block ---");
console.log(JSON.stringify(out.slice(Math.max(0, i - 90), i + 200)));
