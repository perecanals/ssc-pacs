import "@testing-library/jest-dom";

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
