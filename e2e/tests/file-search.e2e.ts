import { test, expect } from "@playwright/test";
import { openApp, assertNoLeakedDom } from "../helpers/app";

// V3 P32: document search on the Files screen. The search endpoint is
// mocked (the E2E backend points its embedding provider at a dead port, so
// no file can be indexed here); the real endpoint is covered by
// tests/test_api.py::test_files_list_and_search against live PostgreSQL.
// The mock returns the exact SearchResultOut shape.

const base = "http://127.0.0.1:" + (process.env.E2E_PORT ?? 8123);
const searchRoute = "**/api/v1/files/search*";

const RESULTS = [
  {
    file_id: 1,
    file_name: "quarterly-plan.txt",
    position: 0,
    text: "The quarterly plan covers launch milestones.",
    score: 0.031,
  },
  {
    file_id: 1,
    file_name: "quarterly-plan.txt",
    position: 1,
    text: "Budget review is scheduled for the end of the quarter.",
    score: 0.028,
  },
];

test("file search: loading, results, empty, error + retry", async ({
  page,
}) => {
  const guard = await openApp(page, base);
  guard.allow("/api/v1/files/search");
  await page.locator('.nav-btn[data-tab="files"]').click();
  const view = page.locator("#view");

  const searchInput = view.locator(".search-row .field-input");
  const searchBtn = view.locator(".search-row .btn", { hasText: "Поиск" });
  await expect(searchInput).toBeVisible();
  await expect(searchBtn).toBeVisible();

  let requests = 0;
  // 1. Loading state is visible while the request is in flight, then the
  //    results render: source filename, chunk position (1-based) and
  //    excerpt.
  await page.route(searchRoute, async (route) => {
    requests += 1;
    await new Promise((r) => setTimeout(r, 400));
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(RESULTS),
    });
  });
  await searchInput.fill("quarterly");
  await searchBtn.click();
  await expect(view.locator(".search-results .state-loading")).toBeVisible();
  const resultCards = view.locator(".search-results .card");
  await expect(resultCards).toHaveCount(2);
  await expect(view.locator(".search-results .file-name").first())
    .toContainText("quarterly-plan.txt");
  await expect(view.locator(".search-results .badge").first())
    .toContainText("фрагмент 1");
  await expect(view.locator(".search-results .search-excerpt").first())
    .toContainText("launch milestones");
  await expect(view.locator(".search-results .search-excerpt").nth(1))
    .toContainText("Budget review");
  await assertNoLeakedDom(page, "launch milestones");

  // 2. Empty result set: a localized empty state, no result cards.
  await page.route(searchRoute, (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: "[]" }),
  );
  await searchInput.fill("nothing matches");
  await searchBtn.click();
  await expect(view.locator(".search-results .state-empty"))
    .toContainText("Ничего не найдено.");
  await expect(view.locator(".search-results .card")).toHaveCount(0);

  // 3. Search failure: localized error state; retry re-issues the request
  //    and renders results.
  let errorCalls = 0;
  await page.route(searchRoute, (route) => {
    errorCalls += 1;
    if (errorCalls === 1) {
      return route.fulfill({
        status: 500,
        contentType: "application/json",
        body: '{"detail":"boom"}',
      });
    }
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(RESULTS),
    });
  });
  await searchInput.fill("boom");
  await searchBtn.click();
  await expect(view.locator(".search-results .state-error")).toBeVisible();
  await view
    .locator(".search-results .state-error .btn", { hasText: "Повторить" })
    .click();
  await expect(view.locator(".search-results .card").first()).toBeVisible();
  expect(errorCalls).toBe(2);

  // 4. An empty query never issues a request.
  const before = requests + errorCalls;
  await searchInput.fill("   ");
  await searchBtn.click();
  expect(requests + errorCalls).toBe(before);

  guard.assertClean();
});
