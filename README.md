# Employee Attendance & Analytics API

A FastAPI and MongoDB backend for employee records, attendance punch-in/punch-out, manual attendance regularization, monthly and department analytics, late-arrival leaderboard reporting, daily department trends, and MongoDB explain diagnostics. The HTTP contract and rules are defined by `openapi.yaml`; storage assumptions are described in `DATA_MODEL.md`.

> **Submission hygiene:** do not include `.env`, credentials, `.venv`, `.git`, Python cache directories, or generated coverage reports in the submitted archive. The uploaded archive reviewed for this README contained `.env` and `.venv`; rebuild the final ZIP with those excluded. The local seeder deletes existing documents from its target collections, so only run it against a disposable database.

## Features

- Employee creation and paginated employee listing.
- Attendance punch-in and punch-out.
- Manual correction of attendance records with an append-only history trail.
- Monthly employee metrics, department summary, late leaderboard, and department trend.
- MongoDB aggregation pipelines for all analytics; analytics should not be rewritten as Python-side loops.
- An explain endpoint for inspecting selected MongoDB query plans.
- Unique indexes and conditional updates to enforce important data-integrity rules under concurrency.
- Strict epoch-millisecond validation, IST calendar attribution, overnight shift support, and half-up numeric rounding.

## Architecture

The implementation is deliberately concentrated in `app/main.py`, in line with the contract's single-module direction. Within that file, the code is organized into configuration/lifecycle, time and business-rule helpers, Pydantic models and serializers, query/pipeline builders, and FastAPI route handlers.

### Directory tree

The following is the source-oriented tree of the uploaded archive, excluding runtime/build artifacts such as `.venv`, `.git`, `__pycache__`, `.pytest_cache`, and `htmlcov`. Do not include the prohibited `.env` file in the final submission.

```text
candidate_kit/
├── app/
│   ├── __init__.py              # Python package marker
│   └── main.py                  # FastAPI app, models, rules, MongoDB queries/pipelines, routes
├── local_tools/
│   ├── __init__.py              # Local tools package marker
│   ├── conftest.py              # Test fixtures and test database setup
│   ├── gen_data.py              # Generates a large test dataset; deletes target collections first
│   ├── r7_check.py              # Working-days/join-date regression check
│   └── test_api.py              # API behavior, validation, concurrency, analytics tests
├── sample_data/
│   ├── employees.json           # Sample employee documents
│   └── attendance_logs.json     # Sample attendance documents
├── .gitignore                   # Ignore rules for local/runtime artifacts
├── DATA_MODEL.md                # MongoDB collections, fields, and fixture expectations
├── DECISIONS.md                 # Architectural decisions and trade-offs
├── PLAN.md                      # Assignment implementation plan
├── PROBLEM_STATEMENT.md         # Assignment statement copy in Markdown
├── README.md                    # Setup, execution, and project guide
├── REVIEW.md                    # Defect and code review notes
├── WALKTHROUGH.md               # Implementation walkthrough
├── openapi.yaml                 # Authoritative API contract and business rules
├── pytest.ini                   # pytest discovery and asyncio configuration
├── requirements.txt             # Runtime Python dependencies
└── sample_seed.py               # Small sample-data loader
```

The assignment's required submission paths are `app/main.py`, `requirements.txt`, `REVIEW.md`, `DECISIONS.md`, and `README.md`. Supporting files may be useful during evaluation, but verify the assignment's exact packaging instructions before including optional files.

## Requirements

- Python **3.11** (assignment target).
- MongoDB **6.0 or newer**.
- A terminal opened at the project root (`candidate_kit/`).
- Network access to the configured MongoDB instance.

The previously observed development environment used Python 3.14.4 and MongoDB 8.0.1. That environment passed the tests described below, but a final run under Python 3.11 and MongoDB 6+ is still required for compatibility confidence.

## Setup

### 1. Create and activate a virtual environment

**Windows Command Prompt:**

```cmd
py -3.11 -m venv .venv
.venv\Scripts\activate
```

**macOS/Linux:**

```bash
python3.11 -m venv .venv
source .venv/bin/activate
```

### 2. Install runtime dependencies

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

`requirements.txt` currently contains:

```text
fastapi>=0.110
uvicorn[standard]>=0.29
pymongo[srv]>=4.7
pydantic>=2.6
python-dotenv>=1.0
```

The file lists runtime dependencies. The observed test environment also uses pytest and coverage tooling; install these separately when needed:

