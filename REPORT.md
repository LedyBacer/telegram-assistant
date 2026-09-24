# Telegram Assistant — Milestone Report (V4: Correctness Closure)

Date: 2026-09-24
Scope: V1 core (SPEC §1–§31) + V2 upgrades (SPEC §42–§48) + the V3
hardening (prior Goals) + the **V4 correctness closure** (this
milestone). This report supersedes the 2026-09-24 V3 final report; where
they differ, this version is authoritative.

**Two corrections to the prior report, stated up front (honest by
design):**

1. **The V3 report claimed CI was verified; it was not.** The V3 GitHub
   Actions run (2026-09-23, workflow `ci.yml`) **rejected the workflow on
   `uv lock --check`** and never executed a single check. The V3 report's
   "CI present and runs" framing overclaimed. This is corrected in §3.
2. **The V3 test-count (496) is stale.** The suite has grown to 511 test
   functions — **530 collected items** after parametrization (16 spec
   files, 18 browser E2E specs).

## 1. What V4 closed

Eight correctness/verification goals, each with a commit reference:

- **§1–§8 — Job leases now protect DOMAIN SIDE EFFECTS (P0).** Before
  V4 the lease guarded only the *job row*, not the work: a worker that
  lost its lease mid-ingest could still commit file records and retrieval
  vectors under a stale lease. Now: the worker runs a supervised
  heartbeat task that renews the lease and flips a `lost` flag on expiry
  (`src/assistant/worker/main.py`); `_run_job` awaits the handler with
  `asyncio.wait(..., FIRST_COMPLETED)` so heartbeat death cancels the
  handler and never calls `complete_job`/`fail_job` (an expired lease can
  no longer own those transitions — `_owned_running_job` already
  enforces `lease_until >= now()`); and the file-ingest handler calls
  `lease.raise_if_lost()` immediately before its final domain commit
  (`src/assistant/services/files.py`), skipping the failure record when
  the lease is lost so the owner stays clean for recovery. (`9ea7c10`)
- **§9–§11 — Proactivity is cross-kind anti-spam gated.** Before V4 the
  anti-spam gates (quiet hours, max-nudges/day, min-interval) were read
  once at the top of `evaluate_user`, so a nudge sent earlier in the same
  pass could not suppress a later, lower-priority one. Now `gates_open()`
  re-runs **all** gates against the *current* delivery state before
  **every** reservation, with a deterministic priority (weekly review >
  overdue > workout) so the just-sent nudge's counters advance and
  suppress the rest of the pass. (`ad93ab4`)
- **§12–§16 — The Qwen turn protocol is structurally contradiction-free.**
  The turn-mode invariants, tool-construction contract, and model-output
  handling were audited for contradictions (e.g. prompts promising
  behavior the schema/state machine forbids) and tightened.
  (`11a2f7b`)
- **§25–§26 — Mini App wall-clock / timezone (already complete, verified).**
  The Mini App renders wall-clock, timezone-aware strings via a single
  `wallClock` helper (ru locale, `Intl`-backed) so the server never ships
  a wall time for the client to "interpret"; a regression suite
  (`e2e/tests/timezone*.e2e.ts`) proves rendering in a non-UTC browser
  timezone. Verified green in this run.
- **§41 — Named regression tests.** ~30 named scenarios audited against
  the suite; all map to existing named tests (see `PROGRESS.md` §41).
- **§39 — Readiness terminology is honest.** `/readyz` reports Postgres
  as `ok`/`error` (a real reachability probe) and AI providers as
  `configured`/`unconfigured` (a config-only check, no inference).
  Readiness is gated **only** on Postgres: an unconfigured embedding
  provider reports `unconfigured` and still yields 200 `ready:true`.
  (`421d22d`)
- **§40 — X-Request-Id is bounded; logging correlation audited.** The
  API middleware now reuses an inbound `X-Request-Id` **only** when it is
  ≤128 chars and matches `^[A-Za-z0-9._-]+$`; otherwise it mints a fresh
  `uuid4().hex`. This closes a log-bloat / log-injection vector (an
  unbounded caller-controlled id stamped onto every log line) and keeps
  the response header clean. The full log-correlation field set
  (`_CONTEXT_KEYS`) was audited and is already complete — no field was
  missing. (`3fab9dc`)

