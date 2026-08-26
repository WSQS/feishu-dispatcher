import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "node",
    include: ["tests/webui/**/*.test.ts"],
    mockReset: true,
    restoreMocks: true,
  },
});
