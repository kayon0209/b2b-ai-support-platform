/**
 * Compile the components the behaviour tests render, into `tests/.built/`.
 *
 * Node's type stripping reads `.ts` and `.mts` but not `.tsx`, so importing a
 * component directly fails with `ERR_UNKNOWN_FILE_EXTENSION` before a single
 * assertion runs. esbuild is already present as a Vite dependency, so this
 * borrows it rather than adding one.
 *
 * Only the entry points the tests need are built, and `react` /
 * `react-dom` / `react-router-dom` stay external so the output imports the same
 * instances the test does - bundling a second copy of React would make hooks
 * fail at runtime for reasons that have nothing to do with the component under
 * test.
 */
import { build } from "esbuild";
import { mkdir } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const outDir = join(here, ".built");

await mkdir(outDir, { recursive: true });

await build({
  entryPoints: [join(here, "..", "src", "components", "ui.tsx")],
  outfile: join(outDir, "ui.js"),
  bundle: true,
  format: "esm",
  platform: "node",
  // Keeps the output readable when a test failure points at a line inside the
  // component - the compiled shape is not the thing under test, and a stack
  // full of generated code helps nobody.
  minify: false,
  sourcemap: "inline",
  external: ["react", "react-dom", "react-router-dom"],
  loader: { ".css": "empty" },
  logLevel: "warning",
});