Full per-item detail, the §41 scenario→test mapping, and assumptions are
in `PROGRESS.md` and `docs/ASSUMPTIONS.md`.

## 2. Code layout

```
src/assistant/
  api/        FastAPI app, initData auth, /api/v1 per-domain routers,
              pydantic schemas, /healthz + /readyz (Postgres gate only)
  bot/        aiogram entrypoint, per-domain handler routers, callbacks
  ai/         provider protocol, chat/embedding providers, turn machine
  i18n/       language registry, t() translator, locales/{ru,en}.json
  models/     SQLAlchemy 2 async ORM (15 tables, pgvector HNSW index)
  services/   calendar, reminders, workouts, files, facts, chat, turns,
              actions, proactivity, digests, motivation, notifications,
              jobs (queue + leases), users
  actions/    action-kind registry (kind -> payload schema + executor)
  worker/     durable job loop (JobLease: heartbeat + raise_if_lost),
              handler registry, digest scheduler, proactive pass
  db/         async engine, session, Base
  logging.py  structured JSON logging, bounded X-Request-Id correlation
  config.py   pydantic-settings (env-driven, audited)
miniapp/      index.html + app.js + js/ SPA + vendor/ (Flatpickr
              self-hosted); all strings from backend locales
alembic/      async migration env, 9 revisions
tests/        27 test modules, 511 test functions (530 collected items),
              real PostgreSQL
e2e/          Playwright Mini App browser E2E (16 spec files / 18 specs)
scripts/      acceptance.sh — 22-step production-like verification run
.github/      credential-free CI workflow (V4: FILE_STORAGE_DIR moved to
              the step that needs it, fixing the V3 rejection)
docs/         ARCHITECTURE.md, ASSUMPTIONS.md, RESEARCH.md
```

Dependency versions (pinned in `uv.lock`): Python ≥3.12, aiogram
3.31.0, FastAPI 0.141.1, SQLAlchemy 2.0.54, asyncpg 0.31.0, Alembic
1.20.0, Pydantic 2.13.5, openai 3.16.2, pgvector 0.5.0 (PostgreSQL 17.11
+ pgvector).

## 3. Remote CI status (honest)

**Status: REMOTE CI VERIFICATION PENDING. No GitHub Actions run is green
in this milestone, and this report does not claim one is.**

Facts:

- The **V3 CI run was NOT green.** On 2026-09-23 the workflow
  `ci.yml` was rejected by GitHub before any step ran (the
  `uv lock --check` / workflow-acceptance failure), so V3 had **no**
  executed CI checks.
- V4 fixed the two root causes of that rejection:
  1. `uv.lock` was stale relative to `pyproject.toml` (a V3 dep bump
     never re-locked) → `uv lock` re-generated it; `uv lock --check` now
     passes.
  2. `FILE_STORAGE_DIR` was set at the job level using `${{ runner.temp }}`,
     which is **not available** in job-level `env` (only in step-level
     `env`). That undefined context broke workflow acceptance. V4 moved it
     to the `tests` step's `env` (`scripts/acceptance.sh` step 10 does the
     same).
- The **fixed workflow has not been pushed** to `origin/main`. This
  session is a single-turn, non-interactive run in which pushing to the
  remote is not permitted, so the corrected CI run **cannot be produced
  here**. The workflow, lockfile, and all 22 local acceptance checks are
  committed and reproducible; a remote green run is the one remaining,
  externally-gated step (a push + GitHub run), explicitly out of scope for
  this milestone.

**Local acceptance (this repo, no remote required) is fully green** — see
§4. It is the authoritative, reproducible verification for this
milestone.

## 4. Definition of Done (fresh run)

All checks executed on 2026-09-24 via `bash scripts/acceptance.sh` (22
steps, throwaway Docker PostgreSQL) — full pass:

