# Progress

Status: V4 IN PROGRESS — closing the correctness gaps found by the
independent post-V3 audit (see the audit below). V3 code is the baseline
(`4a57308...`); V4 makes targeted correctness fixes and does NOT rewrite
the product. Working tree is committed at each meaningful boundary.

## V4 — Audit findings (post-V3, recorded before implementation)

The independent post-V3 audit found the V3 milestone was architecturally
sound but had real correctness gaps. Most importantly, **the V3 CI claim
was not true**: the first (and only) GitHub Actions run for the V3 final
commit `4a57308...` completed with `conclusion=failure` and **no jobs
scheduled** — the workflow was syntactically valid YAML but used the
`runner` context in job-level `env` (not a valid context there), so GitHub
rejected the whole workflow. V3 must not be claimed CI-green; the REPORT
is corrected in V4 P44.

Defects found, grouped by the eight V4 primary goals:

1. **Leases don't protect domain side effects** (P0).
   `JobWorker._run_job` starts a detached heartbeat; if the heartbeat can't
   renew ownership (returns `False`) it just exits and the handler keeps
   committing domain state. Only the *final* complete/fail re-checks
   ownership. File ingestion's final check only rejects
   `job.status == cancelled` — it does not detect a job recovered by
   another worker, a changed owner token, an expired lease, or a non-running
   job. A stale owner can also `_record_failure()` over a newer owner's
   successful work.

2. **Proactivity cross-kind anti-spam is violated** (P0/P1).
   The pass reads the daily count and the latest-nudge time **once** at the
   start, then may commit/send several *different* nudge kinds without
   re-checking `max_nudges_per_day` / `min_interval_minutes` / quiet hours.
   Several existing tests encode the bug (they assert two messages are sent
   when `max_nudges_per_day = 1`).

3. **The Qwen turn protocol can contradict itself** (P0/P1).
   `AssistantTurn` is a broad permissive schema: `need_data` + `actions`,
   `reply` + `data_requests`, and a fold that re-requests `data_requests`
   are all representable. The "reply wins" behavior silently discards a
   tool request. There is no separate fold schema and no strict mode
   discriminator.

4. **DB/network transaction gaps** (P0/P1).
   The NL task-draft FSM path (`TaskDraftStates.waiting_for_text`) opens a
   DB transaction, then runs the Qwen structured inference with it still
   open for up to `CHAT_TIMEOUT_SECONDS`. Free chat does it right; the
   draft path does not.

5. **Mini App wall-clock handling** (P0).
   A naive user-timezone wall-clock form value is run through
   `new Date(...)` + timezone conversion as if it were an absolute instant,
   so the picker drifts by the browser/user timezone offset. No dedicated
   instant↔wall helpers, and the New Item surface has no `ends_at`.

6. **CI/acceptance not reproducible from an empty machine** (P0/P1).
   V3 CI failed (see above); Alembic required the full app `Settings`
   (Telegram + AI credentials) even though it only needs `DATABASE_URL`;
   E2E hardcodes `assistant:assistant@localhost:5432`; acceptance used a
   separate developer Postgres for the Playwright stage.

7. **Domain/UX inconsistencies** (P1).
   Deterministic action previews are not localized (RU users see English
   previews); scheduled workouts encode duration only in `extra` with a
   hard-coded English `Workout:` title prefix and no `ends_at`; action
   payload schemas silently ignore unknown fields (a `{"item_id": 42}`
   no-op update is a valid proposal); `GET /actions?status=` does not
   filter by the *effective* (TTL-aware) status; local-upload failures can
   leave a filesystem orphan; the test-auth server lives in `src/assistant`.

