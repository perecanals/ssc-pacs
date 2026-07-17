import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Dev-proxy target: the backend port. Defaults to config.toml's [web-app].port
// default; override for a non-standard backend with WEBAPP_PORT=9000 npm run dev.
const backend = `http://localhost:${process.env.WEBAPP_PORT ?? 8043}`;

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      "/api": backend,
      "/ohif": backend,
      "/dicom-web": backend,
    },
  },
});