```bash
python -m pip install pytest pytest-asyncio pytest-cov httpx
```

If OpenAPI validation is needed and the validator is not installed:

```bash
python -m pip install openapi-spec-validator
```

### 3. Configure MongoDB

Start MongoDB locally or use an authorized MongoDB URI. The application reads:

- `MONGO_URI` — defaults to `mongodb://localhost:27017`
- `MONGO_DB` — defaults to `attendance_db`

Real process environment variables take precedence over dotenv values. For the assignment, prefer setting the environment variables rather than creating or submitting a `.env` file.

**Windows Command Prompt example:**

```cmd
set "MONGO_URI=mongodb://localhost:27017"
set "MONGO_DB=attendance_db"
```

**PowerShell example:**

```powershell
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_db"
```

Use a disposable database for tests that seed or mutate data. `local_tools/gen_data.py` deletes all documents in the `employees` and `attendance_logs` collections of its configured database before inserting its generated fixture. Never point it at a database whose data you need to keep.

### 4. Start the API

From the project root:

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

The application creates required indexes idempotently during startup. Keep the server terminal running while executing manual HTTP checks from a second terminal.

Health check:

```bash
curl.exe -i "http://127.0.0.1:8000/health"
```

Expected success response:

```json
{"status":"ok"}
```

FastAPI's interactive docs are available at `/docs`, but manual tests can be run directly with `curl.exe`; the OpenAPI source file is `openapi.yaml`.

## API endpoint inventory

The following paths are defined in `openapi.yaml`:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | MongoDB health check |
| `POST` | `/employees` | Create employee |
| `GET` | `/employees` | List employees with pagination/filtering |
| `POST` | `/attendance/punch-in` | Record punch-in |
| `POST` | `/attendance/punch-out` | Record punch-out |
| `GET` | `/attendance` | List attendance records |
| `PATCH` | `/attendance/{emp_code}/{date}` | Regularize an attendance record |
| `GET` | `/analytics/employees/{emp_code}/monthly` | Employee monthly analytics |
| `GET` | `/analytics/departments/summary` | Department monthly summary |
| `GET` | `/analytics/leaderboard/late` | Late-arrival leaderboard |
| `GET` | `/analytics/departments/{department}/trend` | Daily department trend |
| `GET` | `/admin/explain/{endpoint}` | Explain selected query/pipeline |

The authoritative field names, parameters, response shapes, statuses, and R1–R10 business rules are in `openapi.yaml`. Do not treat this table as a substitute for the contract.

## Tests and validation

Run the API test suite from the project root:

```bash
python -m pytest -v local_tools/test_api.py
```

Run coverage and create the HTML report:

```bash
python -m pytest --cov=app --cov-report=term-missing --cov-report=html
```

Compile the application and local test utilities:

```bash
python -m compileall -q app local_tools
```

Validate the OpenAPI document if `openapi-spec-validator` is installed:

```bash
python -m openapi_spec_validator openapi.yaml
```

The HTML coverage report is written to `htmlcov/`; do not include it in the submission ZIP unless specifically requested.

### Latest observed verification

In the supplied Windows environment:
- `python -m pytest -v local_tools/test_api.py` — **20 passed**.
- Coverage run — **20 passed**, `app/main.py` at **86% statement coverage** (532 statements, 73 missed).
- Earlier compile check completed without errors.
- Earlier OpenAPI validation reported `openapi.yaml: OK`.
- Direct curl checks returned `200 OK` for tested analytics/explain requests and `422` for invalid month/date/range inputs.
- `local_tools/r7_check.py` passed all seven join-date working-day cases when the API was configured to use an isolated `hrone_r7_test` database.

These results are from Python 3.14.4 and MongoDB 8.0.1, so rerun them under the assignment target versions before final submission.

## Final pre-submission checklist

- [ ] Confirm `app/main.py`, `requirements.txt`, `REVIEW.md`, `DECISIONS.md`, and `README.md` are present at the submission root.
- [ ] Run tests using Python 3.11.
- [ ] Verify behavior on MongoDB 6+.
- [ ] Compare endpoints and response shapes against `openapi.yaml`.
- [ ] Keep all analytics in MongoDB aggregation pipelines.
- [ ] Remove `.env`, `.venv`, `.git`, caches, and generated reports from the final ZIP.
- [ ] Ensure no credentials or secrets are included.
- [ ] Rebuild the ZIP and inspect its file listing before upload.
