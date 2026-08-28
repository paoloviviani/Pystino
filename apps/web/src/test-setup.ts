import "@testing-library/jest-dom/vitest";
import { configure } from "@testing-library/react";

/** Room for the streaming assertions, which wait on several ticks of SSE. */
configure({ asyncUtilTimeout: 2000 });
