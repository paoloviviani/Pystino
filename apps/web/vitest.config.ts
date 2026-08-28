import { mergeConfig } from "vite";
import { defineConfig } from "vitest/config";
import viteConfig from "./vite.config";

// Separate from vite.config.ts because Vite's own `defineConfig` has no `test`
// key — merged rather than duplicated so the plugin list cannot drift between
// how the app builds and how it is tested.
export default mergeConfig(
  viteConfig,
  defineConfig({
    test: {
      environment: "jsdom",
      setupFiles: ["./src/test-setup.ts"],
      globals: true,
      css: true,
      // The compose stack shares this machine; a descheduled test is not a
      // failing one. See the same note in apps/console.
      testTimeout: 30_000,
    },
  }),
);
