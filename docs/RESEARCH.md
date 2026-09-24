# Research notes

Library/API usage verified during implementation. Context7 MCP (`mcp__context7__query-docs`)
was used for the items marked **(Context7)**; others come from stable, version-pinned
official documentation.

## Context7 lookups performed

| Library (Context7 ID) | Query | Outcome |
| --- | --- | --- |
| `/aiogram/aiogram` | Dispatcher, Router, CallbackData, FSM usage in aiogram 3 | Success |
| `/openai/openai-python` | AsyncOpenAI, `chat.completions.parse` with Pydantic `response_format`, embeddings | Success |
| `/websites/alembic_sqlalchemy` | Async Alembic `env.py` template | Success |
| `/pgvector/pgvector` | Extension and HNSW index syntax | Success |
| `/pgvector/pgvector-python` | SQLAlchemy `Vector` type + HNSW cosine index DDL | Timed out; details written from the stable pgvector docs |

Per QWEN.md, only the successful lookups above are claimed as Context7-assisted.

## aiogram 3 (v3.13+)

- Entry point: `Dispatcher()` + `dp.include_router(router)` + `dp.start_polling(bot)` inside
  `asyncio.run(main())`.
- Sub-routers: `Router()`; callback handlers use
  `class MyCallback(CallbackData, prefix="my")` with typed fields and
  `@router.callback_query(MyCallback.filter(F.foo == "demo"))`.
- FSM: `FSMContextMiddleware` is wired automatically by the Dispatcher;
  `await state.set_data(...)` / `await state.update_data(...)`;
  `FSMStrategy.USER_IN_CHAT` default isolates state per (user, chat).
- Media uploads: `TelegramObject`/`Bot.download` helpers; bot token passed to
  `Bot(token, parse_mode=...)`; `aiogram.F` for filter expressions.

## OpenAI Python SDK (v1.40+, OpenAI-compatible endpoints)

- `AsyncOpenAI(api_key=..., base_url=..., max_retries=0)` — `base_url` allows
  swapping in any OpenAI-compatible provider (also honors `OPENAI_BASE_URL`).
- Structured outputs (what the app uses): the JSON contract is appended to the
  system prompt, a plain `chat.completions.create` is issued with **no**
  `response_format` (llama.cpp rejects/ignores the OpenAI-only
  `json_schema` type), and the model's content is parsed with
  `extract_json_object` before Pydantic validation:
  ```python
  system = system + JSON_OUTPUT_INSTRUCTION  # "reply with ONLY a single valid JSON object"
  completion = await client.chat.completions.create(model=..., messages=[...])
  data = extract_json_object(completion.choices[0].message.content or "")
  payload = Model.model_validate(data)
  ```
- Embeddings: `resp = await client.embeddings.create(model=..., input=...)`;
  vector is `resp.data[0].embedding` (list[float]).

### openai 3.x specifics (verified against the installed 3.16.2)

- `APIError.__init__(message, request, *, body)` — the 2.x
  `(message, response, body)` signature is gone; construct with an
  `httpx.Request`, not an `httpx.Response`.
- `client.base_url` is normalized with a trailing slash
  (`"https://ex/v1"` → `URL("https://ex/v1/")`).
- Plain `chat.completions.create` accepts
  `response_format={"type": "json_schema", "json_schema": {...}}` on OpenAI;
  the provider does **not** use it (llama.cpp rejects/ignores it) and instead
  carries the JSON contract in the system prompt, validating the JSON content
  client-side (see ASSUMPTIONS #13).

### llama.cpp server compatibility (chat + embeddings)

- llama.cpp's `server` exposes OpenAI-compatible `/v1/chat/completions` and
  `/v1/embeddings`; `AsyncOpenAI(base_url="http://host:port/v1")` works
  against it for both.
- Embedding requests use only the `model` + `input` fields — llama.cpp does
  not support OpenAI-only options such as `dimensions`, so the provider never
  sends them. The dimension of the returned vectors is validated client-side
  against `EMBEDDING_DIMENSIONS`.
- Chat requests send `store=False` (SPEC §15); llama.cpp ignores unknown
  fields, and OpenAI-compatible servers use it to skip provider-side
  conversation storage.

## Alembic async (1.13+)

- `alembic init -t async`.
- `env.py` `run_migrations_online()` → `asyncio.run(run_async_migrations())`.
- `run_async_migrations` builds `async_engine_from_config(..., prefix="sqlalchemy.",
  poolclass=pool.NullPool)` and does
  `async with connectable.connect() as connection: await connection.run_sync(do_run_migrations)`.
- `do_run_migrations` calls `context.configure(connection=..., target_metadata=...)` then
  `with context.begin_transaction(): context.run_migrations()`.

## PostgreSQL / pgvector (17, pgvector 0.8.x)

- `CREATE EXTENSION IF NOT EXISTS vector;`
- Store `Vector(384)` columns; HNSW index:
  `CREATE INDEX ... ON table USING hnsw (embedding vector_cosine_ops);`
- Cosine distance operator: `embedding <=> $1` (ascending = most similar first).

## Durable queue pattern

- Single `jobs` table; workers claim with
  `SELECT ... WHERE state = 'pending' AND available_at <= now()
   ORDER BY available_at, created_at FOR UPDATE SKIP LOCKED LIMIT n`.
- States: `pending → running → completed | failed`; `available_at` drives retries with
  backoff; a recovery step re-queues `running` jobs whose `locked_at` is older than a TTL
  (abandoned worker). Idempotency key column carries a unique partial index for one-time jobs.

## Telegram Mini App auth

- The Mini App passes `initData` (a query string of key=value pairs). Verify:
  1. Rebuild `check_string` = every line except `hash`, sorted by key, joined with `\n`.
  2. `secret = HMAC_SHA256(b"WebAppData", bot_token)`;
     `expected = HMAC_SHA256(secret, check_string).hexdigest()`.
  3. Constant-time compare against `hash`; reject if missing.
  4. Reject if `auth_date` is older than `MINIAPP_AUTH_MAX_AGE_SECONDS`.
  5. Parse `user` JSON from the payload; never trust client-declared user IDs.

## Hybrid retrieval (updated for V3 P17/P18/P19/P20)

- Lexical arm: PostgreSQL full text over a `simple`-config tsvector column —
  an OR-style `to_tsquery` built from normalized terms, ranked with
  `ts_rank`, always filtered by `user_id` (ownership isolation).
- Semantic arm: `FileChunk.embedding.cosine_distance($query_vec)` over the HNSW
  cosine index, joined to `user_files` for the source filename, always filtered
  by `user_id` and `embedding IS NOT NULL`.
- The two arms run **independently — there is no lexical prerequisite** (a
  paraphrase with zero token overlap is still discoverable); the ranked lists
  are fused with Reciprocal Rank Fusion (`RRF_K=60`), and fused candidates below
  the RRF floor are dropped.
- Relevance policy: a vector candidate is dropped when its cosine distance
  exceeds `RETRIEVAL_MAX_DISTANCE` (default 0.9); lexical candidates are always
  kept. An embedding outage (or a chat-only deployment) degrades to
  lexical-only results.
- Adjacent chunks from the same file within `RETRIEVAL_MERGE_GAP` positions are
  merged into one bounded excerpt; citations are generated by the application
  (filename + range), never by the model.
- Document text is treated as untrusted data: it is chunked/embedded and
  retrieved, never interpreted as instructions.
