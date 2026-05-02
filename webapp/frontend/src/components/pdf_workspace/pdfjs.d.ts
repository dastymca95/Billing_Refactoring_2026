// Phase 1H — minimal ambient typings for the legacy pdf.js entrypoints.
// `pdfjs-dist` ships its own .d.ts under the package root, but the
// `build/pdf.mjs` and `build/pdf.worker.mjs` paths we import for the
// classic worker-URL setup are typed as `any`. Declare them here so
// TypeScript stops complaining without pulling in @types/pdfjs-dist
// (which is for the v2 API and conflicts with v4).

declare module "pdfjs-dist/build/pdf.mjs" {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const pdfjs: any;
  export = pdfjs;
}

declare module "pdfjs-dist/build/pdf.worker.mjs?url" {
  const src: string;
  export default src;
}
