import "@testing-library/jest-dom";

// On Node >= 22 the runtime defines an experimental global `localStorage`
// that is undefined unless --localstorage-file is passed, shadowing jsdom's
// implementation (vitest's jsdom window IS globalThis). Provide an
// in-memory stand-in so components using localStorage can mount.
if (!globalThis.localStorage) {
  const store = new Map();
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: {
      getItem: (k) => (store.has(k) ? store.get(k) : null),
      setItem: (k, v) => store.set(String(k), String(v)),
      removeItem: (k) => store.delete(k),
      clear: () => store.clear(),
      key: (i) => Array.from(store.keys())[i] ?? null,
      get length() { return store.size; },
    },
  });
}

// jsdom does not implement IntersectionObserver (used by the DataTable
// infinite-scroll sentinel). Provide a no-op stub: it never reports an
// intersection, so loadMore() is driven explicitly in hook tests instead.
if (typeof globalThis.IntersectionObserver === "undefined") {
  globalThis.IntersectionObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
    takeRecords() { return []; }
  };
}
