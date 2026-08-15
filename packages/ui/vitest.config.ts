import { defineConfig } from "vitest/config";

// Node environment: what is tested here is formatting logic, not rendering.
// Component behaviour is covered where the components are used, in the console.
export default defineConfig({
  test: { environment: "node" },
});