8. **Remote CI never verified green** (P0). V4 must end with a real green
   GitHub Actions run (or explicitly state "Remote CI verification
   pending" if the run is unreachable).

## V4 — Implementation progress

Goal items are tracked by their `§` number. Committed on `main`; working
tree clean at each boundary (full suite + Ruff green before each commit).

- **§17-18** action payload schemas reject unknown / ignore-no-op fields.
- **§19-20** NL draft path releases its DB transaction before model I/O.
- **§21** localized (RU/EN) deterministic action previews.
- **§22** scheduled workouts get a real `ends_at`, no hard-coded prefix.
- **§23** (`a45a4cc`) free-text entity resolution
  (`calendar.resolve_item`, `reminders.resolve_reminder`) is bounded at the
  SQL layer: exact match then a metachar-escaped substring search, each
  capped at 25 rows in deterministic order — an unbounded history is never
  scanned into Python.
- **§24** (`074b093`) the calendar read tool gains four strongly-typed,
  calendar-only params (`range_start`/`range_end`/`priority`/`kind`) with a
  schema validator, so "this month" / "next Friday" / "high-priority tasks"
  are answered by a bounded `list_range` + bounded Python filter (no
  arbitrary SQL, no unbounded loop).
- **§25** (`1cb1501`) Mini App datetime picker no longer drifts by the
  browser/user offset. `time.js` splits the two wall-clock concepts:
  `instantIsoToUserWallBrowserDate` (the only aware-instant→user-TZ
  conversion, for the empty-picker "now" default) and
  `wallStringToBrowserDatePreservingFields` (a naive user-TZ wall string is
  mapped to a flatpickr `Date` by its fields directly, never via
  `new Date()` + TZ conversion). `ui.js` seeds the pickers from these
  helpers, so an empty picker defaults to the user's "now" and a re-seeded
  picker round-trips exactly.
- **§26** (`3d92add`) E2E proves §25: browser Europe/Amsterdam, user
  Europe/Moscow, frozen clock. An empty picker defaults to the user's wall
  time (not the browser's); a chosen value survives close+reopen with no
  drift; saving stores the exact user-TZ instant; a reload renders the
  user's time. A second spec freezes an instant where Moscow is just past
  midnight and Amsterdam is still the previous day, proving the picker's
  DATE follows the user's zone.
- **§27** (`6fb0c29`) The Mini App New-create surface now offers an "end"
  date control (reusing the existing `pickerBtn` + flatpickr visual language),
  so an event can be created with BOTH a start and an end in a single pass —
  no second edit. `app.js viewNew` adds `formState.endsAt`, an `endBtn`,
  `ends_at` in the save payload, and reflows the rows (start+end share a row,
  due on its own). `en`/`ru` add `miniapp.new_end`. The `end >= start` rule is
  the SHARED backend invariant already in `calendar.create_item` (surfaced as
  a 400 at the API) — no new backend logic. API test asserts an event created
  with start+end in one pass (201, end after start) and an inverted pair ->
  400. E2E spec `create-event-start-end.e2e.ts` drives start+end in one pass,
  asserts both times round-trip after reload, and asserts an inverted end is
  rejected (400); it is self-cleaning (deletes created items, restores UTC in
  a `finally`) so it does not pollute the shared sequential suite. The New view
  gained a picker field, so `miniapp.e2e.ts` / `screens-audit.e2e.ts` move the
  due picker from index 3 to 4 and the field count 4 -> 5.
- **§28** The New-view reminder-offset picker (presets + custom, SPEC §14.2
  UX bounds) is extracted into a shared `reminderPicker()` helper in
  `miniapp/app.js` (returns `{ node, get, clear }`); the Edit view reuses it
  below the existing pending-reminder list so the user can inspect pending
  reminders, cancel one, and ADD a new offset. New endpoint
  `POST /api/v1/items/{item_id}/reminders` (`ItemReminderCreate.offsets_minutes`)
  calls the shared `reminders.create_item_reminders`, so max/bounds/dedupe
  stay in the service — no duplicated validation in JS beyond the UX
  constants. The service now enforces the cap over the item's TOTAL pending
  `item_linked` reminders (a second create cannot push an item past 5;
  400 when exceeded). Items without `starts_at` create nothing (201 +
  `[]`; the UI toasts `miniapp.reminder_needs_start` first). New keys
  `miniapp.reminder_save` / `reminder_added` / `reminder_needs_start`
  (en+ru). `ui.js field()` now accepts multiple controls. API tests cover
  add/dedupe/fire-time, total-cap (exactly 5 ok, 6th -> 400), schema 422s,
  no-starts_at, and cross-user 404. `edit-item.e2e.ts` extends the edit
  flow: adds a "10 min" reminder via the shared picker, then cancels both
  pending reminders; self-cleaning (item delete cascades). Verified: full
  pytest 525 passed, Ruff clean, E2E 18/18 passed.
- **§29** TTL-aware effective status filter on `GET /actions`.
- **§30** service owns local-upload artifact cleanup on write-failure; the
  API layer also cleans on commit-failure.
- **§31** local-upload disk I/O (mkdir/write) is off the event loop via
  `asyncio.to_thread`.
- **§32** chat-only / embedding-disabled docs consistency. Audited every
  embedding mention (README, .env.example, docs/RESEARCH.md,
  docs/ASSUMPTIONS.md, docs/ARCHITECTURE.md) against the code
  (`config.embedding_configured`): chat credentials are the only required
  AI credential, and a chat-only deployment boots fully useful. Fixed the
  two places that presented `EMBEDDING_*` as required: the Quick-start
  comment now says fill `CHAT_*` and notes `EMBEDDING_*` is optional, and
  the "AI providers" section gains a chat-only paragraph (uploads rejected
  visibly at registration with `files.err_embedding_unconfigured`, search
  degrades to lexical-only with the provider never called, `/readyz`
  reports `ai_embedding` `degraded` not `not_ready`, everything else
  unaffected, pre-existing ingest jobs fast-fail). `.env.example` marks the
  `EMBEDDING_*` block OPTIONAL with the same degradation notes.
  RESEARCH/ASSUMPTIONS/ARCHITECTURE already matched the behavior (no
  change). Docs-only.
- **§33** the test-auth entry point moved from the production package to
  `e2e/support/test_app.py` (outside `src/`). The Docker image copies only
  `src/` and installs only the `assistant` package, so the test-auth
  override is now structurally absent from every production artifact —
  not merely unimported. `git rm src/assistant/api/testing.py`;
  Playwright's `webServer` command is `uv run python e2e/support/test_app.py`
  (cwd = repo root; the script dir is on sys.path so `uvicorn.run`
  re-imports it by name). `tests/test_minapp_shell.py` loads the module by
  path and gains `test_package_has_no_test_auth_module` (asserts
  `find_spec("assistant.api.testing") is None` in the installed package and
  the script exists). Acceptance step 3 now also runs the built production
  image asserting `assistant.api.testing` is absent from it; step 21 runs
  the new absence test. README + ASSUMPTIONS 40 updated to the new location
  and the "absent from the image" guarantee. Verified: Ruff clean, full
  pytest 526 passed, E2E 18/18 (webServer now runs the new script).
- **§34** E2E database is now fully parameterized by `E2E_DATABASE_URL`
  (asyncpg form accepted; `postgresql+asyncpg://` also accepted and
  normalized) with optional `E2E_DATABASE_ADMIN_URL` for the
  CREATE DATABASE connection (default: `postgres` DB of the same server).
  Every hardcoded `assistant:assistant@localhost:5432` in `e2e/` is gone —
  `global-setup.ts` (URLs now passed to the Python script via env, no
  interpolation), `playwright.config.ts` webServer `DATABASE_URL`,
  `helpers/seed.ts` (new `e2eDbUrl` export), and the `v2-features` seed
  script all derive from the same env var + default. global-setup order is
  now create-DB → alembic → TRUNCATE (TRUNCATE previously ran before the
  tables existed, which only worked on a pre-existing DB). Verified: full
  E2E 18/18 on a FRESH database via `E2E_DATABASE_URL` (dropped first,
  created+migrated+truncated by the suite) and 18/18 on the default
  `assistant_e2e`.
- **§35** The acceptance Playwright E2E stage (step 22) now targets the
  SAME throwaway Docker PostgreSQL as steps 4–21, not the dev database.
  `E2E_DATABASE_URL`/`E2E_DATABASE_ADMIN_URL` are set for the `npm run
  test:e2e` invocation to point at the `assistant_e2e` DB on
  `127.0.0.1:${PG_PORT}` (the `postgres` superuser DB supplies the
  CREATE DATABASE connection). The header "Requires" comment no longer
  claims the E2E needs a pre-existing dev Postgres: the entire run is
  served by the step-4 Docker Postgres, so acceptance is reproducible from
  an empty machine. Verified: on a fresh `pgvector/pgvector:pg17`
  container (no pre-existing `assistant_e2e`), the suite created +
  migrated + truncated the DB and passed 18/18.
- **§36** Acceptance step 14 now builds the bot through the **production**
  `create_bot()` (exactly what `python -m assistant.bot.main` runs) instead
  of a hand-rolled `Bot(token=..., parse_mode=HTML)`, and asserts the P23
  plain-text invariant `bot.default.parse_mode is None`. The old step
  hand-built a bot with `parse_mode=HTML`, so it never exercised the real
  factory and would have stayed green even if `create_bot()` had regressed
  to a markup mode. The matching pytest regression
  (`tests/test_bot_foundation.py::TestPlainTextOutput::test_bot_process_bot_has_no_parse_mode`)
  already exists and is run by step 18. Verified: the new step-14 snippet
  passes against the production factory; the TestPlainTextOutput suite is
  8/8.
- **§37** The CI workflow self-lints with `rsteube/actionlint@v3.5.0` (added
  in V4 §45 P1, commit `8ab39b1`) — a plain YAML parse would have missed the
  `runner`-in-job-level-`env` context error that broke V3 CI. Verified
  locally with a real actionlint v1.7.12 binary: `actionlint
  .github/workflows/ci.yml` → **0 parse errors, 0 lint errors** (the
  shellcheck/pyflakes inline sub-rules are not installed locally and are
  provided by the CI action). No workflow change needed for §37.
- **§38** A REAL GitHub Actions run is **not possible from this
  environment** without pushing to `origin` (forbidden by QWEN.md; `gh` is
  not installed; the run is single-turn/non-interactive), and I will not
  claim CI green. Read-only check of the remote (no push): the only
  recorded run is on the pushed baseline `4a57308` (V3) with
  `conclusion=failure` — that run used the pre-V4 workflow whose
  `FILE_STORAGE_DIR: ${{ runner.temp }}/ci-files` lived in job-level `env`
  (where the `runner` context is unavailable). Local HEAD already contains
  the three fixes that address it (actionlint self-lint, `runner` context
  moved to step-level `env`, job-level `DATABASE_URL` for the pre-conftest
  alembic run) — confirmed via `git diff 4a57308..HEAD --
  .github/workflows/ci.yml`. **Remote CI verification pending** until those
  commits are pushed (a manual/user action).
- **§39** Readiness AI-provider terminology corrected from `ok`/`degraded` to
  `configured`/`unconfigured`. The `/readyz` probe performs **no inference**
  — it only checks Postgres connectivity (`SELECT 1`, 3 s timeout) and reads
  provider configuration from settings — so the previous `ok`/`degraded`
  words falsely implied it could see provider *health* it cannot check. The
  terms now state exactly what is checked: `ai_chat` is `configured`/
  `unconfigured` on `settings.chat_api_key` and `ai_embedding` on
  `settings.embedding_configured`; an `unconfigured` component (a chat-only
  deployment) never makes the service `not_ready` (readiness stays gated
  solely on Postgres reachability). PostgreSQL keeps its `ok`/`error`
  wording (that one *is* a live connectivity probe). Updated
  `src/assistant/api/readiness.py` (docstring + component statuses),
  `src/assistant/api/main.py` (the `/readyz` comment),
  `tests/test_readiness.py` (renamed the two AI tests to
  `test_readyz_ai_unconfigured_is_not_unready` /
  `test_readyz_ai_configured_when_credentials_present` with matching
  assertions), the `README.md` chat-only bullet, and `docs/ASSUMPTIONS.md`
  row 41. Verified: `tests/test_readiness.py` 5 passed; Ruff clean on the
  touched files.
- **§40** The caller-supplied `X-Request-Id` is now **bounded** before it is
  trusted for tracing. Previously `request.headers.get("X-Request-Id")` was
  used verbatim, so a hostile/buggy caller could send an arbitrarily long id
  (or one full of control/format characters) that the request-id middleware
  then (a) echoes in the response header and (b) binds into the log context
  and stamps onto **every** log line the request emits — a log-bloat and
  log/header-injection vector. The new `_sanitize_request_id()` in
  `src/assistant/api/main.py` accepts the incoming value only when it is ≤
  128 chars and matches `^[A-Za-z0-9._-]+$`; anything else (including an
  absent header) is replaced by a fresh `uuid4().hex`. A `_CONTEXT_KEYS`
  audit found the tuple already complete for every correlation field the
  codebase binds (`request_id`, `user_id`, `chat_id`, `job_id`, `job_type`,
  plus `file_id`), so no field was added. Added 4 regression tests to
  `tests/test_api.py` (reuse well-formed id, mint when absent, replace when
  oversized, replace when it contains control/format chars). Verified: the 4
  new tests pass; Ruff clean on the touched files.

**Next (in order):** §4-8 lease safety, §9-11 proactivity, §12-16 turn
protocol; §41-47 named regression tests, docs sync, Definition of Done.


## V3 — Priority 55: Definition of Done (final verification)

- `bash scripts/acceptance.sh` (22 steps) executed end-to-end green on
  2026-09-24: uv sync, Ruff, imports, lock-drift check, Compose config
  + loopback port audit, `docker compose build`, fresh PostgreSQL
  (`assistant` + `assistant_e2e`), `alembic upgrade head` from empty
  database (9 revisions), full **496**-test pytest suite on the fresh
  DB, **14/14** Playwright E2E specs, credential-free CI workflow
  present, no required TODO/stub.
- `REPORT.md` rewritten: evidence-based V3 final report (verification
  table, known-limitations list, no production-readiness claims).
- `PROGRESS.md` (this document) describes the final state.
- `git status` clean after the final commit.

## V3 — Priority 53: preserve successful V2 behavior

Verified each listed V2 behavior is intact with live test coverage (no
code changes were needed — the suite already pins all of these):

- **Mini App initData HMAC verification** — `tests/test_init_data.py`
  (17 tests: tampered user/auth_date, missing/malformed hash, duplicate
  parameters, stale/future auth_date, wrong token, bot users) +
  `tests/test_api.py` (missing/tampered/stale → 401).
- **Strict per-user isolation** — user-scoping tests across calendar,
  reminders, workouts, files/retrieval, actions, facts and chat context
  (e.g. `test_build_context_excludes_other_users_data`,
  `test_delete_file_scoped_to_owner`, `test_read_tools_are_user_scoped`).
- **Shared Docker storage** — `tests/test_storage_shared.py` + the
  compose `./storage` mount (validated by the Compose config step).
- **Job leases/heartbeats** — `tests/test_jobs.py` (claim sets lease,
  no double claim, renew only for owner, completion rejected after lease
  expiry, abandoned-job recovery with backoff).
- **No-think primary Qwen path** — `tests/test_thinking_ux.py`
  (`CHAT_THINKING_ENABLED` default false; explicit llama.cpp
  `enable_thinking` off; structured completions included).
- **ru/en i18n** — `tests/test_i18n.py` (locale-key parity, RU fallback,
  language at execution time, per-user persistence) + onboarding RU/EN
  tests.
- **Calendar reminder rescheduling** —
  `tests/test_calendar.py::test_reschedule_recomputes_linked_reminders`.
- **Tri-state PATCH** — `tests/test_api.py::test_item_patch_tri_state`,
  `tests/test_calendar.py::test_update_item_tri_state`, and the
  `edit-item.e2e.ts` spec.
- **File retry** — `tests/test_api.py::test_file_retry_flow`,
  `tests/test_files.py::test_ingest_failure_is_visible_and_retryable`,
  and the `v2-features.e2e.ts` spec.
- **Action inbox** — `action-inbox.e2e.ts` + the actions API/service
  suites.
- **Automatic memory proposal confirmation** —
  `tests/test_turns.py` (fact proposals stay `proposed` until confirmed;
  dedupe; superseded reproposal).
- **Proactivity settings** — `tests/test_api.py::test_proactive_settings_api`
  (+ validation), `tests/test_proactivity.py`, and the
  `v2-features.e2e.ts` defaults/PATCH spec.
- **Playwright accessibility/touch-target behavior** —
  `e2e/tests/a11y.e2e.ts`.
- **Telegram theming** — `e2e/tests/theme.e2e.ts` (light/dark/custom +
  runtime change via computed styles).

- **Verification.** `FILE_STORAGE_DIR=$(mktemp -d) uv run pytest -q` →
  496 passed; `npm run test:e2e` → 14 passed (last green run of the P51
  acceptance); no regressions introduced by P51/P52.

## V3 — Priority 52: required regression tests

Audited every one of the 28 scenarios named in the priority against the
current suite; 25 were already covered (real PostgreSQL where
transaction/locking semantics matter). The three gaps were added:

- **Workout conversational mutation** (`tests/test_turns.py`):
  - `test_workout_log_conversational_proposal` — "I just ran for 40 minutes,
    effort 7/10" produces a `log_workout` PendingAction (proposed, not
    executed); no second workout row is created before confirmation.
  - `test_workout_schedule_conversational_proposal` — "Schedule gym Friday
    at 20:00 for one hour" produces a `schedule_workout` proposal; no
    calendar item exists until confirm.
- **Russian inflection/paraphrase retrieval** (`tests/test_files.py`):
  `test_russian_inflection_paraphrase_retrieval` — "как сварить кофе"
  (zero word overlap with the stored "рецепт эспрессо для дома") is
  recalled by the vector arm while an unrelated RU chunk stays out.

Already-covered scenarios (where they live):
- worker `_run_job(files.ingest)` + handler intermediate commits +
  cancelled-ingest commits nothing — `tests/test_worker_ingest.py`
- two concurrent confirms execute once (real PG row lock) —
  `tests/test_actions.py::test_concurrent_confirm_executes_once`
- TTL expiry persistence / reads don't mutate overdue —
  `tests/test_actions.py` (expired confirm, read-no-mutate, bulk-expire) +
  `tests/test_api.py::test_action_overdue_reports_expired_without_write`
- stale version conflict — `tests/test_actions.py::test_drifted_entity_expires_action`
  (+ `test_api.py::test_action_confirm_stale_target_is_409`)
- helper conflict doesn't roll back caller transaction —
  `tests/test_digests.py::test_conflict_does_not_rollback_caller_transaction`,
  `tests/test_jobs.py::test_on_conflict_do_nothing_does_not_abort_caller_tx`
- replacement rejected → old stays confirmed; confirmed → atomic supersede —
  `tests/test_facts.py` (reject/delete/confirm) + `tests/test_api.py::test_fact_supersede_flow`
- conflicting automatic memory proposal linked as replacement —
  `tests/test_facts.py::test_propose_if_absent_links_valid_replaces_fact`
- `reply + data_requests` handled safely (reply wins, one call) —
  `tests/test_turns.py::test_reply_wins_over_data_requests_end_to_end`
- `clarification + actions` rejected —
  `tests/test_ai.py::test_turn_schema_rejects_clarification_with_actions`
  (+ repair/repair-exhausted fixtures)
- lookup → second structured call → action proposal with tool-revealed id —
  `tests/test_turns.py::test_lookup_then_mutation_in_two_calls`
- recent-entity "move it" outside the 7-day window —
  `tests/test_chat.py::test_recent_entities_in_context` (30-day item)
- ambiguous reference → clarification, no mutation —
  `tests/test_turns.py::test_fixture_ambiguous_reference_yields_clarification_no_mutation`
- semantic result with zero lexical overlap —
  `tests/test_files.py::test_no_lexical_overlap_still_runs_vector_arm`
- irrelevant vector result rejected by distance bound —
  `tests/test_files.py::test_offtopic_vector_candidate_dropped_by_distance_bound`
- HTML-like user data delivered safely —
  `tests/test_bot_foundation.py::TestPlainTextOutput` (both bots plain text,
  parametrized `<`/`>`/`&`/quote payloads forwarded verbatim, no parse_mode)
- >4096 character output — `tests/test_tg_text.py::TestSendLong`
- browser timezone ≠ user timezone — `e2e/tests/timezone.e2e.ts`
- Mini App historical workout timestamp — `e2e/tests/workout-log-time.e2e.ts`
- rapid tab navigation with delayed request — `e2e/tests/stale-render.e2e.ts`
- Mini App document search — `e2e/tests/file-search.e2e.ts`
- Mini App item edit with `ends_at` — `e2e/tests/edit-item.e2e.ts`
- reminder offset max/dedupe — `tests/test_reminders.py` (validate +
  dedupe + bounded)
- oversized/decompression-heavy file safety —
  `tests/test_files.py` (DOCX bomb bound, extracted-text/chunk limits,
  oversize upload rejection)
- concurrent proactive evaluation —
  `tests/test_proactivity.py::test_concurrent_sessions_nudge_exactly_once`

- **Verification.** `uv run ruff check .` clean;
  `FILE_STORAGE_DIR=$(mktemp -d) uv run pytest -q` → 496 passed
  (493 prior + 3 new).

## V3 — Priority 51: expand acceptance verification to the full §51 check list

`scripts/acceptance.sh` was rewritten from 14 to 22 steps so every check the
priority names is exercised in one production-like run (no real AI or
Telegram credentials anywhere — the worker runs with a fake bot token and
the conversational steps use the fake provider):

1. Docker Compose validation.
2. Port exposure audit — fails if any published port is not
   `127.0.0.1`-bound (Postgres publishes none).
3. Production image build from the frozen lock (`uv sync --frozen` in the
   Dockerfile) + import smoke of the built image (`assistant.api.main` and
   `assistant.bot.main` entrypoints; fake `CHAT_API_KEY` satisfies the
   import-time credential validation, no network call).
4. Fresh Docker PostgreSQL (`ta-acceptance-pg`, own port 5433, torn down by
   the EXIT trap).
5. `alembic upgrade head` on the fresh database.
6. API start (loopback only) + `/healthz` (liveness) + `/readyz`
   (Postgres reachable) answered.
7. Seed users — one WITH settings, one WITHOUT (exercises both worker
   branches).
8. Worker real execution — digest-scheduling iterations stay alive, no
   `MissingGreenlet`, and 2 digest deliveries persist in Postgres (the
   reminder/digest job smoke with Telegram mocked).
9. Real `files.ingest` through `JobWorker._run_job` (`tests/test_worker_ingest.py`).
10. Job lease/heartbeat semantics (`tests/test_jobs.py`).
11. PendingAction confirmation + two-session concurrent-execution
    protection (`tests/test_actions.py -k confirm`).
12. Bounded conversational flow with the fake provider
    (`tests/test_turns.py`).
13. Ordinary chat with embeddings unavailable (targeted chat-only /
    no-embedding-call tests).
14. Bot imports and wires its dispatcher with Telegram mocked.
15. RU onboarding strings; 16. EN onboarding strings.
17. NL structured task draft (llama.cpp-style responses, `tests/test_ai.py`).
18. Complete pytest suite against the fresh database
    (`FILE_STORAGE_DIR` pointed at a writable temp dir).
19. Ruff. 20. `uv lock --check` (lockfile integrity — the
    `--frozen` build's maintenance-side half).
21. Production auth has no test-auth bypass
    (`test_production_app_has_no_test_auth_bypass`).
22. Mini App Playwright E2E stage (isolated `assistant_e2e` database,
    test-only `assistant.api.testing` entrypoint, stubbed initData).

`README.md` acceptance description updated to the 22-step summary.

- **Verification.** `bash scripts/acceptance.sh` completed end-to-end: all
  22 steps, `493 passed` (full suite, 47 s), Ruff clean, `uv lock --check`
  clean, `14 passed` (Playwright, 41.8 s), final line "All acceptance checks
  passed".

## V3 — Priority 50: credential-free CI

Added `.github/workflows/ci.yml` (4 jobs, `pgvector/pgvector:pg17` service
container, nothing published, no secrets required):

- **lint** — `uv lock --check` (the lockfile cannot silently drift from
  `pyproject.toml`), `uv sync --frozen` (exact locked dependencies),
  `uv run ruff check .`.
- **tests** — fresh `assistant` database; `uv run alembic upgrade head`
  migrates the empty DB (initial migration creates the `vector` extension);
  `TEST_DATABASE_URL` pinned so fixtures and app-side session factories agree;
  `FILE_STORAGE_DIR` under the runner temp dir; full pytest suite (includes
  the ru/en locale-parity test).
- **e2e** — same PG service; `npm ci`, `npx playwright install --with-deps
  chromium`, `CI=1 npm run test:e2e` (isolated `assistant_e2e` DB is created,
  migrated, and truncated by `e2e/global-setup.ts`; the test-only
  `assistant.api.testing` entrypoint serves the app — production startup
  cannot enable the test-auth bypass).
- **compose** — `docker compose config --quiet`.

`UV_VERSION` / `PYTHON_VERSION` are pinned to 0.12.17 / 3.12 to match the
Dockerfile. Concurrency cancels superseded runs of the same ref.

- **Verification (locally runnable subset).** YAML parses; `uv lock --check`
  clean; `docker compose config --quiet` OK; Ruff clean. The pytest/E2E
  steps invoke the exact commands last verified green locally (493 pytest /
  14 E2E against the same PG 17 + pgvector setup); the GitHub runner
  environment itself is not reproducible on this host, so first-run CI
  output is the remaining unverified piece.

## V3 — Priority 49: documentation reality audit

Checked the ten areas named in the priority against the code (a targeted
audit, not a claim that every documentation claim in the repository was
verified). Result: **7 stale, 3 already accurate.**

Fixed (stale → reality):
- **Structured AI output** (`docs/ASSUMPTIONS.md` row 13, `docs/RESEARCH.md`):
  the docs described `response_format=json_schema`; the provider sends no
  `response_format` (llama.cpp rejects/ignores the OpenAI-only `json_schema`
  type) — the JSON contract lives in the system prompt
  (`JSON_OUTPUT_INSTRUCTION`), the object is extracted with
  `extract_json_object`, and validated client-side with bounded retries.
- **`APP_TIMEZONE`** (`docs/ASSUMPTIONS.md` row 3): marked superseded — the
  setting was removed in P48; all "today" boundaries use the per-user IANA
  timezone.
- **Action kinds** (`docs/ARCHITECTURE.md` invariant 7): listed the nine
  actually registered kinds (`create_item`, `update_item`, `complete_item`,
  `cancel_item`, `delete_item`, `create_reminder`, `cancel_reminder`,
  `log_workout`, `schedule_workout`) per the registry in
  `src/assistant/actions/__init__.py`.
- **Fact replacement semantics** (`docs/ARCHITECTURE.md` invariant 8,
  `README.md`, `REPORT.md`): deferred supersede — the referenced fact keeps
  its state (`confirmed` stays confirmed), the new value is `proposed` linked
  by `replaces_fact_id`, and only confirming the replacement atomically moves
  the old fact to `superseded`; a rejected replacement leaves it untouched.
- **RAG semantics** (`docs/RESEARCH.md` "Hybrid retrieval", `README.md`,
  `REPORT.md`): the lexical (tsvector/ts_rank) and semantic (pgvector cosine)
  arms run independently — no lexical prerequisite — fused with RRF
  (`RRF_K=60`); vector candidates beyond `RETRIEVAL_MAX_DISTANCE` are dropped
  (lexical kept); embedding outages degrade to lexical-only.
- **`CHAT_THINKING_ENABLED` default** (`README.md`, `REPORT.md`): `false`
  (fast/no-think), matching `config.py`.

Verified accurate (no change needed):
- **Telegram delivery guarantees** and **proactivity durability**
  (`docs/ARCHITECTURE.md`): job handlers are durable at-least-once
  (idempotency keys, lease/heartbeat, `recover_abandoned`); nudges are
  at-most-once via commit-before-send — already stated correctly.
- **Flatpickr loading** (`README.md`): self-hosted in `miniapp/vendor/`
  (P36) — already stated correctly.
- **Worker transaction ownership** (`docs/ARCHITECTURE.md`): the worker owns
  claiming and final job state; handlers own their domain transactions; no
  outer `session.begin()` around handlers — already stated correctly.

- **Verification.** Doc-only change (no code touched): `uv run ruff check .`
  clean; prior P48 gates (493 pytest, 14 E2E) remain the last green baseline.

## V3 — Priority 48: remove dead config, audit env vars

- **Removed `APP_TIMEZONE` / `app_timezone`.** A full audit of every
  `Settings` field found it is the only one with zero read sites: every
  "today" boundary (morning digest, overdue/workout proactivity, workout
  stats, calendar grouping, chat turn context) resolves the per-user IANA
  timezone via `_user_tz(user)` and never reads an app-wide timezone. Deleted
  the field from `src/assistant/config.py` and the `APP_TIMEZONE=UTC` line
  from `.env.example`. SPEC §3 lists `APP_TIMEZONE` under "at minimum
  support"; the app never consulted it, so honoring that line would only add
  a misleading unused knob — `SPEC.md` is left untouched (recorded in
  `docs/ASSUMPTIONS.md` row 44).
- **README parity fix.** The README documented `CHAT_THINKING_ENABLED` as
  default `true` while `config.py` and `.env.example` both default it to
  `false` (the fast/no-think profile, P41). Corrected the default and the
  example block to `false`.
- **Audit result.** Every remaining `Settings` field has at least one usage
  site and a default (or is a required field: `database_url`,
  `public_base_url`, `telegram_bot_token`), and all have README/`.env.example`
  parity.
- **Verification.** Ruff clean; `FILE_STORAGE_DIR=$(mktemp -d) timeout 560
  uv run pytest -q` → 493 passed; `npm run test:e2e` → 14 passed.

## V3 — Priority 47: split the bot handlers and API routes into domain routers

`bot/handlers.py` (~1053 lines) and `api/routes.py` (759 lines) had grown into
monoliths: every domain (onboarding, facts, menu, settings, drafts, actions,
calendar items, files, chat on the bot side; me, calendar, workouts, files,
reminders, facts, actions, settings, i18n on the API side) lived in one
module, so an unrelated change touched a thousand-line file and the
handler/route list was hard to review. SPEC §47 asks for focused modules per
domain, with the public surface (paths, handler order, dispatcher wiring)
preserved exactly.

Design (see `docs/ASSUMPTIONS.md` row 43):
- **API: `api/routes/` package.** Each domain is a module with
  `router = APIRouter(tags=["miniapp"])` and the verbatim route bodies from the
  old single router. `common.py` holds the shared helpers (`_not_found`,
  `_bad_request`, `_user_tz`, `_aware`). The package `__init__.py` builds
  `router = APIRouter(prefix="/api/v1", ...)` and `include_router`s the nine
  sub-routers in the original declaration order. `api/main.py`
  (`from assistant.api.routes import router as api_router`) is unchanged.
- **Bot: `bot/handlers/` package.** `common.py` holds the `logger`, the
  `private_guard` router, `TaskDraft`, and every shared helper. Each domain
  module defines `router = Router(name="<domain>")`. The package `__init__.py`
  builds `router = Router(name="bot")`, sets the private-chats-only filter on
  it, `include_router`s the nine sub-routers in the original handler order, and
  re-exports every name the entrypoint, acceptance script, and tests import.
  `bot/main.py` (`from assistant.bot.handlers import private_guard, router`) is
  unchanged.
- **Behavior preserved.** The API OpenAPI exposes the same 28 `/api/v1` paths;
  the bot's message-handler order (commands → document → free-text) and
  callback order match the original one-for-one. The private-only filter set on
  the composed parent router propagates to every sub-router via aiogram's
  `check_root_filters`, so group chats are still rejected before any handler.

Splitting the module moved the monkeypatch seams in the bot tests: the suite
now patches `handlers.common.get_settings` (the `_send_thinking` guard) and
`handlers.chat.get_ai_provider` (the free-text/draft AI call) at the module
where each symbol is actually resolved. The acceptance script's step 10 now
points `FILE_STORAGE_DIR` at a writable temp dir so the file-upload test does
not depend on `/data` being writable on the host.

Verification: `uv run ruff check .` clean; 493 pytest pass; 14 E2E pass; the
acceptance script (fresh Postgres) passes all 14 steps.

## V3 — Priority 46: structured / contextual logging

Logs were previously plain text (`asctime levelname name: message`) with no
correlation: an operator could not tie a worker failure, an AI call, or an API
request to the user/job/request that produced them, and there was no guard
against a credential leaking into a log line. SPEC §23 asks for structured or
consistently formatted logs with useful context (component, user, job, file,
request/correlation id, exception details), secrets never logged, and
diagnosable worker failures.

Design (see `docs/ASSUMPTIONS.md` row 42):
- **One-line JSON records.** `assistant.logging` emits each record as a JSON
  object: `ts` (UTC ISO-8601, ms), `level`, `logger` (the **component**, e.g.
  `assistant.worker`), `msg`, and `exc` (redacted exception trace) when
  present. Cheap to ship to any log aggregator and greppable by hand.
- **Per-unit context via `contextvars`.** A single `ContextVar` holds the
  active correlation fields. `log_context(**fields)` is a context manager that
  merges fields for a block and restores the previous context on exit (so a
  job running inside a request keeps both ids). A `ContextFilter` stamps the
  active context onto every record; the formatter lifts the known fields
  (`request_id`, `user_id`, `chat_id`, `job_id`, `job_type`, `file_id`) to the
  top level and passes through any extras.
- **Request id (API).** A FastAPI HTTP middleware in `create_app()` reuses an
  incoming `X-Request-Id` (for caller-side tracing) or mints a `uuid4`, binds
  it into the log context for the whole request, and echoes it back in the
  response header.
- **Job context (worker).** `JobWorker._run_job` wraps the handler in
  `log_context(job_id, job_type, user_id)` **after** loading the job row, so
  every log the handler emits — including nested AI/embedding/file-ingestion
  logs — carries the job and owning user. This is what makes worker failures
  diagnosable.
- **User context (bot).** A new `LogContextMiddleware` binds `user_id` and
  `chat_id` from the update and is registered as the outermost middleware
  (before `DBSessionMiddleware`), so the ids are present for every log in the
  update, including session-open failures and guard rejections.
- **Secrets are never logged.** A redaction pass replaces the configured bot
  token and API keys in every message *and* exception string with
  `[REDACTED]` (values read live from `Settings`, cached until they change,
  and guarded so a logging path never raises). The existing AI-provider
  `_redact_for_log` (which strips `api_key=`/`authorization`/`bearer` from SDK
  exception text) is kept on top of this.
- **AI/embedding timing.** Successful `chat`, `chat_structured`, and `embed`
  calls log `model` + `duration_s` (+ `schema`/`attempt`/`n` where relevant),
  complementing the existing timeout/error/rejected diagnostics.

Changes:
- `src/assistant/logging.py` (rewritten): `JsonFormatter`, `ContextFilter`,
  `current_context`/`bind_context`/`clear_context`/`log_context`, the
  `_SecretRedactor`, and `setup_logging(level)` wiring a JSON handler onto the
  root logger (idempotent; app `assistant.*` loggers propagate to it).
- `src/assistant/api/main.py`: request-id HTTP middleware (bind + echo header)
  and the `log_context` import.
- `src/assistant/worker/main.py`: `_run_job` binds job/user context.
- `src/assistant/bot/middlewares.py`: new `LogContextMiddleware`;
  `src/assistant/bot/main.py` registers it outermost on `message` and
  `callback_query`.
- `src/assistant/ai/provider.py`: duration/status info logs on successful
  `chat`/`chat_structured`/`embed` (`import time`).
- `tests/test_logging.py` (new, 7 tests): JSON record shape, context
  propagation, nested-context restore, secret redaction in message and
  exception, exception detail capture, and that `setup_logging` wires a JSON
  handler (with root state saved/restored).

Verified: `tests/test_logging.py` 7 passed; live integration check — a
request logged `{"...","logger":"assistant.logprobe","msg":"probe inside
request","request_id":"probe-xyz"}` and the response echoed
`x-request-id: probe-xyz` (minted when absent, `my-trace-abc` echoed when
provided); `uv run ruff check .` clean; full `uv run pytest -q` 493 passed
(486 prior + 7 new); `npm run test:e2e` 14 passed.

## V3 — Priority 45: liveness and readiness probes

The API previously only had a plain `/health` liveness answer. An
orchestrator (or operator) could not distinguish "the process is up" from
"the process is up and can serve traffic" — specifically, it could not tell
whether the one hard dependency (PostgreSQL) was reachable, and it had no
non-invasive way to see whether the AI providers were configured.

Design (see `docs/ASSUMPTIONS.md` row 41):
- **`/healthz`** = liveness. Answers `{"status":"ok"}` as soon as the app
  is up. No dependency checks. (The legacy `/health` path is kept.)
- **`/readyz`** = readiness. Returns **200 only when PostgreSQL is
  reachable** (`SELECT 1` through the app's own `get_engine()`, bounded by
  a 3-second `asyncio.timeout` so a wedged database cannot hang the probe);
  anything else is **503**.
- **AI providers are components, not gates.** `ai_chat` is `ok` when
  `settings.chat_api_key` is set, `degraded` otherwise; `ai_embedding` is
  `ok` when `settings.embedding_configured`, `degraded` otherwise. A
  degraded component never makes the service `not_ready` — a chat-only
  deployment (no embedding provider) is fully usable, and a missing chat
  key is surfaced as `degraded` so an operator can see it without taking
  the service out of rotation.
- **No inference.** The probe checks Postgres connectivity and reads
  provider *configuration* from settings only. It never calls a chat or
  embedding endpoint, so it is cheap and safe to run on a schedule.

Changes:
- `src/assistant/api/readiness.py` (new): `check_readiness(db_probe=None)`
  returns `(ready, payload)`. `db_probe` is an injectable
  `async () -> None` check (default `SELECT 1` via `get_engine()`); tests
  inject a probe that raises to exercise the `not_ready` path without
  dropping the database.
- `src/assistant/api/main.py`: adds the `/readyz` route (200/503 via
  `JSONResponse`) and the `/healthz` liveness route; the existing `/health`
  liveness path is preserved.
- `scripts/acceptance.sh` step 3: now also curls `/readyz` and asserts
  `"status":"ready"` against the fresh database, proving the probe is
  wired end-to-end.
- `tests/test_readiness.py` (new, 5 tests):
  - `test_healthz_is_liveness` — 200 `{"status":"ok"}`.
  - `test_readyz_ready_when_postgres_reachable` — 200, `ready`, postgres `ok`.
  - `test_readyz_not_ready_when_postgres_down` — injected probe raises →
    `not_ready`, postgres `error`.
  - `test_readyz_ai_degraded_is_not_unready` — unconfigured chat+embedding
    → `ready` with both components `degraded`.
  - `test_readyz_ai_ok_when_configured` — configured key → both `ok`.

Verified: `tests/test_readiness.py` 5 passed; live `uvicorn` smoke test —
`/healthz` → `{"status":"ok"}`, `/readyz` →
`{"status":"ready","components":{"postgres":{"status":"ok"},
"ai_chat":{"status":"ok"},"ai_embedding":{"status":"ok"}}}`;
`uv run ruff check .` clean; full `uv run pytest -q` 486 passed
(481 prior + 5 new); `npm run test:e2e` 14 passed.

## V3 — Priority 44: remove the production test-auth backdoor

The Mini App auth bypass used to live inside the production
`create_app()`: a `Settings.test_auth_enabled` field read the
`ASSISTANT_TEST_AUTH` env var, and when set the production app overrode
`get_current_user` with a fixed test user. That means a leaked or
mis-set env var in a real deployment would silently open the API to a
deterministic identity. The override is moved out of the production code
path so it is structurally unreachable from a normal app.

- `src/assistant/config.py`: removed **all** test-auth fields
  (`test_auth_enabled`, `test_auth_user_id`, `test_auth_first_name`,
  `test_auth_last_name`, `test_auth_username`). `Settings` now has zero
  test-auth surface; the `ASSISTANT_TEST_AUTH` env var is ignored
  (`extra="ignore"`).
- `src/assistant/api/main.py` (production): removed `_install_test_auth`
  and the `if settings.test_auth_enabled:` block. `create_app()` now only
  wires the initData-verified `get_current_user` dependency — no env-gated
  bypass, and no test-auth imports remain in the module.
- `src/assistant/api/testing.py` (new, test-only): `install_test_auth(app)`
  overrides `get_current_user` with the deterministic test user (constants
  live here), `create_test_app()` = `create_app()` + the override, and a
  `main()` runnable via `python -m assistant.api.testing`. Nothing in the
  production runtime imports this module.
- `e2e/playwright.config.ts`: the E2E web server now runs
  `python -m assistant.api.testing` (was `assistant.api.main`) and the
  `ASSISTANT_TEST_AUTH: "1"` env var is removed — the entry point itself
  installs the override.
- `tests/test_minapp_shell.py`: the old bypass test is replaced by two
  boundary tests — `test_production_app_has_no_test_auth_bypass` (401
  without initData **even when** `ASSISTANT_TEST_AUTH=1` is in the
  environment) and `test_test_only_entrypoint_installs_deterministic_user`
  (`create_test_app()` resolves id 999999 with no initData).
- `README.md`: the "Authenticated tests without a production bypass"
  paragraph updated to describe the test-only entry point.

Verified: `tests/test_minapp_shell.py` 7 passed; both
`assistant.api.main` and `assistant.api.testing` import cleanly;
`uv run ruff check .` clean.

## V3 — Priority 43: production builds from the lockfile

Production must install exactly what `uv.lock` pins, and a check must
prevent the lockfile from being silently bypassed. The old image used
`pip install .`, which re-resolved the `pyproject.toml` ranges at build
time — a dependency edit could change the shipped image without the
lockfile (or CI) noticing.

- `Dockerfile` (rewritten):
  - Multi-stage: pinned `ghcr.io/astral-sh/uv:0.12.17` supplies the uv
    binary (no network fetch of uv at install time), over
    `python:3.12-slim`.
  - `VIRTUAL_ENV=/app/.venv` with `/app/.venv/bin` first on `PATH`, so the
    `python -m assistant.*` entrypoints used by docker-compose resolve
    from the venv.
  - `COPY pyproject.toml uv.lock README.md ./` before `COPY src ./src`
    (layer caching on code-only changes).
  - `RUN uv sync --frozen --no-dev` — `--frozen` fails the build if
    `uv.lock` is missing (production never resolves from ranges);
    `--no-dev` excludes the pytest/ruff dev group.
  - Non-root `appuser`, `FILE_STORAGE_DIR=/data/storage/files` volume
    pre-created, `EXPOSE 8000`, unchanged entrypoint.
- `scripts/acceptance.sh`: new step "14. Lockfile integrity" runs
  `uv lock --check` — exits non-zero if `uv.lock` is missing or would
  change to match `pyproject.toml` (i.e. a dependency added/edited
  without a matching `uv lock`). This is the bypass check: the install
  side (`--frozen`) and the maintenance side (`--check`) together keep
  the image and the lockfile in lockstep.
- `tests/test_packaging.py` (new): `test_uv_lockfile_is_present` and
  `test_dockerfile_installs_from_the_lockfile` — cheap file-level
  regression guards that fail if the Dockerfile regresses to
  `pip install`/range-based resolution or the lockfile disappears,
  independent of an acceptance run.

Verified: `docker build` succeeds; runtime imports (`assistant`,
sqlalchemy, aiogram, alembic, openai, fastapi, uvicorn, asyncpg, pypdf,
docx, multipart, pgvector) all resolve in the image on Python 3.12.14;
`uv lock --check` passes; `uv run pytest -q` 480 passed (was 478);
`uv run ruff check .` clean; full E2E suite 14 passed.

## V3 — Priority 42: optional embedding degradation (chat-only deployments)

Chat credentials remain mandatory; the embedding provider is now optional,
so a deployment can run chat-only without a vector model. When unconfigured
the composite provider reports `embedding_configured == False` and its embed
methods raise `AIProviderError` (fast, no network); ingestion rejects new
uploads at registration; retrieval degrades to lexical-only; and a job
enqueued before a config change fast-fails at pipeline start.

- `src/assistant/config.py`:
  - `_resolve_provider_credentials` now requires only `chat_api_key`
    (embedding may stay `None`); a missing chat key is the only
    credential error.
  - New `embedding_configured` property — `bool(self.embedding_api_key)` —
    is the single source of truth the service layer consults.
- `src/assistant/ai/provider.py`: `OpenAICompatibleProvider.__init__`
  accepts `embedding: OpenAIEmbeddingProvider | None`; new
  `embedding_configured` property; new `_require_embedding()` raises
  `AIProviderError("embedding provider is not configured ...")`;
  `embed_documents` / `embed_query` go through it.
- `src/assistant/ai/__init__.py`: `build_ai_provider` attaches the embedding
  client only when `settings.embedding_configured` is true, else `None`.
- `src/assistant/services/files.py`:
  - `_rejection_reason` gains an `embedding_configured` parameter; after the
    MIME/size checks it returns `files.err_embedding_unconfigured` when a
    chat-only deployment receives a document.
  - Both `register_upload` and `register_local_upload` pass
    `settings.embedding_configured`; a rejected upload is persisted
    `state=rejected`, `error=files.err_embedding_unconfigured`, no job, and
    (local upload) no disk write.
  - `_run_pipeline` fast-fails with `FileUploadError` when
    `settings.embedding_configured` is False — a job enqueued before a
    config change fails visibly at pipeline start instead of spending its
    backoff budget on a file that can never be embedded.
  - `retrieve_chunks` gates the vector arm on
    `get_settings().embedding_configured`: when False the vector arm is
    skipped (logged as the expected lexical-only mode, not an outage) and
    lexical hits are returned as before.
- i18n: `files.err_embedding_unconfigured` added to both `en.json` and
  `ru.json` (parity maintained).
- `tests/test_ai.py`: `test_settings_require_ai_credentials` renamed to
  `test_settings_require_chat_credentials` (chat is the only required
  credential); new `test_settings_allow_chat_only_without_embedding` builds
  a chat-only `Settings`, asserts `embedding_configured is False`, that the
  provider reports it, and that both embed methods raise
  `AIProviderError("not configured")`.
- `tests/test_files.py`: four new service-level tests — registration
  rejection for both the Telegram and Mini App paths (rejected state, the
  locale key, no job, no disk write), `_run_pipeline` fast-fail after a
  config flip, and lexical-only `retrieve_chunks` (lexical hits returned,
  `distance is None`, provider never called).

Verified: `uv run pytest -q` 478 passed (was 473); `uv run ruff check .`
clean; full E2E suite 14 passed.

## V3 — Priority 41: 9B fast/no-think runtime (bounded cost, concrete docs, fixtures)

- `src/assistant/ai/schemas.py`: `AssistantTurn` gains an
  `after`-mode `model_validator` enforcing the SPEC §8 invariant — a
  non-blank `clarification` plus any `actions` fails validation. This is
  the authoritative guard: `chat_structured`'s bounded repair loop
  (max 2 attempts, corrective feedback appended) is the only recovery
  path, and two contradictory responses surface as
  `AIOutputValidationError` instead of executing half of a
  contradictory turn.
- `src/assistant/actions/calendar.py`: `expected_updated_at` in the four
  item-mutation payloads is `Field(default=None, exclude=True)` — the
  internal optimistic-drift guard is engine-filled at proposal time and
  is now excluded from `model_dump()` and from the generated prompt
  docs, so the model can never set it.
- `src/assistant/services/turns.py`:
  - `TOOL_DESCRIPTIONS` carries short query hints ("query=<the item the
    user named> resolves it"), and `_tools_doc()` documents the request
    shape (`{tool, query?, limit 1..20}`) — no schema dump.
  - New `_field_type()` renders a compact type descriptor from a field
    annotation: enum value sets (`low|normal|high`), `datetime` as
    `"YYYY-MM-DD HH:MM"`, `list[T]` → `T[]`, unions, `Literal`, and
    unwrapping of `Annotated`/`conint`.
  - `_actions_doc()` renders `kind(field:type?, ...)` per registered
    kind from the registry (single source of truth; internal fields
    skipped via `FieldInfo.exclude`) — the action catalog is compact,
    concrete, and cannot drift from the schemas.
- `tests/test_ai.py`: 6 fixture tests through the full `chat_structured`
  path with a faked client — bare JSON single call (cost stays at 1),
  contradictory clarification+actions repairs on the 2nd attempt,
  unrepairable contradiction fails with `AIOutputValidationError`,
  schema validator unit check (blank clarification + actions allowed),
  unknown tool name fails on both attempts, invalid enum/bounds value
  (`limit: 99`) repairs on the 2nd attempt.
- `tests/test_turns.py`: 4 engine-level fixture tests through
  `run_turn` — invalid enum payload (`priority="urgent"`) lands in
  `skipped_actions` and stores no `PendingAction`; ambiguous reference
  (two "Dentist" items) ends in `TOOL_FOLD` clarification with no
  mutation; `_actions_doc()` content assertions (types, enums, date
  format, no `expected_updated_at`); turn prompts carry the action/tool
  docs.

Verified: `uv run pytest -q` 473 passed (was 463);
`uv run ruff check .` clean; full E2E suite 14 passed.

## V3 — Priorities 38–40: Proactivity hardening (deterministic summary, context, concurrency)

- `src/assistant/models/proactivity.py`: `NudgeKind.overdue` added;
  `ProactiveSettings.overdue_nudge_enabled` (Boolean, server default
  true). Migration `alembic/versions/20260924_a3f5b7c9d1e2_overdue_nudge.py`.
- `src/assistant/services/proactivity.py` (rewritten):
  - **P38 weekly review**: `_weekly_review_stats()` computes overdue
    count, completed-this-week count, up to 3 upcoming high-priority
    titles within 7 days, and workout count/minutes since the week
    started (local midnight Monday — so a Monday-morning review already
    includes that morning). `_weekly_review_text()` composes the message
    from i18n lines (no LLM). `has_content` gate: an empty week sends
    nothing.
  - **P38 overdue nudge**: default-on, once per user-local day when any
    scheduled item's anchor (due_at, else starts_at) is past; text is
    `proactive.overdue` with the count.
  - **P39 context-aware workout nudge**: fired only when the last log is
    >48h old (or none) AND there is no scheduled `source="workout"` item
    with `starts_at >= day start` — a plan on the books suppresses the
    nudge.
  - **P40 concurrency**: `evaluate_user` loads the settings row with
    `FOR UPDATE` (first-run create race → IntegrityError → per-user pass
    failure, retried next pass); every candidate claims its slot with
    `_reserve_nudge()` — `INSERT ... ON CONFLICT DO NOTHING` on the
    (user_id, kind, period_key) unique constraint, so exactly one
    concurrent session wins regardless of interleaving.
- `api/schemas.py`: `ProactiveSettingsOut`/`ProactiveSettingsUpdate`
  expose `overdue_nudge_enabled` (route PATCH already loops
  `model_fields_set`).
- i18n (ru+en parity): `proactive.weekly_review` ("Обзор недели:"),
  `proactive.overdue`, `proactive.wr_overdue/wr_completed/wr_upcoming/
  wr_workouts`, `miniapp.proactive_overdue_nudge`.
- `miniapp/app.js`: 5th proactive switch (overdue nudge) in the
  proactive card; `e2e/tests/screens-audit.e2e.ts` switch count 4 → 5.
- `tests/test_proactivity.py` (rewritten, 20 tests): weekly review
  fires once per ISO week and is silent on empty weeks; summary content
  and deterministic line order; workout nudges suppressed by a
  scheduled workout (today/upcoming) and allowed for a slipped one;
  overdue nudge fires/dedupes daily, honors its toggle, ignores
  future/completed items; anti-spam caps and min-interval; and a
  multi-session `asyncio.gather` test proving exactly one nudge is sent
  across two concurrent sessions.

Verified: `uv run pytest -q` 463 passed; `uv run ruff check .` clean;
ESM `node --check` clean; full E2E suite 14 passed (incl. the proactive
card switch count and the v2-features proactive settings spec).

## V3 — Priority 37: Today dashboard summary

- `miniapp/app.js`: new `daySummary(dayItems)` helper (calendar
  section). Returns `null` for days without items; otherwise a
  `card` with the "Итоги дня" subtitle and a `.summary-chips` row of
  `badge`s: total items on the day, scheduled ("Осталось", `high`
  tone when > 0), completed ("ok" tone), and overdue (`error` tone,
  only when > 0 — a scheduled item is overdue when its due_at or
  starts_at is in the past). All counts are derived from the month
  items `viewToday` already fetched — no extra API call. Inserted as
  the first child in `viewToday`'s `view.replaceChildren(...)`,
  above the month grid.
- `miniapp/styles.css`: `.summary-chips` (flex row, wrap, 6px gap).
- i18n (ru + en, parity kept): `miniapp.today_summary`,
  `miniapp.summary_total`, `miniapp.summary_left`,
  `miniapp.summary_done`, `miniapp.summary_overdue` (all `{n}`-param).
- `e2e/tests/miniapp.e2e.ts`: step 11b asserts the summary renders
  after creating today's task (Всего: 1 / Осталось: 1 / Готово: 0);
  step 12b asserts it disappears when an empty day is selected.

Verified: full E2E suite 14 passed; `uv run pytest -q` 454 passed;
`uv run ruff check .` clean; ESM `node --check` clean.

## V3 — Priority 36: Self-hosted Flatpickr

- `miniapp/vendor/flatpickr/` (new, pinned flatpickr 4.6.13):
  `flatpickr.min.css`, `flatpickr.min.js`, `l10n/ru.js`,
  `LICENSE.md` (MIT). Byte-identical to the dist files the CDN was
  serving, downloaded once at build time — no runtime CDN dependency.
- `miniapp/index.html`: the three `cdn.jsdelivr.net/flatpickr@4.6.13`
  references now load `/miniapp/vendor/flatpickr/...` (served by the
  existing `StaticFiles` mount; the Dockerfile's `COPY miniapp ./miniapp`
  already includes the vendor dir). The telegram-web-app.js CDN tag
  stays — it is intentionally client-provided and already mocked/blocked
  in the E2E harness.
- No JS/CSS changes: `window.flatpickr` and the `ru` locale are
  exposed exactly as before, so `js/ui.js` pickers and the theme
  overrides in styles.css are untouched.

Verified: full E2E suite 14 passed (incl. the workout-scheduling and
workout-log-time specs that drive the calendar/time pickers);
`uv run pytest -q` 454 passed; `uv run ruff check .` clean.

## V3 — Priority 35: Searchable IANA timezone picker

- `miniapp/app.js`: the hardcoded `TIMEZONES` array (15 zones) is gone.
  `timezoneOptions()` returns `Intl.supportedValuesOf("timeZone")`
  (browser-provided, 400+ canonical IANA names, no network request)
  with the old 15 zones kept as `FALLBACK_TIMEZONES` for pre-2022
  browsers, and prepends the user's currently configured zone when the
  browser list does not include it. The Settings timezone row now opens
  the sheet with `searchable: true`.
- `miniapp/js/ui.js` `openSheet`: new optional `searchable` /
  `searchPlaceholder` parameters — a `type="search"` filter input above
  the options (auto-focused) hides non-matching rows on input
  (case-insensitive substring on the value) and shows the localized
  `miniapp.search_empty` state when nothing matches. The focus trap now
  includes the search input. Non-searchable sheets are unchanged.
- `miniapp/styles.css`: `.sheet-search`, `.sheet-empty`; plus a global
  `[hidden] { display: none !important }` so component display rules
  (e.g. `.sheet-row { display: flex }`) cannot defeat the `hidden`
  attribute.
- i18n (en+ru): `miniapp.tz_search_ph`.
- `e2e/tests/timezone-picker.e2e.ts` (new): opens the settings sheet,
  asserts 100+ zones render; "Berlin" narrows to exactly Europe/Berlin
  (Asia/Tokyo hidden); a no-match query shows the empty state; picking
  Europe/Berlin persists via PATCH (toast + row value) and restores
  UTC afterwards.

Verified: full E2E suite 14 passed; `uv run pytest -q` 454 passed;
`uv run ruff check .` clean; `node --check` on the ESM miniapp OK.

## V3 — Priority 34: Memory UX (fact statuses in the Mini App)

- `src/assistant/api/schemas.py`: `FactOut` now exposes
  `superseded_by: int | None`, so a superseded fact can name the fact
  that replaced it (the column already existed on `UserFact` and is
  set atomically by `services/facts.confirm_fact`).
- `miniapp/app.js` `factCard`:
  - a proposed fact with `replaces_fact_id` gets an extra
    "замена"/"replacement" badge next to the "предложен" status badge,
    distinguishing a pending replacement from a plain proposal;
  - a superseded fact renders a "Заменено на: <value>" line (reusing
    the `.fact-replaces` style) when the replacing fact is in the
    current list.
  - The four status badges (предложен/подтверждён/отклонён/заменён)
    and all existing buttons/formats are untouched.
- i18n (en+ru): `miniapp.fact_replacement`, `miniapp.fact_replaced_by`.
- Tests:
  - `tests/test_api.py::test_fact_supersede_flow` now asserts
    `superseded_by is None` before the replacement is confirmed and
    `superseded_by == <new id>` after.
  - `e2e/tests/v2-features.e2e.ts` facts section: the proposed
    replacement card must carry exactly one "замена" badge (status
    badge asserted via `.first()`), and the superseded card must show
    the replacing fact's value in its `.fact-replaces` line.

Verified: full E2E suite 13 passed; `uv run pytest -q` 454 passed;
`uv run ruff check .` clean; `node --check` on the ESM miniapp OK.

## V3 — Priority 33: Assistant action inbox inspection

- `miniapp/app.js` `actionCard`: inspection surface per SPEC (kind
  icon, typed preview, target info, stale/conflict reason) added on
  top of the existing card without redesign:
  - `ACTION_KIND_ICONS` now covers every registered kind, incl.
    `log_workout` / `schedule_workout`; the dead `replace_fact` entry
    was removed (unknown kinds still fall back to "⚡").
  - `actionTarget(a)` renders a target hint from the validated
    payload: `item_id` → "Цель: запись №{id}", `reminder_id` →
    "Цель: напоминание №{id}" (create_* payloads carry no entity id
    and show nothing — the typed preview already names the target).
  - Expired cards surface `last_error` (the server's staleness reason,
    e.g. "calendar item no longer exists") in a `.item-error` line.
  - Stale confirm: a 409 (or 400 "payload no longer valid") toast now
    shows the server detail via `miniapp.action_stale_detail` and the
    view re-renders so the card immediately shows the expired state
    instead of requiring a manual navigation.
- i18n (en+ru): `miniapp.action_stale_detail`,
  `miniapp.action_target_item`, `miniapp.action_target_reminder`.
- Backend unchanged: `GET /actions` already returned the full
  `ActionOut` (kind, typed summary from the preview builder, payload,
  last_error, effective status with read-side expiry); the live
  propose/confirm/409/reject flow stays covered by
  `e2e/tests/v2-features.e2e.ts` (its stale-toast assertion now
  expects the detail-rich message).
- `e2e/tests/action-inbox.e2e.ts` (new, GET /actions mocked with exact
  ActionOut shapes): asserts 4 cards render; kind icons
  (✅/⏰/✏️/🏋️); typed previews; target hint "Цель: запись №42" on the
  expired update_item and its `last_error` line; executed workout has
  no buttons; only proposed cards carry confirm/reject; a 409 confirm
  shows "Действие неактуально: calendar item no longer exists" and the
  re-rendered list reaches the empty state.

Verified: full E2E suite 13 passed; `uv run pytest -q` 454 passed;
`uv run ruff check .` clean.

## V3 — Priority 32: Document search in the Mini App

- `miniapp/app.js` `viewFiles`: search row (input + "Поиск" button,
  Enter also submits) above the upload row; `doSearch()` calls
  `GET /api/v1/files/search?q=&top_k=10` with the view signal and
  renders into a `search-results` holder: `loading()` while in
  flight, result cards / localized empty state / `errorState` with a
  retry that re-issues the query. Empty (whitespace) queries are a
  no-op. `searchResultCard()` shows the source filename, a
  `miniapp.search_chunk` badge with the 1-based position (consistent
  with `format_citations`) and the excerpt as text (safe `el()`).
  Upload/list/retry/delete rows untouched.
- `miniapp/styles.css`: `.search-row`, `.search-results`,
  `.search-excerpt`.
- Backend unchanged: `GET /files/search` already returned
  `SearchResultOut` (file_id, file_name, position, text, score) with
  lexical-only degradation on embedding outages; endpoint covered by
  `tests/test_api.py::test_files_list_and_search`.
- `e2e/tests/file-search.e2e.ts` (new): the endpoint is mocked with
  the exact `SearchResultOut` shape (E2E has a dead embedding port, so
  nothing can be indexed there). Asserts the loading state, result
  rendering (filename, "фрагмент 1", both excerpts), the empty state,
  the 500 error state with a working "Повторить" retry, and that an
  empty query issues no request.

Verified: full E2E suite 12 passed; `uv run pytest -q` 454 passed;
`uv run ruff check .` clean.

## V3 — Priority 31: Reminder-offset controls (no comma text entry)

- `src/assistant/services/reminders.py`: new shared constants
  `MAX_REMINDERS_PER_ITEM = 5`, `MIN_OFFSET_MINUTES = -1440`,
  `MAX_OFFSET_MINUTES = 1440` and `validate_reminder_offsets()`
  (integer check, bounds, dedupe, max count) used by
  `create_item_reminders` — single source of truth per SPEC §14.2.
- `api/schemas.py` (`ItemCreate.remind_offsets_minutes`),
  `actions/calendar.py` (`CreateItemPayload.remind_offsets_minutes`),
  `ai/schemas.py` (`AITaskDraft.reminder_offsets`): bounded via
  `conint(ge=..., le=...)` + `max_length=5`, importing the shared
  constants. `bot/handlers.py::_parse_draft` routes comma text through
  the same `validate_reminder_offsets`.
- `miniapp/app.js`: comma text input removed from the New screen.
  Preset chips (at start, 10/30/60 min, 1 day — all i18n) plus a
  bounded custom input (0–1440, integer); multi-select as a `Set`,
  at most 5 total. Selected chips are tappable to deselect; unselected
  chips disable at the limit. Duplicate/range/limit rejections toast.
  Save sends `remind_offsets_minutes` (array).
- `miniapp/styles.css`: `.remind-chips`, `.chip` (+ `.is-on`, disabled),
  `.remind-custom-row`. i18n (en+ru): preset labels, `reminder_min`,
  `reminder_custom_ph`, `reminder_add`, `reminder_limit`,
  `reminder_range`; removed the old `new_reminders_ph` placeholder.
- `tests/test_reminders.py`: validator unit tests (dedupe, inclusive
  bounds, rejects out-of-range/too many/non-int) +
  `create_item_reminders` max enforcement. `tests/test_api.py`:
  `POST /items` 422s on >5 offsets and out-of-bounds offset.
- `e2e/tests/reminder-presets.e2e.ts` (new): drives the chip UI
  (presets, custom, range toast, limit, deselect), saves with a start
  date, and verifies ground truth — exactly offsets [10, 30, 45, 60,
  1440] persisted. Deletes its item in `afterEach`.

Verified: full E2E suite 11 passed; `uv run pytest -q` 454 passed on a
fresh DB; `uv run ruff check .` clean.

## V3 — Priority 30: Task/event edit flow in the Mini App

- `src/assistant/api/schemas.py`: `ReminderOut` gains
  `calendar_item_id` + `offset_minutes` (the Mini App needs them to show
  "за N мин до начала" and filter by item). `ItemUpdate`/`ItemOut` already
  carried the tri-state fields.
- `src/assistant/services/reminders.py`: `list_reminders(...)` accepts an
  `item_id` filter.
- `src/assistant/api/routes.py`: `GET /reminders` accepts `?item_id=`.
- `miniapp/app.js`: item cards show `ends_at` in the meta line and an
  "Изменить" action (first, before Done) for scheduled items; new
  `viewEdit` view (tab "edit", `state.editId`/`state.editReturn`) fetches
  the item + its pending reminders, prefills title/description/priority
  and start/end/due (naive user-TZ wall via `isoToWall`), renders kind
  read-only, lists each pending reminder with fire time + offset and an
  inline cancel (`POST /reminders/{id}/cancel`), and saves via
  `PATCH /items/{id}` sending ONLY changed keys (explicit null clears).
  New `whenField()` row: tap-to-pick + inline ✕ clear.
- `miniapp/js/state.js`: `editId`/`editReturn`; back-button and nav
  handling for the edit tab. `miniapp/styles.css`: `.when-row`,
  `.when-clear`, `.reminder-*`. i18n: `btn_edit`, `item_ends`,
  `edit_title`, `new_ends`, `clear`, `item_not_found`, `reminder_offset`,
  `reminder_at_start`, `reminders_empty`, `reminder_cancelled` (en+ru).
- `tests/test_api.py::test_reminders_list_filter_by_item` (new).
- `e2e/tests/edit-item.e2e.ts` (new): card shows the end time; edit view
  prefills every field; tri-state PATCH verified against ground truth
  (title+priority changed, ends_at cleared, starts/due/description/kind
  untouched); reminder listed, then cancelled. The spec deletes its
  seeded item in `afterEach` (reminder cascades) so later specs see a
  clean calendar.
- E2E isolation hardening (root cause of a 5-failure run):
  `e2e/tests/miniapp.e2e.ts` left the shared deterministic user's language
  as "en" (Playwright spec order is filesystem order, not alphabetical —
  adding a file shifted it), breaking every later spec's Russian
  assertions. It now restores `language: "ru"` in `test.afterEach` (runs
  even on failure). `e2e/tests/screens-audit.e2e.ts` action count updated
  3→4 (edit button) and completion now clicks "✓ Готово" by text.
- App bug found by the new E2E: `renderReminders` passed an array to the
  variadic `Element.replaceChildren()`, stringifying it to
  "[object HTMLDivElement]" — now spread. `whenField` also used the raw
  i18n key as the picker aria-label — now `S(labelKey)`.

Verified: full E2E suite 10 passed; `uv run pytest -q` 447 passed on a
fresh DB; `uv run ruff check .` clean.

## V3 — Priority 29: Workout logging datetime

The workout-log form's "when" picker was display-only: its value was
never stored in form state, so `POST /api/v1/workouts` always omitted
`started_at` and the backend fell back to "now" — a historical log
(picked yesterday, 18:30) was silently stored as the submission instant.

- `miniapp/app.js`: the log form now keeps `logStart` (naive user-TZ
  wall string, same protocol as every other form field — see
  ASSUMPTIONS #35), seeds the picker from it, and submits it as
  `started_at`. No backend change: `WorkoutCreate.started_at` already
  accepts a naive datetime in the user's timezone.
- `e2e/tests/workout-log-time.e2e.ts` (new): Amsterdam browser + Moscow
  user + frozen clock; picks a past wall datetime (2026-09-20 18:30)
  through the Flatpickr UI, logs the workout, asserts the card shows the
  user-TZ wall time, reloads the page and asserts it survives, and checks
  the stored value is exactly `2026-09-20T15:30:00Z` via the API.

Verified: full E2E suite 9 passed; `uv run ruff check .` clean (no
Python changed).

## V3 — Priority 28: User-timezone date/time display in the Mini App

All dates/times in the Mini App now follow `state.me.settings.timezone`,
never the browser's zone (contract: instants travel as ISO-with-offset;
user-TZ wall values are naive "YYYY-MM-DD[THH:MM]" strings, which the
backend interprets in the user's timezone).

- `miniapp/js/time.js` (new): user-TZ helpers built on cached
  `Intl.DateTimeFormat(..., {timeZone})` — `userTZ`, `wallParts`,
  `dateKeyTZ`, `todayKey`, two-pass DST-safe `dayStartUTC`/`dayEndUTC`,
  `monthStartUTC(y, m)` (out-of-range month normalizes; `monthStartUTC(y,
  m+1)` is exactly a month's end), `isoToWall`, `userWallAsBrowserDate`
  (seeds flatpickr, which renders browser-local, so its "Y-m-d H:i"
  output is the naive user-TZ wall clock), `fmtDT` (aware ISO instants),
  `fmtWall` (naive wall strings).
- `miniapp/app.js`: Today/month view computes the month range with
  `monthStartUTC` in the user's zone and fetches
  `GET /items?start&end` on it; calendar cells, "today", and selected-day
  grouping use user-TZ date keys; day header and item times format in the
  user's zone (`fmtDT`); new-item and schedule forms store/submit the
  picker's naive user-TZ wall value.
- `miniapp/js/ui.js`: `pickDateTime` seeds from the instant's user-TZ
  wall clock and resolves the naive user-TZ wall string.
- `e2e/tests/timezone.e2e.ts` (new): browser pinned to Europe/Amsterdam
  with a frozen clock where Moscow's and Amsterdam's calendar days differ;
  user tz = Europe/Moscow via `PATCH /settings`; asserts "today" is the
  Moscow day, the item dot lands on the Moscow day, the item lists with
  Moscow wall time (00:30, not 23:30), and the day header shows the
  Moscow date. Restores tz to UTC at the end so later tests (which assume
  UTC) don't inherit Moscow day boundaries.
- `tests/test_api.py::test_today_uses_user_timezone`: fixed a latent
  flake — the item anchor was "noon UTC", which falls in Berlin's
  previous day once the clock passes 22:00 UTC; now anchored at Berlin
  noon (always inside the user's own "today" window).

Verified: full E2E suite 8 passed (incl. the new cross-zone test);
`uv run pytest -q` 446 passed on a fresh DB; `uv run ruff check .` clean.

## V3 — Priority 27: Mini App stale-render race

A view that resolved *after* the user switched tabs overwrote the newer
screen (slow Today response clobbering a fast Actions screen).

- `miniapp/app.js`: `render()` now bumps a monotonic `renderGeneration`
  and aborts the previous generation's `AbortController`. Every view is
  `(view, gen, signal)`: the `signal` cancels in-flight fetches, and
  `if (isStale(gen)) return;` guards every DOM commit (and the settings
  view's proactive card is built/committed under the same rule; its
  failure still cannot break the core settings screen). The `render()`
  catch renders a localized error state only for the active generation.
- `miniapp/js/api.js`: `api()` and `apiUpload()` accept an optional
  `AbortSignal`.
- `e2e/helpers/console-guard.ts`: controlled aborts are no longer
  failures — `requestfailed` with `net::ERR_ABORTED` and the browser's
  `Failed to load resource ... ERR_ABORTED` console entry are ignored
  (the app cancels in-flight fetches by design; a real failure of a
  *completed* request is still reported).
- `e2e/tests/stale-render.e2e.ts` (new): delays the Today month-range
  request 1.5 s, switches tabs rapidly in both directions, asserts the
  stale view never reaches the DOM and the active nav stays marked, and
  that the un-delayed happy path still renders the calendar.

Verified: full E2E suite 7 passed (including the new stale-render
regression); `uv run ruff check .` clean (no Python changed).

## V3 — Priority 25: Restrict the bot to private chats

The data model uses the Telegram *user* ID as the background-delivery
chat ID (reminders, digests, nudges go to the user's 1-on-1 chat), so a
task created in a group would deliver its notifications to the user's
private chat — an incoherent experience. P25 makes the bot private-only:

- `src/assistant/bot/handlers.py`:
  - Router-level filters on the main router:
    `router.message.filter = F.chat.type == "private"` and
    `router.callback_query.filter = F.message.chat.type == "private"` —
    every existing and future handler is private-only by construction.
  - New `private_guard` router (no router-level filter; each handler
    carries its own `F.chat.type != "private"` /
    `F.message.chat.type != "private"` filter):
    - group/supergroup/channel **messages** get a normal reply with the
      localized `chat.private_only` explanation;
    - non-private **callbacks** get an inline alert with the same text.
  - `_lookup_user_lang(session, tg_user)` resolves the sender's language
    from an *existing* user row only (no upsert) and falls back to the
    default language, so the rejection path is side-effect free.
- `src/assistant/bot/main.py`: the dispatcher includes `private_guard`
  **before** `router`, so non-private updates are answered and stop
  before any bot handler can see them.
- i18n: `chat.private_only` added to `en.json` and `ru.json`.

Verified: `tests/test_bot_foundation.py::TestPrivateChatsOnly` (5 tests)
— group message → localized (default-language) reply and zero user rows;
existing English user → English rejection; group callback → alert answer
and zero user rows; the router-level message and callback filters admit
`private` and reject `group`/`supergroup`/`channel`; the guard router is
wired before the main router. Full suite 446 passed, Ruff clean.
Assumption #33 documents the design and the no-upsert stance.

## V3 — Priority 24: Telegram message-length limits

Telegram rejects any message over 4096 characters, and the unbounded
outputs (an AI reply with long citations, a busy day's digest) previously
failed the whole send with a Bot API 400 — the user saw nothing.

P24 adds one central delivery helper and routes the large-output paths
through it:

- `src/assistant/services/tg_text.py` (new):
  - `split_for_telegram(text, limit=4096)` — greedy, lossless split:
    prefers a `\n\n` paragraph break, then a `\n` line break (a boundary
    must sit at least 1/3 into the window to avoid degenerate fragments),
    then a hard cut at the limit. Segments concatenate back to the
    original text; empty input yields no segments.
  - `answer_long(message, text, reply_markup=...)` — sends each segment
    as a new message, attaching the inline keyboard to the *last* segment
    only, so it stays with the end of the content.
  - `send_long(bot, chat_id, text)` — the worker-side variant; raises on
    the first failed segment so the job handler re-queues as before.
- `src/assistant/services/notifications.py`: `send_text` now goes through
  `send_long`, covering reminders, digests and proactive nudges.
- `src/assistant/bot/handlers.py`: the conversational AI reply (including
  its appended citations) is sent via `answer_long`.

Verified: `tests/test_tg_text.py` (17 tests) — short/empty/exactly-at-limit
text passes through untouched; >4096-char text splits into bounded,
lossless segments; paragraph and line boundaries win over hard cuts;
`send_long` delivers multi-segment messages and propagates failures;
`answer_long` puts the keyboard only on the final segment and omits the
`reply_markup` kwarg when there is none; `notifications.send_text`
integration: an 80-line digest splits across multiple <=4096-char
messages with no content lost. Full suite: 441 passed, Ruff clean.

## V3 — Priority 23: Telegram output safe by default (plain text)

Both outbound bots (the bot process and the worker's notification bot) were
constructed with a global `DefaultBotProperties(parse_mode=ParseMode.HTML)`.
With HTML parsing on, *any* unescaped `<` in the outgoing text — a filename
like `report <draft>.pdf`, an AI answer containing a URL with `&`, a task
title with markup-looking characters — produced a broken render or an
`aiogram.exceptions.TelegramBadRequest` from the Bot API. Safety depended on
every future send site remembering to escape, and none of them did.

P23 makes plain text the only mode:

- `src/assistant/bot/main.py`: new `create_bot()` builds
  `Bot(token=settings.telegram_bot_token)` with **no** `DefaultBotProperties`
  at all; `_run()` uses it. Removed the `DefaultBotProperties`/`ParseMode`
  imports.
- `src/assistant/services/notifications.py`: `_get_bot()` builds the worker's
  `Bot` the same way (no parse mode); `send_text` now documents itself as
  plain text. Removed the imports.

With `parse_mode` unset, aiogram sends every message as plain text: user
content is shown verbatim and can never be interpreted as markup. All i18n
locale strings are already plain (no HTML tags anywhere in `locales/*.json`),
so no visible output changes for any existing flow.

Verified: `TestPlainTextOutput` in `tests/test_bot_foundation.py` — the bot
process bot and the notification bot both expose `default.parse_mode is
None`, and `send_text` forwards six markup/URL/`&`-laden payloads byte-for-
byte with no `parse_mode` kwarg (the test .env token is a placeholder, so the
tests monkeypatch a syntactically valid fake token before constructing the
bots). Full suite: 424 passed, Ruff clean.

## V3 — Priority 22: file lifecycle consistency (fs vs DB)

Disk and database were not consistent with each other across the file
lifecycle on the single-server local volume:

- **Delete ordering (fs vs DB transaction ordering)**: `delete_file`
  unlinked the disk artifact *before* the API route's commit — a failed
  commit would leave a DB row whose only copy of the bytes (a local upload)
  was already gone. `delete_file` is now DB-only (cancels the job, deletes
  chunks + row, flushes) and returns the `storage_key`; the route commits
  first, then calls `discard_storage`.
- **Terminal-failure reaping (tombstone strategy)**: every permanently
  failed Telegram download pinned its (up to 20 MB) artifact on the shared
  volume forever. New bounded, idempotent worker pass
  `reap_terminal_artifacts` removes artifacts only when state is `failed`,
  the job is in the terminal `failed` state, and the bytes are re-sourcable
  (`telegram_file_id` set). Local uploads keep their artifact (disk is the
  only source and retry needs it); mid-backoff files (job `pending`) are
  untouched. Runs on the digest-interval cadence in `JobWorker.run`.
- **Missing-artifact visibility**: a local upload whose artifact vanished
  (lost volume) now fails visibly with "Stored file data is missing and
  cannot be re-ingested" at the top of the pipeline, matching the existing
  `retry_file` pre-check.

Tests (`test_files.py`): delete tests updated for the commit-first
contract (artifact survives `delete_file` until `discard_storage` after
commit; not-found returns `None`); new
`test_reap_terminal_artifacts_removes_resourcable_only` (Telegram
terminal-failed reaped; local-upload terminal-failed kept; mid-backoff
kept; idempotent) and
`test_local_upload_missing_artifact_fails_visibly`.
Amended `docs/ASSUMPTIONS.md` #30.
Verified: 416 pytest pass, Ruff clean.

## V3 — Priority 21: file-ingestion resource safety

Hostile inputs (highly compressed PDF/DOCX, huge text) could drive unbounded
CPU/memory in the single worker process, and the Mini App upload endpoint
buffered the whole body before the size check.

- **Per-file caps (SPEC §21)**, all configurable: `max_extracted_text_chars`
  (default 2 000 000 — enforced after extraction, before chunking),
  `max_chunks_per_file` (default 2 000 — bounds embedding batches and
  `file_chunks` rows; enforced after chunking, before embedding),
  `max_pdf_pages` (default 500 — checked from `len(reader.pages)` before any
  page is parsed). Any cap exceeded raises `FileUploadError`; the job fails
  visibly with `state=failed` and a human-readable `error`, and zero chunks
  are written.
- **Stdlib streaming DOCX extraction**: `_extract_docx` now reads
  `word/document.xml` through `zipfile` in 1 MiB chunks with a 64 MiB
  decompressed-byte cap (`_DOCX_XML_MAX_BYTES`) — a zip bomb fails with a
  clear error instead of exhausting memory — and pulls `<w:t>` runs with a
  regex + `html.unescape`. `python-docx` is no longer imported at runtime
  (kept as a test-fixture dependency only, for creating real DOCX files).
- **Off-loop pipeline work**: disk read, `extract_text`, and `chunk_text`
  run via `asyncio.to_thread` so a large document cannot starve the worker's
  event loop / job heartbeats. Indexed files record `char_count` and
  `chunk_count` in `extra` for inspection.
- **Streamed upload reads + orphan cleanup**: the Mini App `POST /files`
  endpoint reads the body in 1 MiB parts and raises HTTP 413 the moment
  `max_upload_size_bytes` is exceeded (an oversized body is never buffered
  whole); if `register_local_upload` / `session.commit()` fails, the freshly
  written disk artifact is removed via the new `discard_storage()` helper.

Tests (`test_files.py`): `test_extract_text_enforces_character_bound`,
`test_extract_docx_decompression_bomb_is_bounded`,
`test_extract_docx_requires_document_xml`,
`test_ingest_fails_when_extracted_text_exceeds_limit`,
`test_ingest_fails_when_chunk_count_exceeds_limit`.
Amended `.env.example` (MAX_EXTRACTED_TEXT_CHARS / MAX_CHUNKS_PER_FILE /
MAX_PDF_PAGES) and `docs/ASSUMPTIONS.md` #29.
Verified: 414 pytest pass, Ruff clean.

## V3 — Priority 20: adjacent-chunk merging + citations

The old `_merge_adjacent` only merged a chunk when it was immediately after the
*first* position of the span and the two chunks happened to be adjacent in the
score-sorted list, so (a) same-file chunks separated by a higher-scoring chunk
were never merged, and (b) a run of consecutive chunks collapsed only the first
pair. Citations listed filenames only, with no position information.

- **Proximity-based, order-independent merge**: chunks are grouped by file and
  merged by position proximity within the new `settings.retrieval_merge_gap`
  bound (default 1 = strictly consecutive; larger bridges a small gap of
  un-retrieved chunks). Merged spans track `position_end` and are re-ordered by
  their best constituent score.
- **Position-aware citations**: `format_citations` groups by file and annotates
  a file whose retrieved chunks span more than one position with its contiguous
  1-based part range (``file.md (parts 2–3)``); single-position sources are
  unchanged, so existing citation output is preserved.

Tests (`test_files.py`): `test_merge_adjacent_is_independent_of_score_order`,
`test_merge_adjacent_uses_configurable_gap`,
`test_citations_show_position_range_for_merged_span`; the existing
adjacent-merge and RRF-recovery tests were updated for the corrected span
merging (three consecutive chunks now collapse to one span).
Amended `docs/ASSUMPTIONS.md` #28.
Verified: 409 pytest pass, Ruff clean.

## V3 — Priority 19: improved lexical retrieval

The lexical arm used `plainto_tsquery`, which ANDs every query word together
(a multi-word query only matched a chunk containing *all* words) and treats
operators as literal text. The arm now builds an explicit OR-style tsquery.

- **OR-style recall**: `_lexical_tsquery` splits the query into terms, lowercases
  each, and joins them with `|`, so a chunk is recalled when it contains *any*
  of the query's words. Restores recall for partial-overlap queries.
- **Operator-safe**: each term is stripped of every non-word character before
  being handed to `to_tsquery`, so arbitrary user input (including `!`, `|`,
  `&`, `<`, `>`, parentheses) can never be interpreted as a tsquery operator
  and the call never raises. A term-less query returns no lexical candidates.
- **PostgreSQL-only**: no new dependencies; uses `to_tsvector`/`to_tsquery`
  with the language-neutral `simple` config.

Tests (`test_files.py`): `test_lexical_or_style_partial_overlap_recalls`
(one-word overlap now recalls, previously dropped by the AND gate);
`test_lexical_query_is_operator_safe` (operator-laden queries are normalized
away and match on the surviving term).
Verified: 406 pytest pass, Ruff clean.

## V3 — Priority 18: meaningful relevance filtering

A pure nearest-K vector recall returns its top-K even for a semantically
off-topic query, which degrades answer quality (the "nearest" chunk can be
unrelated). Retrieval now applies a configurable meaningful-relevance bound.

- **Configurable policy**: `settings.retrieval_max_distance` (default 0.9,
  0 = identical, 1 = orthogonal, 2 = opposite). The vector arm keeps only
  candidates whose cosine distance to the query is `<= retrieval_max_distance`.
  Lexical (keyword-overlap) candidates are always kept regardless of the bound.
- **Distance tracked separately**: `RetrievedChunk.distance` records each
  fused chunk's best (minimum) vector-arm cosine distance (`None` for a
  lexical-only recall); `_vector_candidates` returns distance per candidate
  and `_fuse` / `_merge_adjacent` preserve it through fusion and merging.
- **Multilingual relevance**: cross-language recall works purely by vector
  closeness (multilingual-e5-small), independent of the lexical arm.

Tests (`test_files.py`): `test_offtopic_vector_candidate_dropped_by_distance_bound`
(distance-1 vector-only candidate dropped, relevant chunk kept);
`test_cross_language_relevance_via_vector_arm` (EN query retrieves ES/RU chunks
by vector closeness with no lexical overlap, distance 0);
`test_no_lexical_overlap_still_runs_vector_arm` now also asserts the tracked
distance; `test_retrieval_is_user_scoped_and_fuses_hybrid` widens the bound to
1.5 so the off-marker fusion demonstration still surfaces.
Amended `docs/ASSUMPTIONS.md` #26 (configurable relevance policy).
Verified: 404 pytest pass, Ruff clean.

## V3 — Priority 17: RAG — remove the lexical prerequisite

Retrieval was lexical-gated: the tsvector full-text arm had to match at least
one of the user's chunks **before** the embedding provider was ever called, so
a semantically-relevant chunk that shared no exact words with the query
(paraphrased queries) was silently dropped even when the vector arm would have
ranked it first.

- **Independent arms**: `retrieve_chunks` now fetches the lexical candidate
  list and the vector candidate list **independently** and fuses them with
  Reciprocal Rank Fusion. The lexical early-return gate (`_lexical_hits`) and
  the prerequisite count query are removed.
- **Empty only when both are empty**: the result is `[]` when neither arm
  yields a candidate; otherwise the fused, merged list is bounded to `top_k`.
- **Embedding outage** still degrades to lexical-only (`AIProviderError` →
  lexical results) and the read transaction is released before the embedding
  network call (no connection held across provider I/O). The embedding call
  is bounded by the model's decision to invoke the `documents` read tool.
- Amended `docs/ASSUMPTIONS.md` #22 and the retrieval docstrings to reflect the
  no-prerequisite design.

Tests (`test_files.py`: `test_no_lexical_overlap_still_runs_vector_arm` —
zero-overlap query now embeds and surfaces the nearest chunk with a
vector-only RRF score; `test_no_match_on_either_arm_returns_empty` — empty
only when both arms are empty; existing hybrid-fusion, RRF-recovery,
embedding-outage, and adjacent-merge tests unchanged).
Verified: 402 pytest pass, Ruff clean.

## V3 — Priority 16: memory conflict/dedupe

The fact dedupe key was truncated to the first 255 normalized characters, so
two long facts sharing a prefix collided into one identity. Dedupe now keys on
a stable digest, and the model can propose an *update* to a specific confirmed
fact it has seen, with the reference revalidated before it is trusted.

- **Collision-resistant identity**: `user_facts` gains `key_hash`
  (String(64), NOT NULL) — the SHA-256 hexdigest of the normalized value
  (`" ".join(value.split()).lower()`), plus a `(user_id, key_hash)` index.
  `key` is retained as a human-readable, display/debug-only truncated form.
  `propose_if_absent` dedupes on `key_hash`. Migration
  `20260924_b7c8d9e0f1b3_fact_key_hash` backfills existing rows in Python
  (no pgcrypto dependency) before making the column NOT NULL.
- **Model-referenced replacements (SPEC §16)**: `FactProposal` gains an
  optional `replaces_fact_id`. The `facts` read tool now renders confirmed
  facts with their ids (`id=N ...`) so the model can cite one; the prompt
  instructs it to set `replaces_fact_id` when a new fact updates an existing
  one. The engine passes the id through `propose_if_absent`, which revalidates
  it (same user, status `proposed`/`confirmed`) via `_revalidated_replaces_id`;
  an invalid reference (another user's fact, a rejected/superseded fact, a
  missing id) is silently dropped and the fact stored as a plain new proposal.
  On confirm the replacement atomically supersedes the referenced fact
  (existing §15 lifecycle).
- **Rejection suppression** is documented as **permanent, not time-bounded**
  (`docs/ASSUMPTIONS.md` #25): a `rejected` fact keeps its `key_hash` in the
  live set and blocks re-proposal of that exact value until the user deletes
  the row; there is no TTL. A `superseded` fact does not block.

Tests (`test_facts.py`: hash-vs-truncated-prefix distinctness, normalized
dedupe collapse, permanent rejection suppression cleared only by delete,
valid/invalid `replaces_fact_id` revalidation, `confirmed_facts` ordering).
Verified: 401 pytest pass, Ruff clean.

## V3 — Priority 15: memory V3 replacement lifecycle

Previously `supersede_fact` demoted a `confirmed` fact to `superseded` the
moment a replacement was *proposed*, so a trusted fact lost its status while
its replacement was still unconfirmed — the assistant would have been
reasoning from a fact it no longer trusted, and the user had no way to keep
the old one.

- **Explicit link**: `user_facts` gains `replaces_fact_id` (self-FK,
  `ON DELETE SET NULL`) — the fact a proposed replacement targets.
  `superseded_by` (old→new) is kept for history. Migration
  `20260923_b7c8d9e0f1a2_fact_replaces`.
- **`supersede_fact`** now only creates the `proposed` replacement (linked via
  `replaces_fact_id`) and leaves the referenced fact's status untouched.
- **`confirm_fact`** atomically moves the referenced fact to `superseded`
  (setting `superseded_by`) in the same flush, so the trusted fact is demoted
  only when its replacement is itself confirmed. Bot and Mini App confirm
  paths both route through this service, so the guarantee holds on every
  surface.
- **`reject_fact` / `delete_fact`** leave the referenced fact untouched.
- **API**: `FactOut` exposes `replaces_fact_id`. **Mini App**: a replacement
  card shows the value it supersedes and is not itself replaceable;
  `e2e/v2-features` now verifies old stays `подтверждён` until the
  replacement is confirmed, then flips to `заменён`.

Tests (`test_facts.py`: replacement keeps old confirmed, confirm atomically
supersedes, reject/delete keep old confirmed, cross-user no-op;
`test_api.py`: supersede flow + `replaces_fact_id` in the payload).
Verified: 395 pytest pass, Ruff clean, E2E pass.

## V3 — Priority 14: calendar/reminder domain hardening

Focused, low-risk hardening of the two most-mutated entities.

- **Temporal invariant**: `create_item` and `update_item` now reject
  `ends_at < starts_at` (after UTC normalization), so an item can never be
  stored with an inverted time range. Checked against the effective
  (post-update) values in `update_item`.
- **Deterministic ordering**: `list_items` orders by `anchor, id` and
  `list_reminders` by `fire_at, id`, so listings with equal anchors/fires are
  stable across calls (previously `ORDER BY anchor` alone was
  nondeterministic).
- **Reminder dedupe**: `create_item_reminders` skips repeated offsets, so
  passing `[0, 0, 30]` no longer enqueues two reminders at the same fire time.

No migration.
Tests (`test_calendar.py` "Domain hardening (P14)" — create/update end-before-
start rejection, list tie-break; `test_reminders.py` "Domain hardening (P14)" —
offset dedupe, list tie-break).
Verified: 391 pytest pass, Ruff clean.

## V3 — Priority 13: mutation previews from typed data

The confirm/done message used the model-supplied `summary` (free text,
≤200 chars), so what the user was asked to confirm was whatever the model
phrased — not necessarily what would actually be written.

- `ActionKind` gains an optional `preview` builder
  (`(session, user, parsed) -> str`), registered alongside `executor` and
  `baseline` in `register_action_kind`.
- `actions.propose_action` calls the kind's preview (if present) at
  proposal time and stores the result as the action's `summary`, falling
  back to the model summary when a kind has no builder. The preview reads
  only the typed payload + user (its timezone), so it is exact.
- Calendar kinds (`create_item`, `update_item`, `complete_item`,
  `cancel_item`, `delete_item`, `create_reminder`, `cancel_reminder`) and
  workout kinds (`log_workout`, `schedule_workout`) each register a
  preview. Datetimes are shown as `YYYY-MM-DD HH:MM` in the user's zone
  (`_fmt`); item-scoped previews look up the current title (`_item_title`)
  and degrade to `#id` when the target is gone.
- No migration: `preview` reuses the existing `summary` Text column.

Tests (`tests/test_actions.py`, "Mutation previews derived from typed data
(P13)" section): create_item (fields + reminder offsets, model summary
overridden), create_item in a non-UTC user timezone (12:00 UTC → 14:00
Berlin), update_item lists only the changed fields, complete_item uses the
current title, create_reminder, and both workout kinds.
Verified: 386 pytest pass, Ruff clean.

## V3 — Priority 12: expand conversational mutation coverage (workouts)

Workout mutations were only reachable via the Mini App; conversation
had no `log_workout` / `schedule_workout` action kinds, so "I ran 5k this
morning" and "remind me to run tomorrow at 7" dead-ended.

- New `src/assistant/actions/workouts.py` registers two kinds:
  - `log_workout`: payload `name`, optional `started_at`,
    `duration_minutes` (>0), `notes` (≤4000), `perceived_effort` (1..10);
    executor delegates to `workouts_service.log_workout`.
  - `schedule_workout`: payload `name`, `starts_at`, optional
    `duration_minutes` (>0); executor delegates to
    `workouts_service.schedule_workout` (calendar item + start-time
    reminder).
- Both executors pass naive datetimes through; the services already
  interpret them in the user's timezone and normalize to UTC.
- `actions/__init__.py` imports the module so the kinds self-register.
- Kinds appear in the turn prompt automatically via `actions_doc`.

Tests (`tests/test_actions.py`, "Workout action kinds (P12)" section):
`test_execute_log_workout` (propose→confirm→execute, fields persisted,
status completed), `test_log_workout_payload_validation` (duration 0 and
effort 11 rejected at propose), `test_execute_schedule_workout_creates_item_and_reminder`
(item titled "Workout: Run", correct start/duration, exactly one pending
reminder).
Verified: 380 pytest pass, Ruff clean.

## V3 — Priority 11: real recent-entity references

Context (`chat.build_context`) only covered items anchored today / within 7
days and the next five pending reminders by fire time. An entity the user
created a moment ago but anchored far away ("book a call in a month", then
"move it to Tuesday") was not in the context at all, forcing a
data_requests round-trip just to reference their own last action.

Now the context includes two deterministic sections:

- `recent_items`: the user's 5 most recently updated/created calendar
  items (any status), deduped against the today/upcoming sections,
  rendered with id, kind, times, and status.
- `recent_reminders`: the 3 most recently created pending reminders,
  deduped against the fire-time list, rendered with id, message, fire
  time.
- `TURN_SYSTEM` tells the model to use these ids for "it" / "that" /
  "the one I just added" references and to clarify when several fit.

No migration: ordered by existing `updated_at` / `created_at` columns.
Test: `test_recent_entities_in_context` (far-anchored item and far-firing
reminder surface only via the recent sections; today item deduped).
Verified: 377 pytest pass, Ruff clean.

## V3 — Priority 10: read tools + deterministic entity resolution

Previously the calendar/reminders read tools dumped listings and relied
on the model to pick the entity the user referenced by name. Resolution
is now deterministic in the service layer:

- `calendar_service.resolve_item(session, user, text)`:
  case-insensitive exact title match, then substring, over **scheduled**
  items only. Returns `(best, candidates)`: unique match, ambiguous
  candidate list (sorted by anchor date then id), or no match.
- `reminders_service.resolve_reminder(...)`: same semantics over
  **pending** reminder messages, candidates ordered by `fire_at` then id.
- `run_read_tool` (calendar and reminders tools): when the model passes a
  `query`, the output starts with a resolution line — `match: <entity
  with id>`, `ambiguous: id=.., id=..`, or `match: none` — followed by the
  usual listings, so the model gets exactly one unambiguous id to use.
- `TURN_SYSTEM` / `TURN_FOLD_SYSTEM` prompts document the three line
  forms: use the matched id, set `clarification` on `ambiguous:`, and
  never guess.

Tests (5 new): exact/substring/empty/no-match resolution, ambiguous
candidate ordering, completed items excluded, pending-only reminder
matching, and the rendered `match:`/`ambiguous:`/`match: none` lines in
both read tools. Verified: 376 pytest pass, Ruff clean.

## V3 — Priority 9: lookup→mutation within the bounded two-call turn

Previously the second (fold) model call was a plain text call, so a turn
that first needed app data (e.g. "move standup to 20:00" — the item id is
not in the context) could only end in a text answer: the model could not
propose the mutation because the id only exists after the tool results.

Now the fold is a **structured** call (new `TURN_FOLD_SYSTEM` prompt in
`ai/prompts.py`; the plain-text `TURN_FINAL_SYSTEM` variant is gone):

- Call 1: `AssistantTurn` with `data_requests` (no reply) → `TOOL_FOLD`.
- Read tools run (bounded, deduped, user-scoped) and their results are
  rendered with the real entity ids.
- Call 2 (structured fold): returns the final `reply` plus any
  `actions`/`facts`, where action payloads use only ids shown in the
  tool results. `data_requests` are not offered and any stray ones are
  ignored — the turn never loops and never makes a third call.
- Phase C validates fold actions/facts through the same re-validation
  path (kind schema, payload schema, optimistic baseline capture,
  dedupe) as first-call proposals.
- `TURN_SYSTEM` rule updated: when the referenced item is not in the
  context and the user asked for a mutation, request the data (the fold
  resolves the id) instead of only asking for clarification.

Tests (3 new, 5 updated): `test_lookup_then_mutation_in_two_calls`
(call-1 lookup → fold proposes `update_item` with the real id and
captured baseline), `test_fold_data_requests_are_ignored` (no third
call), `test_fold_clarification_used_when_reply_blank`; existing
fold-path tests now assert the second structured call
(`structured_calls == 2`, `fold_system`) and
`test_no_transaction_spans_provider_calls` verifies no transaction spans
the structured fold either. Verified: 371 pytest pass, Ruff clean.

## V3 — Priority 8: formal turn state machine

The bounded two-call engine (`turns.run_turn`) previously branched on
ad-hoc field combinations (`if turn.data_requests and not reply`). It now
follows an explicit, documented state machine (module docstring in
`src/assistant/services/turns.py`):

- **`TurnState`** (StrEnum) — exactly one terminal state per turn after the
  single structured call: `TOOL_FOLD`, `DIRECT_REPLY`, `CLARIFICATION`,
  `EMPTY`.
- **`_classify_turn`** — deterministic total priority: (1) `data_requests`
  with no non-blank `reply` → `TOOL_FOLD`; (2) non-blank `reply` →
  `DIRECT_REPLY` (wins over any `data_requests` — a reply means the model
  answered from context, so the data requests are ignored); (3) non-blank
  `clarification` → `CLARIFICATION`; (4) otherwise `EMPTY`.
- **Transitions**: `TOOL_FOLD` is the only state that triggers the one
  bounded second model call; every state is terminal — the turn never
  re-enters the structured state, so the engine cannot loop. `EMPTY` (or a
  blank fold with no clarification) raises `chat.empty_turn` unless the
  turn proposed actions/facts, and Phase C is aborted before any write.
- `TurnResult.state` exposes the settled state for observability/tests.

Behavioral hardening: a whitespace-only `reply` alongside `data_requests`
now counts as "no reply" (previously truthy, skipping the tools); a blank
fold without clarification degrades to the same localized empty-turn
error instead of persisting a blank assistant message.

Tests (8 new): per-state end-to-end transitions
(`test_state_direct_reply` / `_tool_fold` / `_clarification` /
`_empty_with_proposal_succeeds`), the pure classification priority
(`test_classification_priority`), `DIRECT_REPLY` ignoring `data_requests`
end-to-end (`test_reply_wins_over_data_requests_end_to_end`), and the blank
fold degradation (`test_tool_fold_blank_fold_without_clarification_raises`).
Verified: 368 pytest pass, Ruff clean.

## V3 — Priority 7: remove long DB transactions around AI/network I/O

Previously the bot middleware's per-update session stayed in ONE open
PostgreSQL transaction across the whole turn: context reads, the structured
model call, the read tools (including the documents tool's embedding call),
the second model call, and the writes — a pooled connection held for the
entire (potentially tens-of-seconds) AI round trip.

Now the request lifecycle follows the same Phase A/B/C layout the worker
job handlers already use (commit → network I/O with no tx → short final tx):

- **`turns.run_turn`** (the bot's `on_text` path):
  - Phase A — build the bounded context, then `commit()` to release the
    connection before any model I/O.
  - Phase B — `chat_structured`, the bounded read tools, and the optional
    second `chat` all run with **no open transaction**; the tool loop is
    followed by a `commit()` so the second model call also starts clean.
  - Phase C — proposals, proposed facts, and the chat messages are written
    and committed in one short transaction. On `AIProviderError` /
    `LocalizableError` nothing from Phase C is committed, so the handler's
    fallback (persist the user message + localized reply) works unchanged
    and the middleware's final commit is a no-op.
- **`files.retrieve_chunks`**: commits after the lexical candidate query,
  so the embedding network call no longer runs inside the lexical read
  transaction. All three call sites (documents read tool,
  `build_context(retrieve=True)`, the API search route) are read-only at
  that point, so the internal commit is safe.
- **`chat.chat`** (legacy single-call path): same Phase A/B/C treatment —
  context read + commit, model call, write + commit.
- The file-ingestion pipeline (`files._run_pipeline`) was verified to
  already commit before every external I/O (download/embed) — unchanged.

New regression test: `test_no_transaction_spans_provider_calls` asserts
`session.in_transaction()` is False inside the structured call, the
documents tool's `embed_query`, and the second plain call.
Verified: 361 pytest pass, Ruff clean.

## V3 — Priority 6: honest Telegram delivery semantics

Audited the three delivery channels and made the code match the semantics
the docs promise:

- **Reminders** (`reminder_send` job): already durable at-least-once —
  `sent_at` is stamped only after a successful send, so a crash in between
  re-sends (a visible duplicate) rather than losing the reminder; a send
  failure re-queues with backoff (SPEC §8).
- **Digests** (`digest_send` job): same at-least-once shape (Phase A read /
  B send with no tx open / C re-fetch + stamp).
- **Nudges** (proactivity): previously *de facto* at-least-once — the
  `NudgeDelivery` dedupe row was only flushed and the per-user rollback on
  send failure wiped it, so a failed nudge was re-sent next pass. Now the
  dedupe row is **committed before the send** (at-most-once): a failed send
  loses the nudge instead of duplicating it. `evaluate_user` /
  `run_proactive_pass` docstrings state the semantics explicitly.
- Docs aligned: `docs/ARCHITECTURE.md` (proactivity note) and
  `docs/ASSUMPTIONS.md` #21 now say "committed before sending,
  at-most-once".

Test updated: `test_run_proactive_pass_isolates_user_failure` now asserts
the failed user's dedupe row survives and the next pass sends nothing.
Verified: 360 pytest pass, Ruff clean.

## V3 — Priority 5: caller-transaction rollback hazards

Audited every `rollback()` / `IntegrityError` site in `src/`:

- **Fixed — `digests.schedule_todays_digest`**: on a lost
  `(user_id, digest_date)` uniqueness race it caught `IntegrityError` and
  called `session.rollback()`, destroying the CALLER's open transaction.
  In the worker's multi-user `ensure_digest_jobs` pass that silently
  discarded every delivery/job row flushed for earlier users. Replaced
  with `pg_insert(...).on_conflict_do_nothing(index_elements=[
  "user_id", "digest_date"]).returning(...)` — the conflict branch now
  selects the winning row; no exception, no rollback, transaction stays
  intact (same pattern as `jobs.py`).
- **Verified safe**: `files.py` (worker handler owns its session; failure
  state recorded on a second connection), `proactivity.py`
  (`run_proactive_pass` deliberately scopes one commit/rollback per user),
  `worker/main.py` (worker owns claim/job-state txns), `bot/middlewares.py`
  (per-request session).

New test: `test_conflict_does_not_rollback_caller_transaction` (a conflict
in one user's schedule must not lose earlier users' flushed rows).
Verified: 360 pytest pass, Ruff clean.

## V3 — Priority 4: optimistic stale-data protection

A confirmed proposal can outlive the data it previewed: the user taps
Confirm a minute after the preview, and the executor would clobber
concurrent changes. The pending-action path is now guarded:

- `ActionKind` gained an optional `baseline` hook (registry,
  `src/assistant/actions/__init__.py`); `propose_action` merges its output
  into the stored payload at proposal time.
- `update_item` / `complete_item` / `cancel_item` / `delete_item` register
  `_item_baseline`, which stores the target's `updated_at` as
  `expected_updated_at` (internal payload field, excluded from the service
  kwargs in `exec_update_item`).
- Executors run `_assert_not_drifted` after re-loading the item: a
  mismatch raises `ActionStaleError` → the action is expired with
  `last_error` (no mutation applied).
- `session.refresh(item)` in `_require_item` / `_item_baseline` re-reads the
  committed row in the async context (a lazy refresh of an expired
  identity-mapped attribute raised `MissingGreenlet`).
- Scoping (ASSUMPTIONS #24): the guard covers the AI-proposed mutation
  surface only; direct Mini App API writes have no proposal gap.

New test: `test_drifted_entity_expires_action` (drift after proposal →
`ActionStaleError`, action expired, item untouched).
Verified: 359 pytest pass, Ruff clean.

## V3 — Priority 3: expiry without secret mutation on read

Read paths (`get_action`, `list_actions`) previously called `_lazy_expire`,
so a plain GET flipped a row to `expired` and flushed a write (a hidden
mutation on a read, and a write on what should be a read-only request). Now:

- `services/actions` splits the logic: a pure `_is_expired_now` /
  `effective_status(action)` reports an overdue proposed/confirmed action as
  `expired` WITHOUT writing; the mutating `_lazy_expire` is kept only for the
  write paths (via a new `_load_write` used by confirm/reject/execute). The
  durable `expired` write happens in those write paths and the worker's bulk
  `expire_actions` — never on read.
- `get_action` / `list_actions` are pure reads (no flush).
- `api/schemas.ActionOut` reports the effective status at the boundary
  (overdue → `expired`) so the UI reflects reality; `api/routes` reject guard
  uses `effective_status` so an overdue action 400s instead of transitioning.

Tests: `test_actions.py::test_read_does_not_mutate_overdue_action` (read
reports expired, stored row stays `proposed`, bulk pass durably flips it) and
`test_api.py::test_action_overdue_reports_expired_without_write` (GET reports
`expired`, reject 400s, DB row untouched). Verified: 358 pytest pass, Ruff clean.

## V3 — Priority 2: atomic PendingAction confirm + execute

Confirm and execute are now one atomic, row-locked unit so a double-click
(the same action confirmed from the Telegram bot and the Mini App, or two
racing requests) cannot double-apply a non-idempotent mutation (SPEC §3):

- `services/actions.confirm_and_execute_action()` loads the action row with
  `session.get(..., with_for_update=True)` (a `SELECT ... FOR UPDATE`), then
  lazily expires, idempotently returns a stored `last_result` for an
  `executed` action, rejects terminal states, transitions
  `proposed` → `confirmed`, and delegates to `execute_action` in the same
  transaction. Two concurrent calls serialize on the row lock: the first
  confirms + executes + commits; the second re-reads the terminal row and
  returns the stored result without re-applying.
- `api/routes.py` `POST /actions/{id}/confirm` and
  `bot/handlers.py` `on_action` confirm branch both now use the atomic path.
  `ActionStaleError` commits the in-transaction expiry then 409; `ValueError`
  (rejected/expired/no-longer-valid) commits then 400.

Tests (`tests/test_actions.py`): `test_confirm_and_execute_is_idempotent`
(second call returns the stored result, item not re-moved) and
`test_concurrent_confirm_executes_once` (two `asyncio.gather` sessions each on
their own connection race a `create_item` action; the FOR UPDATE lock serializes
them and the mutation lands exactly once). Verified: 356 pytest pass, Ruff clean.

## V3 — Priority 1: worker transaction ownership (commit this session)

`worker/main.py::_run_job` no longer wraps a job handler in an outer
transaction. New ownership contract (SPEC §5): the worker owns job CLAIMING
(`poll_once`) and the FINAL job state (`_complete` / `_fail`, each a short
transaction on its own session); each handler owns its domain transactions and
commits between external I/O stages, so no DB transaction spans the Telegram /
embedding network calls. Handlers updated:
- `reminders._handle_reminder_send` and `digests._handle_digest_send` now use
  Phase A (read + commit) → Phase B (Telegram send, no open tx) → Phase C
  (re-fetch + stamp `sent_at` exactly once) — durable at-least-once delivery,
  `sent_at` written after the send so a crash re-sends rather than drops.
- `files` ingestion already committed between download/extract/embed/chunk-write.

Tests: `tests/test_worker_ingest.py` (3) drive a REAL `files.ingest` job through
`JobWorker._run_job` — happy path (file → `indexed`, job → `completed`, lease
cleared), owner-token/lease protection (a stale token cannot complete the job),
and cancel-mid-ingest (the fake embedder blocks at the embed stage until the
cancel commits, so the run's chunk-write is provably skipped). Test infra:
`tests/conftest.py` now forces `DATABASE_URL` to the test database (the shell
exports the Docker hostname `postgres`, which is unresolvable from the host, so
app-side `get_session_factory()` calls like `files._record_failure` were
order-dependent on `test_api.py` importing first). Verified: 354 pytest pass,
Ruff clean.

---

## V2 (baseline before this V3 effort)

## P13 — Final documentation and report (this session)

- **`docs/ASSUMPTIONS.md`**: entries 20–23 (bounded typed conversational
  actions with the closed `kind` registry — built-ins `create_item` /
  `cancel_item`; deterministic deduped proactivity; lexical-gated RRF
  hybrid retrieval, K=60; automatic memory proposals that stay
  `proposed` until confirmed).
- **`docs/ARCHITECTURE.md`**: invariants 7–9 (bounded typed actions with
  commit-before-409/400; facts never auto-confirmed, `supersede_fact`
  semantics; deterministic deduped proactivity), worker paragraph now
  covers the proactive pass, code layout updated (`actions/` registry,
  proactivity service, 351 tests, 6 E2E specs, full migration chain).
- **`README.md`**: new "V2 features" section (conversational actions,
  long-term memory, proactivity, lexical-gated hybrid search, workout
  scheduling, file ingestion retry); E2E paragraph updated to 6 specs
  incl. the V2 spec via the `e2e/helpers/seed.ts` asyncpg seed helper.
- **`REPORT.md`**: header brought current (all 20 milestones complete);
  §1/§2/§3/§4 updated with milestones 18–20 (commits `38bc65e`…`a33fb9d`,
  `9a2e7e5`…`124a54c`, `6324766`…`82ea1d5`) and current verification
  numbers (351 pytest, 6/6 Playwright, 264/264 locale parity, acceptance
  13/13 green on 2026-09-23); new "## 10. Milestone 19 — V2 core" and
  "## 11. Milestone 20 — V2 surface, acceptance coverage, and final docs"
  sections with the final verification state.
- Every documentation claim was verified against tool results in this
  session (migration chain grepped in `alembic/versions`, action registry
  grepped in `src/assistant/actions/`, RRF/lexical gate read in
  `services/files.py`, locale parity computed, test counts from this
  session's pytest/Playwright/Ruff runs).
- `scripts/acceptance.sh` intentionally left unchanged: a SPEC grep found
  no acceptance/E2E mandate there, and all 13 production-like checks pass.

## P12 — Tests/acceptance for the V2 features

New E2E spec `e2e/tests/v2-features.e2e.ts` (single full-flow test) covers
the five V2 features against the real API + DB: (1) Actions inbox — three
seeded proposed actions: confirm → "Сохранено." + badge "выполнено" +
created item in Today; stale target confirm → 409 toast "Это действие
больше не актуально." + badge "истекло" after re-fetch, 0 buttons; reject
→ "отклонено". (2) Workout scheduling — missing-time validation toast,
Flatpickr date + 23:50 time pick, "Workout: <name>" lands in Today.
(3) File retry — a DB-seeded `failed` file (real bytes on disk) →
"Повторить индексацию" → "в очереди". (4) Fact supersede — replace form on
a confirmed fact → new proposed fact "предложен", old fact badge "заменён"
(no replace button). (5) Proactive settings card — default values
(weekly on, 22:00/08:00/3/120 мин), switch toggle → "Настройки сохранены."
+ restore. Supporting: `e2e/helpers/seed.ts` (`runDbScript` runs asyncpg
Python against the isolated `assistant_e2e` DB from the repo root);
`global-setup.ts` now also truncates `pending_actions`,
`proactive_settings`, `nudge_deliveries`. E2E caught a real production
bug the pytest suite masked: the confirm route raised 409/400 before
committing, so the session dependency rolled back the in-transaction
`expired` marking (pytest's client overrides `get_session` with the raw
shared session and never rolls back). Fix in
`src/assistant/api/routes.py`: commit before raising in both the
`ActionStaleError` and `ValueError` branches. i18n: added
`miniapp.fact_superseded` (ru "заменён" / en "superseded") +
`FACT_STATE_KEYS`/`FACT_STATE_TONES` entry in `miniapp/app.js`.
`scripts/acceptance.sh` left as-is (SPEC does not mandate E2E there); it
stays green. Verified: full Playwright 6/6, full pytest 351 passing,
Ruff clean, ru/en locale parity 264/264, `scripts/acceptance.sh` all green
(fresh DB + migrations).

Prior: PRIORITY 11 COMPLETE (Mini App integration of the V2 features).
`miniapp/app.js` + locales + CSS: (1) new ⏳ Actions tab (second in the
bottom nav) — pending-proposals inbox from `GET /actions`: kind icon,
summary, "expires {when}" meta, status badge (pending/done/rejected/
expired), Confirm/Reject buttons on proposed; 409 → localized
"no longer applies" toast; (2) workout scheduling card (name + start
datetime + optional minutes → `POST /workouts/schedule`); (3) "Retry
indexing" button on failed file cards (`POST /files/{id}/retry`); (4)
inline "Replace" form on proposed/confirmed fact cards
(`POST /facts/{id}/supersede`, one open at a time, survives re-renders);
(5) proactive-settings card in Settings (three role=switch rows:
enabled/weekly review/workout nudge, quiet hours from/until via the
time picker, max-per-day and min-interval via option sheets) with
isolated fetch failure (a /proactive-settings error never breaks the
core settings screen). `motivationRow` generalized to `switchRow`.
21 new i18n keys per language (ru/en parity holds). E2E updated: a11y
nav count 7→8, screens-audit settings rows 3→7 / switches 1→4,
"actions" added to the touch-target loop. Verified: full pytest 351
passing, Ruff clean, full Playwright suite 5/5 (incl. per-screen
audit), ru/en locale parity.

Prior: PRIORITY 10 COMPLETE (Mini App API extension). Added the API
surface for the V2 features (all in `src/assistant/api/routes.py` +
`schemas.py`, 12 new tests in `tests/test_api.py`): (1) pending-actions
inbox — `GET /actions?status=&limit=` (proposed-first, newest),
`POST /actions/{id}/confirm` (get → 404, rejected/expired → 400,
confirm+execute in one transaction; stale target → `ActionStaleError` →
409 + expired; idempotent replay returns executed without re-running),
`POST /actions/{id}/reject` (terminal states → 400); (2) fact supersede —
`POST /facts/{id}/supersede` proposes a `replace_fact` action (old fact
stays active until confirmed, 404 cross-user/rejected); (3) proactive
settings — `GET/PATCH /proactive-settings` (row auto-created, partial
PATCH, pydantic bounds 422); (4) file ingestion retry —
`POST /files/{id}/retry` + `files_service.retry_file` (only `failed`
retryable, `rejected`/other states → 400; new job with
`file:{id}:retry:{n}` idempotency key, old job cancelled,
`extra.retry_count` incremented); (5) workout scheduling —
`POST /workouts/schedule` creates a calendar item + reminder at
`starts_at`. Verified: full suite 351 passing, Ruff clean.

Prior: PRIORITY 9 COMPLETE (bounded proactivity pass, SPEC §11, commit
124a54c). Priority 9 done: deterministic state-derived triggers only (no
model calls) — `weekly_review` on Monday in the user's local time, once per
ISO week; `workout` once per user-local day when the last workout is None or
older than 48 h (`WORKOUT_STALE_HOURS`). New models `ProactiveSettings`
(per-user: enabled, weekly_review_enabled, workout_nudge_enabled,
quiet_hours_start/end wrapping midnight, default 22:00→08:00,
max_nudges_per_day, min_interval_minutes) + `NudgeDelivery` (durable dedupe
per (user_id, kind, period_key)); migration
`20260923_9c8d7e6f5a4b_proactivity`. `services/proactivity.py`:
`evaluate_user(session, user_id: int, now, send)` — all data via explicit
SELECTs (no ORM instance state: `Session.rollback` expires every object in
the session, async lazy reload raises MissingGreenlet); anti-spam gates in
order enabled → quiet hours (user TZ) → daily cap → min interval; delivery
row flushed BEFORE send so a crashed send still dedupes.
`expire_stale_actions` marks past-expiry proposed/confirmed pending actions
expired (bulk UPDATE). `run_proactive_pass`: per-user commit/rollback
isolation (one failed send rolls back only that user; retries next pass),
returns {nudges_sent, actions_expired}. Worker: `proactive_pass()` in the
run loop every `digest_interval` seconds (default 30). i18n `proactive.*`
keys in ru/en. Tests: `tests/test_proactivity.py` (11 — triggers, dedupe,
quiet-hours wrap, daily cap, min interval, disabled, user scoping, action
expiry, pass isolation + retry). Verified: full suite 339 passing, Ruff
clean.

Prior: MILESTONE 17 COMPLETE (configurable chat timeout + explicit Qwen
thinking mode with Telegram UX). Milestone 17 done: (1) `CHAT_TIMEOUT_SECONDS`
(default 180, `1 <= x <= 3600`) drives the chat OpenAI client `Timeout`
(`read=chat timeout`, `connect=10`, `write=30`, `pool=10`, `max_retries=0`)
for normal chat AND structured generation; the embedding provider keeps its own
short (60 s) float timeout; `APITimeoutError` is mapped to the narrow
`AITimeoutError` (subclass of `AIProviderError`) with the configured value
logged and no raw provider text reaching users. (2) `CHAT_THINKING_ENABLED`
(default `true`) is sent EXPLICITLY on every chat/structured completion as
`extra_body → chat_template_kwargs.enable_thinking` (the llama.cpp
OpenAI-compatible mechanism), built centrally in `OpenAIChatProvider._chat_options()`;
it never touches embeddings. (3) Timeout vs malformed response: a full
inference timeout fails cleanly after exactly ONE provider call (no second
equally-long inference); a malformed structured response may be retried once
with corrective feedback (Pydantic validation unchanged, `extra="forbid"`).
(4) Telegram UX: when thinking is enabled, a short localized temporary status
message ("Думаю…" / "Thinking…", key `ai.thinking`) in the user's persisted
language is sent before a slow AI op (draft + chat) and deleted on success,
provider error, and timeout; send/delete failures are logged and swallowed so
they never break the flow; no status when thinking is disabled; existing
typing actions preserved. (5) `.env.example` + README document both settings.
Tests: 22 new in `tests/test_thinking_ux.py` (settings defaults/validation,
timeout reaching client / embeddings unaffected, explicit llama.cpp option on
chat + structured, clean no-retry timeout, RU/EN status lifecycle incl.
provider error + timeout + delete-failure, disabled → no status); 4 existing
single-message bot tests pin `chat_thinking_enabled=False` (their assertions
are about persistence/fallback, not UX). Verified: full suite 257 passing,
Ruff clean, imports OK, `.env.example` loads into `Settings`, `docker compose
config` valid, ru/en locale parity.

Milestone 16 COMPLETE (production runtime-hardening pass). Milestone 16
done: (1) worker `MissingGreenlet` fix — `digests.ensure_digest_jobs` eager-
loads `User.settings` via `selectinload` (also `digest_send` handler);
regression tests in `tests/test_digests.py` (users WITH and WITHOUT a
`UserSettings` row, repeated passes, plus a test pinning the lazy-load hazard).
(2) Onboarding i18n complete — every onboarding/FSM/validation user-facing
string resolves through `t()` / `LocalizableError` in the user's persisted
language (timezone, digest time, cancellation, task creation, workout + fact
+ file validation, incl. `workouts.err_notes`); RU/EN onboarding + FSM
state-isolation tests in `tests/test_onboarding_i18n.py`; user-authored
content never translated. (3) Qwen3.5/llama.cpp NL task parsing —
`chat_structured` no longer sends OpenAI-only `response_format`; JSON contract
in the system prompt; `extract_json_object` (fences, thinking preambles,
trailing prose, braces-in-strings; prefers the LAST balanced object, skips
nested ones); corrective-feedback retry (2 attempts); structured logging with
secrets redacted (`_redact_for_log`); explicit confirm-before-mutate preserved;
relative dates ("сегодня"/"today") resolved in the user's timezone (prompt
carries tz + now); regression tests with realistic Qwen-style responses in
RU/EN. (4) API loopback-only: compose `api` port now `127.0.0.1:8000:8000`;
Postgres publishes no ports; README "Network exposure" documents the HTTPS
reverse-proxy path for a future public Mini App. (5) `scripts/acceptance.sh`
— production-like verification: fresh Docker Postgres, `alembic upgrade head`,
API start + `/healthz`, worker with several digest-scheduling iterations (no
`MissingGreenlet`), bot dispatcher wiring (Telegram mocked), RU/EN onboarding
tests, NL draft tests, full pytest on the fresh DB, Ruff, `docker compose
config`, loopback-only port-exposure audit. Verified: acceptance run fully
green, full suite 235 passing, Ruff clean.

Milestone 15 (production-ready per-user
internationalization) done: `src/assistant/i18n/` registry + `t()` translator
with `locales/{ru,en}.json` (RU default + fallback, never crashes on missing
keys), `user_settings.language` (NOT NULL, server default `ru`) with new
Alembic migration `e8a2c41b7f05`, bot language picker (Settings button +
`/language`, immediate re-render, persisted), background jobs resolving the
recipient's language at execution time, explicit AI answer-language
instruction + language-neutral draft schema, Mini App rendering from the
backend locale dictionaries (`/api/v1/i18n/*`) with settings persistence,
and API `language` in settings with 422 on unsupported codes. Verified:
fresh-DB migration, full suite 206 passing, Ruff clean, api/bot/worker
import-verified, ru/en locale parity, two-user two-language tests,
no global env-var language.

Milestone 14 (independent chat/embedding
providers + 384-dim pgvector) done: split `CHAT_*` / `EMBEDDING_*` config
with legacy `OPENAI_*` fallback, independent AsyncOpenAI clients behind a
composite `OpenAICompatibleProvider`, E5 prefixes centralized in the
embedding provider, llama.cpp-compatible `/v1/embeddings` (model+input
only) with client-side dimension validation, migration
`7b492f548c86` shrinking `file_chunks.embedding` to `vector(384)` with the
HNSW cosine index rebuilt. Verified on a fresh database: `alembic upgrade
head` clean, full suite 176 passing, `ruff check` clean, api/bot/worker
import-verified, DB column `vector(384)` + `ix_file_chunks_embedding_hnsw`
confirmed in the catalog.

## Completed

- Autonomous project scaffold and specification created.
- Architecture/assumptions/research docs written (`docs/ARCHITECTURE.md`,
  `docs/ASSUMPTIONS.md`, `docs/RESEARCH.md`).
- Bootstrap: `pyproject.toml` + `uv.lock`, package skeleton under `src/assistant`,
  config via pydantic-settings (`assistant/config.py`), async SQLAlchemy engine
  (`assistant/db/engine.py`), FastAPI app with `/healthz` + static Mini App mount
  (`assistant/api/main.py`), Alembic async migration environment (`alembic/`),
  Dockerfile + docker-compose (postgres/api/bot/worker), Mini App stub.
- Verified: `uv run` import of the app succeeds; DB connection + pgvector 0.8.6
  reachable via `assistant.db.get_engine()`; `alembic heads` loads cleanly.
- Milestone 2: ORM models for all 11 SPEC §21 entities in `src/assistant/models/`
  (users, user_settings, calendar_items, reminders, workout_logs, user_files,
  file_chunks with `Vector(1536)` + HNSW cosine index, chat_messages, user_facts,
  background_jobs, digests). Async Alembic env; initial migration
  `c390315de59f_initial_schema` applied to real PostgreSQL 17 (pgvector extension and
  HNSW index verified in catalog). Ruff clean.

- Milestone 3: durable PG job queue service (`src/assistant/services/jobs.py` —
  `FOR UPDATE SKIP LOCKED` claim, status transitions, exponential-backoff retries
  (30 s base), abandoned-lock TTL recovery, idempotency keys) + worker entrypoint
  `python -m assistant.worker.main`; 11 real-PostgreSQL tests in `tests/test_jobs.py`
  (including concurrent-claimer no-double-claim) passing; Ruff clean.
- Milestone 4: aiogram 3 bot foundation under `src/assistant/bot/` —
  `callbacks.py` (pydantic `CallbackData`: Menu/Settings/Item/Draft),
  `keyboards.py` (main menu incl. conditional Mini App WebApp button, settings,
  draft confirm/cancel), `states.py` (`TaskDraftStates` FSM), `middlewares.py`
  (DB session + user upsert), `handlers.py` (`/start`, `/help`, `/cancel`, main
  menu, settings, draft confirm/cancel, structured task-draft text parsing with
  validation, chat-message persistence), `main.py` (bot polling entrypoint).
  25 credential-free tests in `tests/test_bot_foundation.py` (callbacks,
  keyboards, draft parser, and 3 real-PostgreSQL handler flows) passing;
  full suite 36 passing; Ruff clean.

- Milestone 5: calendar service (`src/assistant/services/calendar.py` — create/
  get/update/complete/cancel/delete/list_today/list_upcoming/list_range with
  user-TZ normalization and user scoping) + reminder service
  (`src/assistant/services/reminders.py` — durable `reminder_send` jobs with
  idempotency keys, offset resolution for item-linked reminders, idempotent
  delivery handler registered via `assistant.worker.handlers`). Bot wiring:
  `items_kb` per-item complete/cancel buttons, `ItemCallback` handler,
  today/upcoming routed through the service, draft confirm creates the item
  plus optional `remind:` offsets (ASSUMPTIONS #11). `tests/test_calendar.py`
  (9) + `tests/test_reminders.py` (13) against real PostgreSQL; conftest
  autouse TRUNCATE of all tables in the fixture session's transaction.
  Full suite 59 passing; Ruff clean.
- Milestone 6: workout service (`src/assistant/services/workouts.py` — log/get/
  list/stats with timezone-aware streaks, schedule_workout creating a calendar
  item + start-time reminder). `calendar.create_item` gained an `extra` kwarg.
  Bot wiring: `WorkoutStates` FSM, `workouts_kb`, workouts menu section shows
  stats + recent logs, `log_workout` / `schedule_workout` flows in `on_text`
  with `name, minutes, effort` and `name, YYYY-MM-DD HH:MM` parsing.
  `tests/test_workouts.py` (7) against real PostgreSQL including ownership
  isolation; full suite 66 passing; Ruff clean.
- Milestone 7: AI layer `src/assistant/ai/` — `AIProvider` protocol +
  `OpenAICompatibleProvider` (AsyncOpenAI, narrow `AIProviderError` /
  `AIOutputValidationError`, bounded retries, json_schema response_format,
  `store=False` on completions to avoid provider-side storage — SPEC §15),
  `AITaskDraft` Pydantic schema, `DRAFT_SYSTEM` prompt. Bot flow: manual line
  format first, AI fallback for natural language (ASSUMPTIONS #12-13); AI
  draft stored typed in FSM state, previewed with ambiguities, confirmed
  before persisting (SPEC §6). Lazy `get_ai_provider()` keeps tests
  credential-free. `tests/test_ai.py` (9) incl. full NL->preview->confirm
  flow with a fake provider; openai 3.16.2 API specifics in RESEARCH.md.
  Full suite 75 passing; Ruff clean.

- Milestone 8: file uploads + ingestion + pgvector retrieval.
  `src/assistant/services/files.py` — `register_upload` (server-side UUID
  storage key, never the user filename; unsupported/oversize persisted as
  `rejected` with a visible error, no job), durable `files.ingest` job handler
  (download -> extract (txt/md/pdf/docx, `FileUploadError` on unreadable) ->
  chunk (word-boundary, overlapping) -> batch embed -> replace chunks ->
  `indexed`), idempotent on replay, visible `failed` state written in a
  separate transaction on error; `delete_file` (rows + job + disk),
  user-scoped `list_files`/`get_file`; `retrieve_chunks` hybrid (cosine
  distance over `Vector(1536)` + keyword ilike boost 0.25) with
  `format_citations`. Job-handler registration moved to leaf module
  `src/assistant/worker/registry.py` to break the worker<->services circular
  import (reminders + files register there; `worker.main` reads
  `registry.handlers`). AI provider gained `embed()` (batch, `text-embedding-
  3-small` default) and `embedding_model` config. Bot: `on_document` registers
  uploads and reports rejections. Config: `file_storage_dir`,
  `embedding_batch_size`. `tests/test_files.py` (17) against real PostgreSQL
  with a fake download + fake embedder; full suite 92 passing; Ruff clean.

- Milestone 9: user facts lifecycle + contextual AI chat (SPEC §14-15).
  `src/assistant/services/facts.py` — `propose_fact` (starts `proposed`;
  normalized dedupe key, category, provenance, confidence), idempotent
  `confirm_fact`/`reject_fact`, `supersede_fact` (old proposed/confirmed ->
  `superseded` with `superseded_by`, new fact created as `proposed`), owner-
  scoped `get_fact`/`list_facts`/`delete_fact`, and `confirmed_lines` (only
  CONFIRMED facts ever reach chat context; `flush` only, caller owns the tx).
  Facts are NEVER auto-confirmed: the user confirms via /remember buttons.
  `src/assistant/services/chat.py` — `build_context` assembles a selective,
  bounded block (recent messages up to `chat_history_messages`, today's and
  upcoming items, pending reminders, recent workouts, confirmed fact lines,
  top-3 retrieved file chunks + citations — never the whole DB); `render_
  context` marks all of it untrusted data; `chat()` calls the provider and
  persists both user and assistant `ChatMessage` rows only after a
  successful reply (flush only). `CHAT_SYSTEM` prompt in `ai/prompts.py`
  (answer in the user's language, no inventing, treat excerpts/facts as
  untrusted data). Config: `chat_history_messages`. Bot: `FactCallback` +
  `fact_kb`, `/remember` (propose + confirm/reject/delete buttons), `/facts`
  (status-listed), `on_fact` (idempotent confirm/reject/delete), free text in
  `on_text` now routes through `chat_service.chat` with an `AIProviderError`
  fallback that persists only the user message. `tests/test_facts.py` (11) +
  `tests/test_chat.py` (9) against real PostgreSQL with a fake provider;
  full suite 116 passing; Ruff clean.

- Milestone 10: durable morning digest + motivation + real delivery
  (SPEC §16-17). `src/assistant/services/notifications.py` — worker-side
  Telegram sender (lazy shared `Bot`, same token as the bot process;
  `send_text` raises on failure so jobs re-queue with backoff).
  `src/assistant/services/motivation.py` — deterministic, state-derived
  one-line motivation (overdue / upcoming workout / streak / empty
  schedule / generic), disabled by `motivation_enabled`.
  `src/assistant/services/digests.py` — `build_digest` (today, overdue,
  upcoming-7d, workout stats, motivation line), `schedule_todays_digest`
  (one `DigestDelivery` per (user, local date) via the `uq_digests_user_day`
  constraint + `digest:{user}:{date}` idempotency key; past digest times
  fire immediately), `ensure_digest_jobs` (all-users pass), `digest_send`
  handler (send-then-stamp `sent_at`, idempotent on replay).
  `calendar.list_overdue` added. Worker: periodic `schedule_digests()` pass
  every `digest_schedule_interval_seconds` (default 30 s), `notifications.
  close()` on shutdown; `worker/handlers.py` registers the digest handler.
  `reminders._handle_reminder_send` now delivers the message through the
  Bot API before marking sent (failures re-queue). Config:
  `digest_schedule_interval_seconds`. `tests/test_digests.py` (11) against
  real PostgreSQL with a fake sender; `tests/test_reminders.py` gained an
  autouse sender stub. Full suite 127 passing; Ruff clean.

- Milestone 11: Mini App (initData HMAC) + /api/v1 authed endpoints
  (SPEC §18-20). `src/assistant/api/auth.py` — `verify_init_data`:
  Telegram's exact initData HMAC (secret = HMAC-SHA256(b"WebAppData",
  token), over the sorted data-check-string; constant-time
  `hmac.compare_digest`), duplicate-parameter rejection, hash
  well-formedness, `auth_date` freshness (max age + future-skew
  tolerance), strict user validation (positive int id, bots rejected,
  JSON shape); `get_current_user` FastAPI dependency (initData header ->
  verified user -> upsert + commit -> 401 on any `InitDataError`).
  `src/assistant/api/schemas.py` — pydantic v2 request/response models
  (`ORMModel` with `from_attributes`). `src/assistant/api/routes.py` —
  `APIRouter(prefix="/api/v1")`: `GET /me`; calendar `today` /
  `upcoming?days`; items CRUD (`POST` 201 with source=miniapp + optional
  `remind_offsets_minutes`, `PATCH`, `complete` / `cancel`, `DELETE` 204);
  workouts (`POST` 201, `GET` list, `GET /stats`); files (`GET` list,
  `DELETE` 204, `GET /search?q&top_k` -> 502 when the embedding provider
  is unavailable); reminders (`GET`, `POST` 201, `POST {id}/cancel`);
  facts (`GET`, `POST` 201 provenance=miniapp, `confirm` / `reject`,
  `DELETE` 204); settings (`GET`, `PATCH` with IANA timezone validation
  -> 422). Naive datetimes are interpreted in the user's timezone;
  every route is user-scoped (404 across users); service `ValueError` ->
  400; pydantic errors -> 422. `src/assistant/api/main.py` — FastAPI
  lifespan (dispose engine) + `/health`/`/healthz` + static `/miniapp`
  mount. `miniapp/` — vanilla-JS SPA (tabs: today / upcoming / new /
  workouts / files / facts / settings) sending the raw
  `Telegram.WebApp.initData` as `X-Telegram-Init-Data`; all user input
  rendered via `textContent`; 401 -> "reopen the app" message.
  `tests/test_init_data.py` (17, pure unit, signed payloads with the real
  algorithm) + `tests/test_api.py` (21, real PostgreSQL via
  `httpx.ASGITransport` + `dependency_overrides`, signed initData, fake
  embedder). Ruff config: `extend-immutable-calls` for FastAPI
  `Depends`/`Query` (B008). `ruff format` is NOT a project gate —
  pre-existing files are unformatted; `ruff check` is the standard.
  Full suite 164 passing; Ruff clean.

- Milestone 12: full test-suite coverage check against SPEC §26. Every
  §26 bullet verified against the suite: health (test_api), user
  isolation (test_api/test_facts/test_workouts/test_files), calendar CRUD
  (test_calendar), structured task-draft validation (test_bot_foundation),
  confirm-before-write (test_bot_foundation + test_ai), reminder
  persistence/idempotency (test_reminders), background job claiming +
  concurrent SKIP LOCKED + worker completion/failure + abandoned-job
  recovery (test_jobs), workouts (test_workouts), file metadata lifecycle
  + chunk ownership isolation + semantic retrieval filtering
  (test_files), user fact states (test_facts), Mini App initData valid /
  invalid signature + expired auth_date (test_init_data), major API
  validation errors (test_api). External AI/Telegram HTTP calls mocked
  everywhere (fake providers / sender stubs). One gap found and closed:
  migrations were never exercised by tests — added
  `tests/test_migrations.py`: (1) creates a throwaway database
  (`ta_migration_test`), runs the real `alembic upgrade head` chain
  against it, asserts the stamped head revision, exact table set vs
  `Base.metadata`, pgvector extension, and the HNSW embedding index, then
  drops the database; (2) ORM-metadata table-set drift guard. The main
  test database is never touched. Full suite 166 passing; Ruff clean.

- Milestone 13: final verification (SPEC §31 + QWEN.md) + REPORT.md.
  Verified: `uv sync` OK; `docker compose config --quiet` valid;
  `docker compose build` produced api/bot/worker images; PostgreSQL
  17.11 + pgvector 0.8.6 healthy; `alembic upgrade head` applied from an
  empty database (`ta_fresh_final`, dropped afterwards); full suite 166
  passing on the dev DB and again on the fresh migrated DB;
  `ruff check .` clean; `assistant.api.main` / `assistant.bot.main` /
  `assistant.worker.main` import-verified (FastAPI 0.141 materializes
  included routers lazily — 26 /api/v1 endpoints confirmed by the API
  tests); worker smoke path ran 15 s on an empty queue and exited 0;
  SKIP LOCKED concurrency tests and 17 initData unit tests pass; grep
  found no TODO/FIXME/stub/placeholder in `src/` or `miniapp/`; README +
  docs present. `REPORT.md` written; nothing pushed to any remote.

- Milestone 14: independent chat/embedding providers (OpenAI-compatible).
  `src/assistant/config.py` — `chat_base_url` / `chat_api_key` /
  `chat_model` and `embedding_base_url` / `embedding_api_key` /
  `embedding_model` / `embedding_dimensions` (default 384); a
  `model_validator` fills them from the legacy `OPENAI_API_KEY` /
  `OPENAI_BASE_URL` when the provider-specific variables are absent and
  requires at least one credential source. `src/assistant/ai/provider.py`
  — `OpenAIChatProvider` (chat + chat_structured, `store=False`) and
  `OpenAIEmbeddingProvider` (`embed_documents` / `embed_query`) as
  independent AsyncOpenAI clients; the embedding provider applies the E5
  prefixes centrally (`passage: ` for stored chunks, `query: ` for
  queries) and validates every returned vector against
  `EMBEDDING_DIMENSIONS` (`EmbeddingDimensionError` on mismatch); requests
  send only `model` + `input` so llama.cpp `/v1/embeddings` works.
  `OpenAICompatibleProvider` is a composite over the two; `build_ai_provider`
  wires them from settings. The `AIProvider` protocol now exposes
  `embed_documents` / `embed_query` (replacing `embed`);
  `services/files.py` updated accordingly. `models/files.py`:
  `EMBEDDING_DIMENSIONS = 384`. Migration
  `7b492f548c86_embedding_vector_384`: drops the HNSW index,
  `batch_alter_table` drops + re-adds the column as `Vector(384)`
  (pre-production: existing vectors discarded), recreates the HNSW cosine
  index; downgrade restores `vector(1536)`. Tests: `tests/test_ai.py`
  rewritten for the split clients (independent base URLs/keys/models,
  legacy fallback precedence, credential requirement, E5 prefix
  application, llama.cpp request shape, dimension validation, composite
  routing); fake providers in `test_chat.py` / `test_files.py` /
  `test_api.py` adopt the new protocol. `.env.example`, README, and
  `docs/` updated. Verified: fresh-DB migration, 176 passing, Ruff clean,
  imports OK, `vector(384)` + HNSW index in the catalog.

- Milestone 15: production-ready per-user internationalization (i18n).
  `src/assistant/i18n/service.py` — `SupportedLanguage` (StrEnum: `ru`,
  `en`), `DEFAULT_LANGUAGE`/`FALLBACK_LANGUAGE = "ru"`,
  `SUPPORTED_LANGUAGES` (derived from the enum), `LANGUAGE_NAMES`,
  `language_name()`, `is_supported()`, `load_locale()` (independent copy of
  the `@cache`d flat dict), and `t(language, key, **kwargs)`: unknown
  language → RU, key missing from active locale → RU, key missing from RU →
  the key itself (never raises), `{param}` interpolation with a safe-format
  fallback; `Translator`/`for_language()` binding. Locale dictionaries at
  `src/assistant/i18n/locales/{ru,en}.json` (same key set, parity enforced
  by a test). Migration `e8a2c41b7f05_user_settings_language`:
  `user_settings.language` `VARCHAR(16) NOT NULL DEFAULT 'ru'`, server
  default, existing rows set to `'ru'`; downgrade drops the column.
  `models/users.py`: `language` column with Python + server defaults.
  `services/users.py` `upsert_user` never reads the Telegram
  `language_code` — new users keep the default `ru`. Bot: `LanguageCallback`
  (`prefix="lang"`), `settings_kb` gained a localized 🗣 language button,
  `language_kb` (one button per supported code, localized labels),
  `cmd_language` + `on_language` (persist, re-render settings + answer
  immediately in the new language, reject unsupported codes); every
  localized bot surface (start, help, menu, settings, drafts, tasks,
  workouts, files, facts, reminders, digests, motivation, errors,
  keyboards) now renders through `t(user.language, ...)`.
  `services/reminders.py`: `create_item_reminders` stores the raw user
  text; the "Напоминание: / Reminder:" wrapper is applied at delivery in
  the recipient's CURRENT language. `services/digests.py` +
  `motivation.py`: built at execution time in the recipient's language.
  `ai/prompts.py` `CHAT_SYSTEM` gained `{language}` ("Always answer in
  {language}"); `DRAFT_SYSTEM` stays language-neutral; user text is never
  translated. API: `Settings`/`SettingsUpdate` expose `language` (422 on
  unsupported codes); new authed endpoints `GET /api/v1/i18n/languages`
  (`[{code, label}]`) and `GET /api/v1/i18n/{locale}` (404 on unknown).
  Mini App `miniapp/app.js`: all UI strings fetched from the backend
  locale dictionary for the user's language (single source of truth, no
  localStorage copy), Settings tab gains a language select, saving
  persists via `PATCH /settings` and reloads the dictionary; initData
  auth unchanged. Ruff config: per-file `F811` ignore for test fixture
  imports. Tests: `tests/test_i18n.py` (30 — service/registry, parity,
  fallbacks, interpolation, column defaults, no language_code override,
  two-user scoping, persistence, start/language bot flows, reminder +
  digest language at execution, AI language instruction, neutral draft
  prompt, API read/update/422, i18n endpoints) + targeted updates in
  existing suites (reminders/digests/facts/files/chat/api) to the new
  execution-time wrapper semantics. README + docs updated. Verified:
  fresh-DB migration, 206 passing, Ruff clean, imports OK.

- Milestone 17: configurable chat timeout + explicit Qwen thinking mode
  (see status block at top): `CHAT_TIMEOUT_SECONDS` (default 180) drives the
  chat `Timeout` for chat + structured generation (embeddings keep their own
  60 s timeout), `APITimeoutError` → narrow `AITimeoutError` logged with the
  configured value; `CHAT_THINKING_ENABLED` (default true) sent explicitly via
  `extra_body → chat_template_kwargs.enable_thinking` on every chat/structured
  call (central `_chat_options()`, never embeddings); a full inference timeout
  fails cleanly after one provider call while a malformed structured response
  may be retried once; a temporary localized "Думаю…" / "Thinking…" status
  message (key `ai.thinking`) in the user's language is sent before and removed
  after slow AI ops (success / provider error / timeout), with send/delete
  failures swallowed; `.env.example` + README updated; 22 new tests in
  `tests/test_thinking_ux.py` + 4 existing single-message bot tests pin
  thinking off. Full suite 257 passing; Ruff clean.

- Milestone 16: production runtime-hardening pass (see status block at top):
  worker `MissingGreenlet` fix (eager `selectinload(User.settings)` in
  `ensure_digest_jobs` + `digest_send` handler) with regression tests;
  onboarding i18n complete via `t()`/`LocalizableError` (RU/EN + FSM
  state-isolation tests); llama.cpp/Qwen3.5-compatible structured parsing
  (no `response_format`, `extract_json_object`, corrective retry, redacted
  structured logging, relative dates in the user's timezone) with RU/EN
  regression tests; API published loopback-only
  (`127.0.0.1:8000:8000`, README "Network exposure"); new
  `scripts/acceptance.sh` production-like verification run (fully green).
  Full suite 235 passing; Ruff clean.

## Current milestone

- Milestone 18: Mini App production-hardening pass. Frontend rewritten as
  small vanilla ES modules (`miniapp/{index.html,styles.css,app.js,
  js/{api,telegram,ui,state}.js}`, vendored Flatpickr — no framework, no
  build step); root-cause fix of the `[object HTMLDivElement]` rendering
  bug (safe `el()` DOM construction, user content as text nodes, no
  innerHTML); `GET /` → 307 → `/miniapp` implemented in FastAPI;
  Telegram-native theming via `--tg-theme-*` variables (light/dark/custom,
  runtime `themeChanged`); Flatpickr (24h, ru/en) replaces native
  pickers; reusable bottom sheet/action sheet replaces native selects
  (selected state, Escape/outside-click/cancel, keyboard focus, ARIA);
  loading/empty/populated/error states on every data screen; backend fix:
  calendar `list_items` anchors on `coalesce(starts_at, due_at)` so
  due-date-only items appear in range views. E2E: Playwright + Chromium
  (devDependency only, never in the prod image) with a deterministic
  Telegram WebApp stub, env-gated `ASSISTANT_TEST_AUTH` dependency
  override (verified inert in production: 401 without initData),
  390x844 viewport, isolated `assistant_e2e` DB truncated via a single
  `TRUNCATE ... CASCADE` in `e2e/global-setup.ts` (wired as Playwright
  `globalSetup`), 5 specs green (a11y, 20-step scenario, per-screen
  audit, screenshots, theme), console guard (suppresses Chromium's
  spurious `net::ERR_ABORTED` on completed `204 No Content` requests),
  programmatic theme/layout/a11y/touch-target assertions, 6 reference
  screenshots in gitignored `test-artifacts/screenshots/`;
  `scripts/public_smoke.sh` read-only post-deploy check (executed against
  the live public URL: it correctly FAILs there only because the public
  server still runs a pre-deployment version); README fully updated.
  The per-screen audit spec found and fixed three real issues:
  `list_range` now returns items of every status (completed/cancelled
  items stay on their calendar day so the UI can show a badge and offer
  deletion), the settings motivation switch got a full 44px hit target
  (the 30px track is drawn centered inside it), and the audit's flatpickr
  Escape step focuses the picker input (flatpickr listens for Escape on
  its input, not the document).
  Verification: 264 pytest passing (fresh DB), Ruff clean, `docker
  compose config` valid, full Playwright suite 5/5 (incl. a per-screen
  audit spec), smoke PASS locally, prod-auth negative check 401, working
  tree clean, nothing pushed.

## Next

- Manual deployment of the current `main` to
  https://telegram-assistant.bacer.ru, then re-run
  `BASE_URL=https://telegram-assistant.bacer.ru bash
  scripts/public_smoke.sh` (expected PASS after deploy).
- Future work (out of scope for this run): real Telegram/OpenAI
  credential smoke tests, HTTPS reverse proxy deployment for the Mini
  App, CI pipeline, observability, additional languages (ru/en
  supported today).
