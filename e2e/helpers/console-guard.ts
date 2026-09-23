import { expect, type Page } from "@playwright/test";

/**
 * Capture browser health signals and fail a test on any unexpected problem:
 *   - uncaught exceptions / unhandled promise rejections (pageerror),
 *   - console.error entries,
 *   - syntax errors (surface as console.error / pageerror),
 *   - missing assets (failed same-origin requests, or >=400 asset responses),
 *   - unexpected same-origin API failures (>=400 on /api/).
 *
 * The telegram.org CDN is blocked by design, so it is always ignored. Favicon
 * is ignored by default (the app ships no icon). Controlled errors are allowed
 * by calling `guard.allow(needle)` before the offending request fires.
 */
export function createConsoleGuard(page: Page, base: string) {
  const problems: string[] = [];
  const allowed = ["favicon"]; // needle substrings that suppress a report

  const isIgnored = (text: string) => allowed.some((a) => text.includes(a));

  page.on("pageerror", (err) => {
    const msg = err.message;
    if (!isIgnored(msg)) problems.push(`uncaught exception: ${msg}`);
  });

  page.on("console", (msg) => {
    if (msg.type() !== "error") return;
    const text = msg.text();
    // The CDN script is blocked on purpose; its "Failed to load resource"
    // console error carries the telegram.org URL as the message location.
    if (msg.location().url.startsWith("https://telegram.org")) return;
    // Generic "Failed to load resource" messages carry no URL; the same-origin
    // response/requestfailed handlers already decide (allow vs record) for the
    // underlying failure, so only report URL-less messages here when they are
    // not that duplicate.
    if (/^Failed to load resource/.test(text) && !text.includes(base)) return;
    if (!isIgnored(text)) problems.push(`console.error: ${text}`);
  });

  page.on("requestfailed", (req) => {
    const url = req.url();
    if (url.startsWith("https://telegram.org")) return; // blocked on purpose
    if (!url.startsWith(base)) return; // only same-origin assets matter
    if (isIgnored(url)) return;
    const type = req.resourceType();
    const text = req.failure()?.errorText ?? "failed";
    problems.push(`missing asset (${type}): ${url} -> ${text}`);
  });

  page.on("response", (res) => {
    const url = res.url();
    if (!url.startsWith(base)) return;
    const status = res.status();
    if (status < 400) return;
    if (isIgnored(url)) return;
    const kind = url.includes("/api/") ? "api failure" : "asset failure";
    problems.push(`${kind}: ${url} -> ${status}`);
  });

  return {
    /** Allow a controlled error whose URL/message contains `needle`. */
    allow(needle: string): void {
      allowed.push(needle);
    },
    problems(): string[] {
      return [...problems];
    },
    assertClean(): void {
      expect(
        problems,
        `unexpected browser problems:\n  - ${problems.join("\n  - ")}`,
      ).toHaveLength(0);
    },
  };
}
