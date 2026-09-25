import { test, expect, type Page } from "@playwright/test";
import {
  openApp,
  goTab,
  assertNoHorizontalOverflow,
  assertNoLeakedDom,
} from "../helpers/app";
import { runDbScript, e2eDbUrl } from "../helpers/seed";

const base = "http://127.0.0.1:" + (process.env.E2E_PORT ?? 8123);

// Unique per run so re-runs against a warm DB never collide.
const run = Date.now().toString(36);
const ACTION_TITLE = `Inbox-${run}: ship report`;
const STALE_SUMMARY = `Stale-${run}: cancel ghost item`;
const REJECT_SUMMARY = `Reject-${run}: draft memo`;
const WORKOUT_NAME = `Sched-${run}`;
const FACT_OLD = `V2-${run}: drink green tea`;
const FACT_NEW = `V2-${run}: drink matcha`;
const FILE_NAME = `v2-failed-${run}.txt`;
const FILE_KEY = `e2e-v2-${run}.txt`;

/**
 * Seed V2 state that no UI flow can create for the deterministic test user
 * (id 999999, upserted by the API on the first /me call in openApp):
 *  - three proposed pending_actions (create_item / stale cancel_item / create_item),
 *  - one confirmed user_fact (for the supersede flow),
 *  - one `failed` user_file whose bytes exist on disk (for the retry flow).
 */
function seedV2State(): void {
  runDbScript(`
import asyncio, hashlib, json, pathlib
from datetime import datetime, timezone
import asyncpg

USER = 999999
RUN = "${run}"
ACTION_TITLE = "${ACTION_TITLE}"
STALE_SUMMARY = "${STALE_SUMMARY}"
REJECT_SUMMARY = "${REJECT_SUMMARY}"
FACT_OLD = "${FACT_OLD}"
FILE_NAME = "${FILE_NAME}"
FILE_KEY = "${FILE_KEY}"

async def main():
    conn = await asyncpg.connect("${e2eDbUrl}")
    try:
        today = datetime.now(timezone.utc).date().isoformat()
        await conn.execute(
            "INSERT INTO pending_actions (user_id, kind, payload, summary, status, expires_at) "
            "VALUES ($1, 'create_item', $2, $3, 'proposed', now() + interval '1 hour')",
            USER,
            json.dumps({
                "title": ACTION_TITLE,
                "kind": "task",
                "starts_at": today + "T10:00:00+00:00",
                "due_at": today + "T18:00:00+00:00",
            }),
            "create_item: " + ACTION_TITLE,
        )
        await conn.execute(
            "INSERT INTO pending_actions (user_id, kind, payload, summary, status, expires_at) "
            "VALUES ($1, 'cancel_item', $2, $3, 'proposed', now() + interval '1 hour')",
            USER,
            json.dumps({"item_id": 999999999}),
            "cancel_item: " + STALE_SUMMARY,
        )
        await conn.execute(
            "INSERT INTO pending_actions (user_id, kind, payload, summary, status, expires_at) "
            "VALUES ($1, 'create_item', $2, $3, 'proposed', now() + interval '1 hour')",
            USER,
            json.dumps({"title": REJECT_SUMMARY, "kind": "task"}),
            "create_item: " + REJECT_SUMMARY,
        )
        fact_value = FACT_OLD
        fact_key_hash = hashlib.sha256(
            " ".join(fact_value.split()).lower().encode("utf-8")
        ).hexdigest()
        await conn.execute(
            "INSERT INTO user_facts (user_id, category, key, key_hash, value, status) "
            "VALUES ($1, 'food', $2, $3, $4, 'confirmed')",
            USER,
            "v2-" + RUN,
            fact_key_hash,
            fact_value,
        )
        p = pathlib.Path("storage/e2e-files")
        p.mkdir(parents=True, exist_ok=True)
        body = b"v2 e2e seed file\\n"
        (p / FILE_KEY).write_bytes(body)
        await conn.execute(
            "INSERT INTO user_files (user_id, storage_key, original_filename, "
            "mime_type, size_bytes, state, error) "
            "VALUES ($1, $2, $3, 'text/plain', $4, 'failed', 'simulated embedding failure (e2e)')",
            USER,
            FILE_KEY,
            FILE_NAME,
            len(body),
        )
        print("seeded V2 state for user", USER)
    finally:
        await conn.close()

asyncio.run(main())
`);
}

/** No rendering-bug literals in the visible view text. */
async function assertCleanText(page: Page): Promise<void> {
  const text = await page.locator("#view").innerText();
  expect(text).not.toContain("[object");
  expect(text).not.toContain("undefined");
  expect(text).not.toContain("null");
}

