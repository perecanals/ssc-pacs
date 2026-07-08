import js from "@eslint/js";
import react from "eslint-plugin-react";
import reactHooks from "eslint-plugin-react-hooks";
import globals from "globals";

// Flat config for the React 19 frontend. Conventions enforced:
// prop-types on every component (runtime validation is the project's chosen
// contract), rules-of-hooks + exhaustive-deps (intentional dep omissions are
// annotated with eslint-disable comments at the call site).
export default [
  { ignores: ["dist/**", "coverage/**", "node_modules/**"] },
  js.configs.recommended,
  react.configs.flat.recommended,
  {
    // Just the two classic hooks rules — the v7 "recommended" preset adds
    // compiler-era rules (react-hooks/refs, set-state-in-effect) that reject
    // deliberate patterns used here (render-mirrored refs, restore-then-set
    // hydration effects).
    plugins: { "react-hooks": reactHooks },
    rules: {
      "react-hooks/rules-of-hooks": "error",
      "react-hooks/exhaustive-deps": "warn",
    },
  },
  {
    files: ["**/*.{js,jsx}"],
    languageOptions: {
      ecmaVersion: 2024,
      sourceType: "module",
      globals: { ...globals.browser },
      parserOptions: { ecmaFeatures: { jsx: true } },
    },
    settings: { react: { version: "detect" } },
    rules: {
      // Vite uses the automatic JSX runtime — no React import needed.
      "react/react-in-jsx-scope": "off",
      // Typographic entities in JSX text are fine (…, ", ').
      "react/no-unescaped-entities": "off",
      "no-unused-vars": ["error", { argsIgnorePattern: "^_", varsIgnorePattern: "^_" }],
    },
  },
  {
    // Build/test tooling runs under Node.
    files: ["*.config.js"],
    languageOptions: { globals: { ...globals.node } },
  },
  {
    // Tests: vitest APIs are imported explicitly, but globals mode also
    // exposes them; process.env appears in setup helpers.
    files: ["src/**/__tests__/**"],
    languageOptions: { globals: { ...globals.node } },
  },
];
