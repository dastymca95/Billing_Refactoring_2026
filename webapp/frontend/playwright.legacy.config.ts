import { defineConfig, devices } from "@playwright/test";

const baseURL = process.env.PLAYWRIGHT_BASE_URL ?? "http://localhost:5174";

export default defineConfig({
  testDir: "./e2e",
  testMatch: [
    "**/operator-visual.spec.ts",
    "**/utility-u4.spec.ts",
    "**/ingestion-ai9.spec.ts",
    "**/reviewer-assisted-workspace.spec.ts",
  ],
  timeout: 30_000,
  expect: {
    timeout: 5_000,
  },
  fullyParallel: false,
  reporter: [["list"]],
  use: {
    baseURL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "chromium-legacy",
      use: {
        ...devices["Desktop Chrome"],
      },
    },
  ],
});
