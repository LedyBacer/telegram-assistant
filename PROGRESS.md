# Progress

Status: V3 PRIORITY 32 COMPLETE (Mini App Files screen has working
document search: loading/empty/error+retry states, results show source
file, 1-based chunk position, excerpt).
P26 (do-not-redesign constraint) is carried by every change in this
Mini App block: styles/components/navigation preserved, no framework.
Next: V3 Priority 33 (assistant action inbox in the Mini App —
GET /actions, validated action type, typed preview, target info,
stale/conflict reason; proposal editing optional).

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
