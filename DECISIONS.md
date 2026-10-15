# DECISIONS.md — Architecture and Engineering Trade-offs

## 1. Architectural shape

### Decision: contract-first, single-module FastAPI application

The application is implemented in `app/main.py`, consistent with the description in `openapi.yaml` that the code lives in this module and is started with `uvicorn app.main:app --port 8000`.

The module uses a lightweight layered arrangement within one file:

1. **Configuration and lifecycle** — environment loading, `MongoClient`, `db`, and `lifespan`.
2. **Domain helpers** — date parsing, epoch-millisecond validation, IST/overnight attribution, late/overtime/work-hour/half-day calculations.
3. **Request and response models** — Pydantic request models and serializers.
4. **Query and analytics builders** — MongoDB find filters and aggregation pipeline builders.
5. **HTTP handlers** — FastAPI routes for employees, attendance, analytics, and explain output.

**Why:** the assignment has a strict single-file implementation direction and a defined OpenAPI contract. Keeping the entry point and behavior together reduces submission complexity and makes the required invocation predictable.

**Trade-off:** this is not a fully separated MVC or repository/service architecture. The module is large (approximately 1,441 lines in the uploaded archive), so changes can be harder to navigate and unit-test in isolation. Do not split it during the assignment unless the contract permits it; if the service evolves later, separate schema definitions, domain calculations, repository operations, pipeline construction, and route handlers.

## 2. Technology choices

| Technology | Current use | Rationale and trade-off |
|---|---|---|
| Python | API and business rules | Readable implementation of date/time rules and domain calculations; runtime version should be verified as Python 3.11 for the assignment. |
| FastAPI | Routing, validation, HTTP errors, OpenAPI behavior | Pydantic integration and built-in validation produce clear 422 responses. |
| Pydantic v2 | Request models and field/model validation | Centralizes request constraints and helps prevent invalid values reaching database operations. |
| PyMongo | MongoDB client, indexes, writes, finds, aggregations, explain | Uses database-native uniqueness, conditional updates, and aggregation features; the application is coupled to MongoDB semantics. |
| MongoDB | Persistent employee and attendance collections | Unique indexes enforce business invariants under concurrent requests; aggregation pipelines handle analytics without moving the dataset into Python memory. |
| `python-dotenv` | Optional local environment loading | `load_dotenv(override=False)` makes `.env` a fallback rather than overriding actual process environment variables. The submitted assignment must not contain a `.env` file. |
| Uvicorn | ASGI server | Simple local execution via `uvicorn app.main:app --port 8000`. |
| pytest / pytest-cov (development tools) | API tests and coverage | The uploaded `pytest.ini` sets `pythonpath = .` and `asyncio_mode = auto`; the observed environment also has pytest, pytest-asyncio/anyio, and pytest-cov installed. These tools are not listed in `requirements.txt`, which currently lists runtime dependencies only. |

The current `requirements.txt` declares:
- `fastapi>=0.110`
- `uvicorn[standard]>=0.29`
- `pymongo[srv]>=4.7`
- `pydantic>=2.6`
- `python-dotenv>=1.0`

These are lower bounds, not pinned reproducible versions. For a time-limited assignment, avoid adding unrelated dependencies. For long-term reproducibility, pin tested versions or add a lock file if the assignment allows it.

## 3. Data integrity and concurrency decisions

### Unique database indexes

`ensure_indexes` creates a unique index on `employees.emp_code` and a unique compound index on `attendance_logs.(emp_code, date)`. Additional indexes support date pagination, employee/date lookup, late leaderboard filtering, and department/join-date lookups.

**Why:** uniqueness must be enforced at the database boundary, not only with a Python `find_one` check. Two concurrent requests can both pass a pre-check. The unique index is the final arbiter.

**Trade-off:** each index consumes storage and increases write cost. Keep indexes tied to a demonstrated query or invariant, and confirm their use with the `/admin/explain/{endpoint}` API.

### Atomic state transitions

Punch-out and regularization use conditional database updates to protect against conflicting state changes. Duplicate inserts are translated from `DuplicateKeyError` into a conflict response rather than leaking a server error.

**Why:** application-level check-then-write logic alone is not race-safe. Conditional writes let MongoDB verify the expected prior state as part of the mutation.

**Trade-off:** handlers must distinguish “not found” from “found but in an incompatible state” where the contract requires different HTTP statuses.

## 4. Time and numeric decisions

- **IST is explicit:** `IST` is represented as UTC+05:30, while request/response instants use epoch milliseconds as required by `openapi.yaml`.
- **Whole-second truncation:** incoming instants are truncated before calculations and persistence to make boundaries deterministic.
- **Overnight shifts:** `is_overnight_shift` and `get_attendance_date` attribute early-morning punch-ins to the prior shift date when required.
- **Decimal half-up rounding:** `round_half_up` and `Decimal` avoid Python's banker's rounding for work hours and reported values.
- **Derived values are stored:** `work_hours`, `late_minutes`, `overtime_minutes`, and `half_day` are computed during punch-out or correction, while analytics reads the stored derived fields as the contract requires.

