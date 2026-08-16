import { mergeConfig } from "vite";
import { defineConfig } from "vitest/config";
import viteConfig from "./vite.config";

// Separate from vite.config.ts because Vite's own `defineConfig` has no `test`
// key — merged rather than duplicated so the plugin list and aliases cannot
// drift between how the app builds and how it is tested.
export default mergeConfig(
  viteConfig,
  defineConfig({
    test: {
      environment: "jsdom",
      setupFiles: ["./src/test-setup.ts"],
      globals: true,
      css: true,
      // Six times vitest's default. Not because any test is slow — the whole
      // suite is about eighteen seconds — but because it gets run beside the
      // Python suite and a compose stack on a small machine, and once that
      // starts swapping a test can sit descheduled for a minute and fail
      // having done nothing wrong. A ceiling costs nothing when there is
      // memory to spare.
      testTimeout: 30_000,
    },
  }),
);
