import katex from "katex";
const cases = {
  "simple": String.raw`17 \times 23 = 391`,
  "array-with-hline": String.raw`\begin{array}{r}
   17 \\
\times 23 \\
\hline
   51 \quad (\text{= } 17 \times 3) \\
 340 \quad (\text{= } 17 \times 20) \\
\hline
 391
\end{array}`,
  "array-no-text": String.raw`\begin{array}{r} 17 \\ \times 23 \\ \hline 391 \end{array}`,
  "boxed": String.raw`\boxed{391}`,
};
for (const [name, src] of Object.entries(cases)) {
  try {
    katex.renderToString(src, { displayMode: true, throwOnError: true });
    console.log(`  OK    ${name}`);
  } catch (e) {
    console.log(`  FAIL  ${name}: ${e.message.slice(0, 160)}`);
  }
}