**Trade-off:** these helpers add more code than relying on generic `datetime` arithmetic and `round()`, but they encode explicit business rules and avoid subtle boundary errors. Do not replace them with floats or naive datetime operations.

## 5. Analytics architecture

### MongoDB aggregation pipelines

The pipeline builders are:
- `build_employee_monthly_pipeline`
- `build_department_summary_pipeline`
- `build_late_leaderboard_pipeline`
- `build_department_trend_pipeline`

They use MongoDB aggregation to calculate monthly metrics, department summaries, leaderboard ranks, and daily department trends. The implementation uses features including `$lookup`, `$group`, `$rank`, `$densify`, and `$setWindowFields`.

**Why:** the assignment explicitly requires analytics to use MongoDB aggregation pipelines. This avoids fetching all attendance records into application memory and enables database indexes and execution plans to be evaluated.

**Trade-off:** pipelines are less familiar to some developers and can be version-sensitive. Test the required pipeline features on MongoDB 6+ rather than assuming that a test on a newer server proves compatibility with every supported version.

### Ranking semantics

The late leaderboard uses `$rank`, filters on rank after ranking, and then applies a deterministic secondary sort. This gives standard competition ranking and retains tied employees at the limit boundary.

**Trade-off:** the number of returned rows can exceed `limit` when there is a tie at the cutoff. This is intentional if required by the contract and should not be changed to `$denseRank` or a pre-ranking `$limit` without rechecking the spec.

### Explain endpoint

`explain_endpoint` exposes execution plans for the contract's named endpoint categories. It is useful for inspecting winning indexes and examined keys/documents.

**Trade-off:** explain output is MongoDB-version-dependent and can be verbose. Treat it as a diagnostic contract response, not a stable application-level schema beyond what `openapi.yaml` specifies.

## 6. Headcount and trend semantics

The department summary begins from eligible employee records and looks up attendance logs, allowing employees with no logs to contribute to headcount. The department trend densifies the date sequence and computes per-day headcount from employees whose `joined_on` is on or before that day.

**Why:** headcount is a property of eligible employees, not a count of employees who happened to generate attendance records. Densification preserves dates with no attendance events.

**Trade-off:** the summary can scan all employees eligible by period end and the trend performs per-date headcount lookups. This is appropriate for the assignment dataset, but should be measured as employee counts and date ranges grow.

## 7. Configuration and local data

`MONGO_URI` and `MONGO_DB` are read from process environment first, with local defaults of `mongodb://localhost:27017` and `attendance_db`. `load_dotenv(override=False)` supports local development without overriding environment variables.

**Operational rule:** never package `.env`, credentials, or `.venv`. The uploaded `final complete assignment.zip` did contain a `.env` file and a `.venv` directory, so the final artifact must be rebuilt with those excluded. The seeder `local_tools/gen_data.py` deletes documents from the target `employees` and `attendance_logs` collections before inserting fixture data; run it only against a disposable database.

## 8. Evolution and refactoring strategy

Priority should be given to correctness and delivery risk, in this order:

1. **Artifact hygiene:** exclude `.env`, `.venv`, `.git`, caches, and generated reports from the submission archive.
2. **Contract verification:** compare each route, parameter, status code, and response shape in `app/main.py` with `openapi.yaml`.
3. **Runtime compatibility:** run under Python 3.11 and MongoDB 6+.
4. **Targeted test additions:** cover missed statements and contract-relevant error paths from the 86% coverage report. Prefer boundary tests over tests written solely to raise the percentage.
5. **Operational performance:** preserve the explain endpoint and record representative `totalKeysExamined`, `totalDocsExamined`, and latency for the 100k fixture.
6. **Post-assignment refactor only if permitted:** split the monolith into modules while retaining `app.main:app` as a compatible entry point. Candidate modules are `config`, `schemas`, `domain/time_rules`, `repositories`, `analytics/pipelines`, and `api/routes`.

At much larger scale, the existing `DECISIONS.md` proposes incremental daily/monthly summary rollups using MongoDB Change Streams. That is a future option, not a current requirement; it introduces change-stream operations, rollup consistency concerns, and operational complexity, so it should be justified by measurements before adoption.

## 9. Validation evidence and open items

Observed in the supplied environment:
- `python -m pytest -v local_tools/test_api.py`: 20 tests passed.
- Coverage command: 20 passed, 86% statement coverage.
- `python -m compileall -q app local_tools`: succeeded in the earlier validation.
- `openapi.yaml` passed `openapi_spec_validator` in the earlier validation.
- R7 isolated-database check: all seven join-date working-day cases passed.
- MongoDB explain calls returned plans with index use for the tested requests.

Still to verify before submission:
- Python 3.11 compatibility (observed environment was Python 3.14.4).
- MongoDB 6+ compatibility (observed server was 8.0.1).
- Final required file paths and final ZIP contents.
- Absence of `.env`, `.venv`, and secrets in the final artifact.
