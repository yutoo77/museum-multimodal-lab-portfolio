// Copyright (C) 2026 yutoo77
// SPDX-License-Identifier: AGPL-3.0-only

import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const localPort = (name: string, fallback: number): number => {
  const value = process.env[name];
  if (!value) return fallback;
  if (!/^\d{1,5}$/.test(value)) throw new Error(`${name} must be a local TCP port`);
  const port = Number(value);
  if (port < 1 || port > 65535) throw new Error(`${name} must be a local TCP port`);
  return port;
};

const frontendPort = localPort("MML_FRONTEND_PORT", 5174);
const backendPort = localPort("MML_BACKEND_PORT", 8765);

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: frontendPort,
    strictPort: true,
    proxy: {
      "/api": `http://127.0.0.1:${backendPort}`,
    },
  },
});
