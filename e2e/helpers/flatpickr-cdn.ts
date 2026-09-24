import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import type { Page, Route } from "@playwright/test";

/**
 * Deterministic test-only Flatpickr CDN.
 *
 * `miniapp/index.html` loads Flatpickr 4.6.13 from jsDelivr (pinned — see the
 * "CDN policy" shell assertion in scripts/acceptance.sh). In E2E we must not
 * hit the real CDN: this helper intercepts the three exact jsDelivr URLs and
 * serves the byte-identical files from the local `node_modules/flatpickr`
 * devDependency, so the picker is available offline and reproducible.
 *
 * The pinned URLs below are the single source of truth for the interception;
 * they must match `miniapp/index.html` exactly.
 */

const here = path.dirname(fileURLToPath(import.meta.url));
const fpDir = path.resolve(here, "../../node_modules/flatpickr/dist");

const PINS = [
  { url: "https://cdn.jsdelivr.net/npm/flatpickr@4.6.13/dist/flatpickr.min.css", file: "flatpickr.min.css", type: "text/css" },
  { url: "https://cdn.jsdelivr.net/npm/flatpickr@4.6.13/dist/flatpickr.min.js", file: "flatpickr.min.js", type: "application/javascript" },
  { url: "https://cdn.jsdelivr.net/npm/flatpickr@4.6.13/dist/l10n/ru.js", file: "l10n/ru.js", type: "application/javascript" },
] as const;

/**
 * Install route interception for the pinned Flatpickr jsDelivr assets,
 * serving them from `node_modules/flatpickr`. Call once per page (before
 * navigating to the app), before any module runs.
 */
export async function installFlatpickrCdn(page: Page): Promise<void> {
  for (const pin of PINS) {
    const body = readFileSync(path.join(fpDir, pin.file), "utf8");
    const handler = (route: Route) =>
      route.fulfill({
        status: 200,
        contentType: pin.type,
        body,
      });
    // Exact-URL match (not a glob): the CDN policy pins these three assets.
    await page.route(pin.url, handler);
  }
}
