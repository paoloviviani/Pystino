// Fails when src/lib/api-schema.ts is not what openapi-typescript makes of the
// committed openapi.json. The other half of the chain (openapi.json against
// the gateway's real models) is a gateway test, which needs Python; CI runs
// each half where its toolchain is.
import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const dir = mkdtempSync(join(tmpdir(), "api-check-"));
const out = join(dir, "api-schema.ts");
try {
  execFileSync("pnpm", ["exec", "openapi-typescript", "openapi.json", "--default-non-nullable", "false", "-o", out], {
    stdio: "ignore",
  });
  const fresh = readFileSync(out, "utf8");
  const committed = readFileSync("src/lib/api-schema.ts", "utf8");
  if (fresh !== committed) {
    console.error(
      "src/lib/api-schema.ts is out of date with openapi.json.\nRun: pnpm --filter @llmp/console api:generate",
    );
    process.exit(1);
  }
  console.log("api-schema.ts is up to date with openapi.json");
} finally {
  rmSync(dir, { recursive: true, force: true });
}
