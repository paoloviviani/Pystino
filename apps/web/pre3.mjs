import { readFileSync } from "node:fs";
import { escapeCurrencyDollars, normalizeMathDelimiters } from "@assistant-ui/react-markdown";
const out = normalizeMathDelimiters(escapeCurrencyDollars(readFileSync("/w/answer.txt", "utf8")));
console.log(JSON.stringify(out, null, 0).replace(/\\n/g, "\\n\n"));
