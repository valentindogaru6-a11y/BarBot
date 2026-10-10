# AI single-flight memory utility

- Used the architecture, permissions, and warehouse-domain summaries.
- They kept the guard process-local and separate from warehouse idempotency.
- Avoided broad storage, migration, admin, and production-document reads.
- No stale or misleading memory was found.
- Approximate value: helpful with small overhead.
- Recorded Gemini live-smoke success and Groq pending owner-only voice smoke.