| # | Check | Result |
|---|-------|--------|
| 1 | `uv sync` (lock resolved) | OK |
| 2 | Ruff (`uv run ruff check .`) | All checks passed |
| 3 | Import check (all entrypoints import cleanly) | OK |
| 4 | `uv lock --check` (lock matches `pyproject.toml`) | OK |
| 5 | Docker Compose config valid; no non-loopback published ports | OK |
| 6 | `docker compose build` (api, bot, worker images) | Built |
| 7 | Fresh PostgreSQL databases (`assistant` + `assistant_e2e`) created | OK |
| 8 | `alembic upgrade head` from empty database — full 9-revision chain | Applied cleanly |
| 9 | Migration invariants (head stamp, exact table set, pgvector, HNSW) on a throwaway DB | Passed |
| 10 | Full pytest suite against real PostgreSQL | **530 passed** |
| 11 | Playwright Mini App E2E (16 spec files / 18 specs, seeded `assistant_e2e`) | **18 passed** |
| 12 | Real-Postgres flows in-suite: turns, actions, ingestion, both retrieval arms, digest, proactive passes, job leases | Covered |
| 13 | Credential-free CI workflow in `.github/workflows/` (corrected; remote run pending — see §3) | Present |
| 14 | No required TODO/stub/fake implementation (grep audit) | Clean |
| 15 | `PROGRESS.md` matches actual state | Updated |
| 16 | Documentation matches code (README/SPEC/ASSUMPTIONS/ARCHITECTURE/this report) | Done |
| 17 | Git working tree clean after final commit | Verified |

The 530-item suite includes the V4 lease tests (`test_jobs.py`,
`test_files.py`), the cross-kind proactivity gate tests, the turn-protocol
contradiction tests, the readiness terminology tests (`test_readiness.py`),
the X-Request-Id bounding tests (`test_api.py`), the self-contained CI /
acceptance guards (`test_ci_workflow.py`), and the full V3 §52 regression
matrix. External AI/Telegram HTTP calls are mocked in tests
(fake providers, sender stubs, locally signed initData); production
integration code is import-verified.

## 5. Known limitations (honest list)

- **Remote CI verification pending.** The corrected CI workflow is
  committed but not pushed; no green GitHub Actions run exists for this
  milestone (single-turn, push not permitted). See §3.
- **No live-provider verification.** AI behavior is tested with
  deterministic in-test fake providers plus Qwen output fixtures; a real
  llama.cpp/Qwen deployment is untested here.
- **No live Telegram verification.** Bot logic is tested against in-test
  updates; no Bot API token is used. Polling behavior under real load is
  unverified.
- **Single-node assumptions.** One Postgres, one worker (lease+heartbeat
  makes >1 workers possible but untested at scale); no HA, no backup
  runbook, no metrics beyond `/healthz`/`/readyz` and JSON logs.
- **Retrieval quality ceiling.** Two-arm RAG is sound (each arm,
  filtering, merging, citations are tested) but recall/precision with an
  actual embedding model is unmeasured.
- **Small-model prompt brittleness.** The bounded turn protocol constrains
  a 9B-class model; weaker models may need prompt/schema tuning.
- **Mini App is vanilla JS by design** (no framework, no bundler) — a
  deliberate constraint, noted so it is not mistaken for a gap.

## 6. How to run

```bash
uv sync
cp .env.example .env            # fill TELEGRAM_BOT_TOKEN, CHAT_*/EMBEDDING_*
docker compose up postgres      # or point DATABASE_URL at any PG 16+ + pgvector
uv run alembic upgrade head
uv run python -m assistant.bot.main      # bot
uv run python -m assistant.api.main      # api (port 8000, /miniapp served)
uv run python -m assistant.worker.main   # worker
uv run pytest                     # full suite (needs PostgreSQL)
uv run ruff check .
bash scripts/acceptance.sh        # production-like end-to-end acceptance run
```

Or `docker compose up` for all four services. See `README.md` for
configuration, `docs/ARCHITECTURE.md` for the design rationale, and
`PROGRESS.md` for the V4 priority log.
