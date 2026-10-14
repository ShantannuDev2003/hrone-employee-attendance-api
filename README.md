# Employee Attendance & Analytics API

FastAPI + MongoDB backend for attendance tracking, manual regularization, and analytics reporting.

## Setup & Running

1. **Install Dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Environment Variables:**
   The application reads database configuration from real environment variables first, falling back to a local `.env` file if not set:
   - `MONGO_URI`: MongoDB connection string (default: `mongodb://localhost:27017`)
   - `MONGO_DB`: Target database name (default: `attendance_db`)

3. **Start the API:**
   ```bash
   uvicorn app.main:app --port 8000
   ```
   All required indexes (unique constraints on `employees.emp_code` and `attendance_logs.(emp_code, date)`, plus query and window indexes) are created idempotently on startup.

4. **Health Check:**
   `GET /health` returns HTTP 200 `{"status": "ok"}` when MongoDB answers a ping, or 503 if unavailable.

## Notes & Architecture
- **Single-File Implementation:** All models, helpers, business logic, endpoints, and aggregation pipelines are defined in `app/main.py`.
- **Database-Driven Analytics:** All four analytics endpoints (`employee_monthly`, `department_summary`, `late_leaderboard`, `department_trend`) and `GET /admin/explain/{endpoint}` run pure MongoDB aggregation pipelines with `$rank`, `$densify`, and `$setWindowFields` without in-memory Python loops.
- **Concurrency & Races:** Handled via unique compound indexes and atomic conditional updates (`update_one` with state match).
- **Compliance:** Full adherence to business rules R1–R10 (half-up rounding via `Decimal`, overnight shift calendar attribution, strict epoch milliseconds instant validation, zero `_id` leakage, and standard competition ranking).
