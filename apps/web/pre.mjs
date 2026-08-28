import { escapeCurrencyDollars, normalizeMathDelimiters } from "@assistant-ui/react-markdown";
const sample = String.raw`Alternatively, using long multiplication:

$$\begin{array}{r} 17 \\ \times 23 \\ \hline 51 \quad (\text{= } 17 \times 3) \\ 340 \quad (\text{= } 17 \times 20) \\ \hline 391 \end{array}$$

So the result is $\boxed{391}$.`;
const afterCurrency = escapeCurrencyDollars(sample);
const afterBoth = normalizeMathDelimiters(afterCurrency);
console.log("--- input ---\n" + sample);
console.log("\n--- after escapeCurrencyDollars ---\n" + afterCurrency);
console.log("\n--- after normalizeMathDelimiters ---\n" + afterBoth);
console.log("\nchanged by currency step:", sample !== afterCurrency);
console.log("changed by delimiter step:", afterCurrency !== afterBoth);
