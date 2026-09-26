// Mini App production build (V5.4 P7): bundle + minify the vanilla-JS
// sources in miniapp/ into miniapp-dist/, which is what the API serves in
// production (MINIAPP_DIR) and what E2E exercises. Flatpickr stays on the
// pinned CDN (V5 §10); only the app's own assets are processed here.
//
// Output: miniapp-dist/{app.js,styles.css,index.html} + a raw/gzip size
// report at test-artifacts/miniapp-size-report.json (gitignored dir).

import { gzipSync } from "node:zlib";
import { mkdir, readFile, readdir, rm, stat, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import * as esbuild from "esbuild";
import htmlMinifier from "html-minifier-terser";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const src = path.join(root, "miniapp");
const dist = path.join(root, "miniapp-dist");
const reportPath = path.join(root, "test-artifacts", "miniapp-size-report.json");

await rm(dist, { recursive: true, force: true });
await mkdir(dist, { recursive: true });

// JS: bundle the ES module graph (app.js -> js/*.js) and minify. es2020 is
// the lowest target Telegram in-app browsers (WKWebView / Android) reliably
// support.
await esbuild.build({
  entryPoints: [path.join(src, "app.js")],
  outfile: path.join(dist, "app.js"),
  bundle: true,
  minify: true,
  format: "esm",
  target: ["es2020"],
  legalComments: "none",
  logLevel: "warning",
});

// CSS: minify styles.css in place (no @imports to bundle).
await esbuild.build({
  entryPoints: [path.join(src, "styles.css")],
  outfile: path.join(dist, "styles.css"),
  bundle: false,
  minify: true,
  logLevel: "warning",
});

// HTML: conservative minify — comments are kept (the CDN-policy comments
// document the pinned asset URLs) and every src/href must survive intact.
const html = await readFile(path.join(src, "index.html"), "utf8");
await writeFile(
  path.join(dist, "index.html"),
  await htmlMinifier.minify(html, {
    collapseWhitespace: true,
    minifyCSS: true,
    minifyJS: true,
    removeComments: false,
    keepClosingSlash: true,
  }),
);

// Size report (raw + gzip) over the built artifacts.
const files = {};
let rawTotal = 0;
let gzipTotal = 0;
for (const name of await readdir(dist)) {
  const buf = await readFile(path.join(dist, name));
  const gz = gzipSync(buf, { level: 9 }).length;
  files[name] = { raw: buf.length, gzip: gz };
  rawTotal += buf.length;
  gzipTotal += gz;
}
const report = {
  generated_at: new Date().toISOString(),
  files,
  totals: { raw: rawTotal, gzip: gzipTotal },
};
await mkdir(path.dirname(reportPath), { recursive: true });
await writeFile(reportPath, JSON.stringify(report, null, 2) + "\n");

for (const [name, s] of Object.entries(files)) {
  console.log(`  ${name.padEnd(12)} raw ${String(s.raw).padStart(8)} B   gzip ${String(s.gzip).padStart(8)} B`);
}
console.log(
  `miniapp-dist: ${Object.keys(files).length} files, raw ${rawTotal} B, gzip ${gzipTotal} B`,
);
console.log(`report: ${path.relative(root, reportPath)}`);

// Fail fast on a broken/empty build.
const bundle = await stat(path.join(dist, "app.js"));
if (bundle.size < 1000) {
  throw new Error(`built bundle suspiciously small: ${bundle.size} B`);
}
