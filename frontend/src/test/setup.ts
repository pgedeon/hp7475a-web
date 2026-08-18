import "@testing-library/jest-dom/vitest";
import { afterEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";

afterEach(cleanup);
// Mark jsdom as an act-capable environment (React 18 requirement).
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
