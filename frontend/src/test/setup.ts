import "@testing-library/jest-dom/vitest";
import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

afterEach(cleanup);
// Mark jsdom as an act-capable environment (React 18 requirement).
(globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