test("V2 features: actions inbox, fact supersede, workout schedule, file retry, proactive settings", async ({
  page,
}) => {
  // openApp also triggers the first /me, which upserts the test user row the
  // seed script references via FK.
  const guard = await openApp(page, base);
  seedV2State();

  // --- 1. Actions inbox: three proposed actions --------------------------
  await goTab(page, "actions");
  const cards = page.locator("#view .list .card");
  await expect(cards).toHaveCount(3);
  await expect(page.locator("#view .list .badge", { hasText: "ожидает" })).toHaveCount(3);

  // 1a. Confirm the create_item action: it executes, the badge flips to
  //     "выполнено" and the created item lands in the Today list.
  const createCard = page.locator("#view .list .card", { hasText: ACTION_TITLE });
  await createCard.locator(".item-actions .btn").first().click(); // Confirm
  await expect(page.locator("#toast")).toContainText("Сохранено.");
  await expect(createCard.locator(".badge")).toHaveText("выполнено");
  // No confirm/reject buttons remain on an executed action.
  await expect(createCard.locator(".item-actions .btn")).toHaveCount(0);
  await goTab(page, "today");
  await expect(page.locator("#view .item-title", { hasText: ACTION_TITLE })).toBeVisible();
  await assertNoLeakedDom(page, ACTION_TITLE);

  // 1b. Stale target: confirming a cancel_item whose target does not exist
  //     returns 409 → localized toast; the action is expired server-side.
  await goTab(page, "actions");
  const staleCard = page.locator("#view .list .card", { hasText: STALE_SUMMARY });
  // The stale confirm is expected to return 409 (controlled error).
  guard.allow("/actions/2/confirm");
  await staleCard.locator(".item-actions .btn").first().click(); // Confirm
  // V3 P33: the stale toast now carries the server's 409 detail.
  await expect(page.locator("#toast")).toContainText(
    "Действие неактуально: proposal no longer applies to current data",
  );
  // V3 P33: the stale branch re-renders; the navigation below re-fetches
  // and shows the expired state.
  await goTab(page, "today");
  await goTab(page, "actions");
  await expect(staleCard.locator(".badge")).toHaveText("истекло");
  await expect(staleCard.locator(".item-actions .btn")).toHaveCount(0);

  // 1c. Reject the remaining proposed action.
  const rejectCard = page.locator("#view .list .card", { hasText: REJECT_SUMMARY });
  await rejectCard.locator(".item-actions .btn").nth(1).click(); // Reject
  await expect(page.locator("#toast")).toContainText("Сохранено.");
  await expect(rejectCard.locator(".badge")).toHaveText("отклонено");
  await assertCleanText(page);
  await assertNoHorizontalOverflow(page);

  // --- 2. Workout scheduling ---------------------------------------------
  await goTab(page, "workouts");
  const schedCard = page.locator("#view .card", {
    has: page.locator('input[aria-label="Запланировать тренировку"]'),
  });
  await expect(schedCard).toHaveCount(1);
  await schedCard
    .locator('input[aria-label="Запланировать тренировку"]')
    .fill(WORKOUT_NAME);
  // Missing start time → localized validation toast, nothing scheduled.
  await schedCard.locator(".btn-primary", { hasText: "Запланировать" }).click();
  await expect(page.locator("#toast")).toContainText("Сначала выбери время.");
  // Pick today at 23:50 via Flatpickr (date cell + hour/minute time inputs).
  await schedCard.locator(".picker-field").click();
  await page.locator(".flatpickr-calendar").waitFor({ state: "visible" });
  await page.locator(".flatpickr-calendar .today").click();
  await page.locator(".flatpickr-time input").nth(0).fill("23");
  await page.locator(".flatpickr-time input").nth(1).fill("50");
  await page.locator(".picker-input").focus();
  await page.keyboard.press("Escape");
  await expect(schedCard.locator(".picker-field")).toContainText("23:50");
  await schedCard.locator('input[type="number"]').fill("30");
  await schedCard.locator(".btn-primary", { hasText: "Запланировать" }).click();
  await expect(page.locator("#toast")).toContainText("Тренировка запланирована.");
  await goTab(page, "today");
  // V5 §6.1/§6.5: the scheduled workout keeps its verbatim title (no
  // "Workout:" prefix) and is identified by the workout icon.
  const workoutCard = page.locator("#view .card", { hasText: WORKOUT_NAME }).first();
  await expect(workoutCard.locator(".item-title")).toHaveText(WORKOUT_NAME);
  await expect(workoutCard.locator(".item-icon--workout")).toBeVisible();
  await assertCleanText(page);

  // --- 3. File retry: a failed file re-queues for ingestion ---------------
  await goTab(page, "files");
  const fileCard = page.locator("#view .list .card", { hasText: FILE_NAME });
  await expect(fileCard).toHaveCount(1);
  // Badge shows the failed state and the card carries the retry button.
  await expect(fileCard.locator(".item-head .badge")).toHaveText("ошибка");
  await fileCard.locator(".item-actions .btn", { hasText: "Повторить индексацию" }).click();
  await expect(page.locator("#toast")).toContainText("Сохранено.");
  // retry_file re-queues (state → queued; no worker runs in the E2E API).
  await expect(fileCard.locator(".item-head .badge")).toHaveText("в очереди");
  await assertCleanText(page);
  await assertNoHorizontalOverflow(page);

  // --- 4. Fact supersede: replace a confirmed fact ------------------------
  await goTab(page, "facts");
  const oldFactCard = page.locator("#view .list .card", { hasText: FACT_OLD });
  await expect(oldFactCard).toHaveCount(1);
  await expect(oldFactCard.locator(".item-head .badge")).toHaveText("подтверждён");
  // Open the inline replace form (one open at a time).
  await oldFactCard.locator(".item-actions .btn", { hasText: "Заменить" }).click();
  const replaceInput = page.locator("#view .replace-form input");
  await expect(replaceInput).toHaveCount(1);
  await replaceInput.fill(FACT_NEW);
  await page.locator("#view .replace-form .btn-primary", { hasText: "Сохранить" }).click();
  await expect(page.locator("#toast")).toContainText("Предложено.");
  // The replacement is a distinct proposed fact that references the old one.
  const newFactValueCard = page.locator("#view .list .card", {
    has: page.locator(`.fact-value:text-is("${FACT_NEW}")`),
  });
  await expect(newFactValueCard).toHaveCount(1);
  await expect(newFactValueCard.locator(".item-head .badge").first()).toHaveText(
    "предложен",
  );
  // V3 P34: a pending replacement is distinguished from a plain proposal.
  await expect(
    newFactValueCard.locator(".item-head .badge", { hasText: "замена" }),
  ).toHaveCount(1);
  // The new card explains which fact it supersedes.
  await expect(newFactValueCard.locator(".fact-replaces")).toContainText(FACT_OLD);
  // The old trusted fact STAYS confirmed while the replacement is pending.
  const supersededCard = page.locator("#view .list .card", {
    has: page.locator(`.fact-value:text-is("${FACT_OLD}")`),
  });
  await expect(supersededCard.locator(".item-head .badge")).toHaveText(
    "подтверждён",
  );
  await assertCleanText(page);

  // Confirming the replacement is the only moment the old fact is superseded:
  // the new fact becomes confirmed and the old one flips to "заменён".
  await newFactValueCard
    .locator(".item-actions .btn", { hasText: "Подтвердить" })
    .click();
  await expect(page.locator("#toast")).toContainText("Сохранено.");
  await expect(newFactValueCard.locator(".item-head .badge")).toHaveText(
    "подтверждён",
  );
  await expect(supersededCard.locator(".item-head .badge")).toHaveText("заменён");
  // V3 P34: the superseded fact points at the fact that replaced it.
  await expect(supersededCard.locator(".fact-replaces")).toContainText(FACT_NEW);
  // The superseded fact lost its replace button.
  await expect(
    supersededCard.locator(".item-actions .btn", { hasText: "Заменить" }),
  ).toHaveCount(0);
  await assertNoLeakedDom(page, FACT_NEW);
  await assertCleanText(page);

  // --- 5. Proactive settings card -----------------------------------------
  await goTab(page, "settings");
  const proactiveCard = page.locator("#view .card", {
    has: page.locator('h2.view-title:text-is("Проактивные уведомления")'),
  });
  await expect(proactiveCard).toHaveCount(1);
  // Defaults: weekly review on, quiet hours 22:00 → 08:00, max 3, 120 min.
  const weeklySwitch = proactiveCard.locator(
    'input.switch[aria-label="Еженедельный обзор (понедельник)"]',
  );
  await expect(weeklySwitch).toBeChecked();
  const rowValues = proactiveCard.locator("button.settings-row .settings-row-value");
  await expect(rowValues).toHaveCount(4);
  expect((await rowValues.nth(0).innerText()).trim()).toBe("22:00");
  expect((await rowValues.nth(1).innerText()).trim()).toBe("08:00");
  expect((await rowValues.nth(2).innerText()).trim()).toBe("3");
  expect((await rowValues.nth(3).innerText()).trim()).toBe("120 мин");
  // Toggling a switch persists via PATCH and shows the saved toast.
  await weeklySwitch.click();
  await expect(page.locator("#toast")).toContainText("Настройки сохранены.");
  expect(await weeklySwitch.isChecked()).toBe(false);
  // Restore the default so later specs see pristine state.
  await weeklySwitch.click();
  await expect(page.locator("#toast")).toContainText("Настройки сохранены.");
  await expect(weeklySwitch).toBeChecked();
  await assertCleanText(page);
  await assertNoHorizontalOverflow(page);

  // No uncaught exceptions, console errors, or unexpected API failures.
  guard.assertClean();
});
