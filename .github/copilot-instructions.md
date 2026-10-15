# Project rules (HROne attendance API)
- Single-file app: ALL application code goes in app/main.py. Run with
  `uvicorn app.main:app --port 8000`. Python 3.11+, FastAPI, PyMongo.
- Sources of truth: PROBLEM_STATEMENT.md, openapi.yaml, DATA_MODEL.md.
  Follow field names and status codes EXACTLY. Never invent fields.
- Read MONGO_URI and MONGO_DB from real env vars first; .env is only a
  fallback. Never hard-code connection details.
- Create all indexes at startup, idempotently.
- Instants are integer epoch milliseconds in the API, BSON UTC datetimes
  in Mongo. Dates and shift times are IST (UTC+05:30).
- Round half-up with Decimal, never Python round().
- Analytics must be MongoDB aggregation pipelines, not Python loops.
- Races must be handled with unique indexes and atomic conditional
  updates, never read-then-write.
- Never return 500; use 404, 409, 422 as the contract says.
- Never commit .env, secrets, a Dockerfile, a venv, or the kit files.
- Keep code readable, with short comments explaining the "why".