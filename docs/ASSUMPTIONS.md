# Assumptions

Ambiguous SPEC points resolved during autonomous development. Each entry records the
assumption, the date it was made, and the reason.

| # | Assumption | Date | Reason |
| --- | --- | --- | --- |
| 1 | Single `jobs` table with a `type` discriminator for all background work (file ingestion, digest generation, reminder firing) rather than per-job-type tables. | 2026-09-21 | SPEC requires durability in PostgreSQL only; one queue keeps the worker simple and uniformly testable. |
| 2 | Reminder scheduling is job-based: a `reminders` table stores fire time; the worker polls due reminders and enqueues/sends notifications. No external scheduler (APScheduler). | 2026-09-21 | Keeps the runtime to the four processes in SPEC (bot/api/worker/postgres); polling interval is bounded by `WORKER_POLL_INTERVAL_SECONDS`. |
| 3 | `APP_TIMEZONE` is the timezone for "today" boundaries (digest, daily stats). Default UTC. | 2026-09-21 | Digest idempotency is per (user, day); a single configurable zone avoids per-user TZ complexity not in SPEC. |
| 4 | Mini App is served by the API process as static files under `/miniapp/`; the bot links to `{PUBLIC_BASE_URL}/miniapp/`. | 2026-09-21 | Single image, one origin; no CDN/extra infrastructure per QWEN.md. |
| 5 | `PUBLIC_BASE_URL` is assumed reachable by Telegram users; TLS termination is out of scope. | 2026-09-21 | SPEC does not require certificate management; a reverse proxy may terminate TLS in front. |
| 6 | AI chat model is OpenAI-compatible and configurable via `OPENAI_BASE_URL`; no direct dependency on a specific provider. | 2026-09-21 | SPEC requires a replaceable provider abstraction. |
| 7 | File chunking: fixed character window `CHUNK_SIZE` (default 1000) with `CHUNK_OVERLAP` (default 150) overlap, split on paragraph/line boundaries where possible. | 2026-09-21 | Deterministic, testable chunking without an external tokenizer. |
| 8 | Uploads are stored on local disk under `storage/files/` keyed by a server-generated UUID; original filename is kept only as metadata. | 2026-09-21 | SPEC: never trust client filenames; no object storage service is permitted. |
| 9 | Embedding dimension is not fixed in the schema; the `jobs`/ingestion flow stores whatever vector the configured `EMBEDDING_MODEL` returns, and the HNSW index is created on the `Vector(1536)` default dimension for `text-embedding-3-small`. Switching embedding models requires an index/column migration. | 2026-09-21 | SPEC names `text-embedding-3-small` (1536-d) as the default; dynamic dimensions complicate the HNSW index. |
| 10 | API auth for Mini App endpoints is initData-derived (verified `user.id`) with no bearer tokens. | 2026-09-21 | SPEC describes Telegram initData HMAC validation as the Mini App auth mechanism. |
