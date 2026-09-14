// Vite's client types, for `import.meta.env` below. The console reads exactly
// one build-time variable — the source tree it was built from (see the
// Dockerfile's CONSOLE_BUILD_SHA) — and a missing vite/client reference
// would fail typecheck on the only line that uses it.
 /// <reference types="vite/client" />
