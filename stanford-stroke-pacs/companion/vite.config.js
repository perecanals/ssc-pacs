import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      "/api": "http://localhost:8043",
      "/ohif": "http://localhost:8043",
      "/dicom-web": "http://localhost:8043",
    },
  },
});
