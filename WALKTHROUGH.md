# Comprehensive Walkthrough: Employee Attendance & Analytics API

Welcome! This document provides a complete, plain-language tour of the codebase, business rules, MongoDB aggregation pipelines, index strategy, concurrency design, and 15 live-interview questions (with on-the-spot code modification examples).

---

## 1. Architectural Tour of `app/main.py`
cl
All application logic resides in `app/main.py` for simplicity of packaging and deployment. The file is structured into clear logical sections:

1. **Configuration & Environment:**
   - Uses `python-dotenv` with `load_dotenv(override=False)` so that real system environment variables (`MONGO_URI`, `MONGO_DB`) always take precedence over `.env`.
   - Initializes `MongoClient` with `tz_aware=True` so all BSON datetimes returned by PyMongo are explicitly timezone-aware in UTC.

2. **Lifespan & Index Creation:**
   - Uses FastAPI's modern `lifespan` context manager.
   - At startup, `ensure_indexes()` runs idempotently to build unique indexes and query-optimizing compound indexes before any request is served.

3. **Validation & Type Boundaries:**
   - `EpochMillis`: A custom Pydantic type validator that verifies instants are integers within `[100000000000, 4102444800000]`. Bools (which subclass `int` in Python), floats, strings, and seconds-range values are strictly rejected with HTTP 422.
   - `truncate_to_seconds`: Truncates epoch milliseconds to whole seconds (`ms // 1000 * 1000`) before calculations or database writes per Rule R1.

4. **Business Math & Rounding:**
   - `round_half_up`: Uses Python's standard `decimal.Decimal` with `ROUND_HALF_UP` to prevent banker's rounding (round-half-to-even) or IEEE-754 floating point imprecision.
   - `get_attendance_date`: Accurately handles overnight shifts (where `shift_end <= shift_start`). If punch-in occurs before `shift_end` in IST, the record belongs to the previous calendar day.
   - `compute_late_minutes`: Checks if arrival strictly exceeds 10:00 (600 seconds) after `shift_start`. If late, returns `diff_seconds // 60`.
   - `compute_overtime`: Computes floor minutes between shift end and punch-out, requiring at least 30 minutes. Handles overnight shift boundaries where shift end falls on the next calendar day.
   - `compute_work_hours`: `(punch_out - punch_in).total_seconds() / 3600`, rounded half-up to 2 decimals.
   - `compute_half_day`: Returns `True` if rounded `work_hours < 4.50`.

5. **Serialization:**
   - `serialize_employee` & `serialize_attendance`: Strip internal MongoDB `_id` and ensure no `_id` or synthetic `id` is leaked. Convert BSON UTC datetimes back into integer epoch milliseconds at the API boundary.

6. **Aggregation Pipeline Builders:**
   - Dedicated helper functions (`build_employee_monthly_pipeline`, `build_department_summary_pipeline`, `build_late_leaderboard_pipeline`, `build_department_trend_pipeline`, `build_attendance_list_query`) construct the queries and pipelines. Both the production analytics endpoints and `GET /admin/explain/{endpoint}` reuse these identical builders.

---

## 2. Deep Dive: Business Rules (R1 - R10)

| Rule | Title | Summary & Implementation Details |
|---|---|---|
| **R1** | Time zone and attendance date | All shifts and calendar dates are Indian Standard Time (IST, UTC+05:30). If a shift is overnight (`shift_end <= shift_start`, e.g., 22:00 to 06:00), a punch-in before 06:00 IST belongs to the previous day's shift. Instants are truncated to whole seconds. |
| **R2** | Late grace period | Grace period is 10 minutes. A punch-in is late only if strictly more than 10:00 after shift start (diff > 600s). When late, `late_minutes = diff_seconds // 60` from shift start (e.g. 09:40:00 -> 0; 09:40:01 -> 10; 10:15:59 -> 45). |
| **R3** | Overtime | Whole minutes (floor) after `shift_end`, counted only if at least 30 minutes; otherwise 0. For overnight shifts, `shift_end` is calculated on `date + 1 day`. |
| **R4** | Work hours | `(punch_out - punch_in)` in seconds / 3600, rounded half-up to 2 decimals via `Decimal(seconds) / Decimal(3600)`. `null` until punch-out. |
| **R5** | Half day | Evaluated after punch-out: `work_hours < 4.50` (using the rounded value). Counts as 0.5 of a present day. |
| **R6** | Presence | `PRESENT`, `WFH`, and `ON_DUTY` count as present. `ABSENT` and `LEAVE` do not. |
| **R7** | Working days | Monday through Friday only. Mid-month joiners count working days starting from `joined_on`. Weekend records add to overtime/late totals but not to `present_days`. A weekday with no document is simply missing, not `ABSENT`. |
| **R8** | Reported numbers | 2 decimals half-up for counts/hours/percentages; 4 decimals half-up for rates (`attendance_rate`, `moving_avg_7d`). |
| **R9** | Headcount | Employees with `joined_on <= period_end`, including employees with 0 attendance logs. |
| **R10** | Pagination | 1-based page indexing, `page_size` max 100 (default 20). `total` reflects active filters. |

---

## 3. Aggregation Pipelines: Stage-by-Stage Breakdown

### 1. `department_summary` (`GET /analytics/departments/summary`)
- **Stage 1 (`$match`):** Matches employees where `joined_on <= end_of_month` (and optional `department`). Uses the index `(joined_on, department)`.
- **Stage 2 (`$lookup`):** Correlated lookup to `attendance_logs` on `emp_code` within the month `[start_date, end_date]`.
- **Stage 3 (`$unwind`):** Unwinds `logs` with `preserveNullAndEmptyArrays: True`. This ensures employees with 0 logs remain in the stream.
- **Stage 4 (`$group`):** Groups by `department`:
  - `employees: {"$addToSet": "$emp_code"}`: Accurate headcount per department including zero-log employees.
  - `present_days`: Sums 1.0 (or 0.5 for half day) only for Mon-Fri present logs.
  - `valid_work_hours`: Pushes `work_hours` for present logs having a non-null work duration.
  - `late_count`, `total_late_minutes`, `leave_count`, `on_duty_count`.
- **Stage 5 (`$project`):** Formats output, calculating `avg_work_hours` as `$avg: "$valid_work_hours"` (average over records, not average of averages).
- **Stage 6 (`$sort`):** Sorts by `department` ascending.

### 2. `late_leaderboard` (`GET /analytics/leaderboard/late`)
- **Stage 1 (`$match`):** Matches attendance logs in the month having `late_minutes > 0`. Directly backed by index `(date, late_minutes, emp_code)`.
- **Stage 2 (`$group`):** Groups by `emp_code`, summing `total_late_minutes` and counting `late_count`.
- **Stage 3 (`$lookup`):** Joins with `employees` to retrieve employee name and department.
- **Stage 4 (`$unwind`):** Unwinds employee details (ignoring logs with missing employee records).
- **Stage 5 (`$match`):** Optional department filter applied before ranking.
- **Stage 6 (`$setWindowFields`):**
  - `sortBy: {"total_late_minutes": -1}`
  - `output: {"rank": {"$rank": {}}}` computes standard competition ranking (1, 2, 2, 4).
- **Stage 7 (`$match`):** Filters `rank <= limit` (applied *after* ranking, properly including cutoff ties).
- **Stage 8 (`$sort`):** Orders by `total_late_minutes` descending, then `emp_code` ascending.

### 3. `department_trend` (`GET /analytics/departments/{department}/trend`)
- **Stage 1 (`$match`):** Filters attendance logs by date range `[from_date, to_date]` using index `(date, emp_code)`.
- **Stage 2 (`$lookup` & `$match`):** Joins `employees` and filters for the requested department.
- **Stage 3 (`$group`):** Aggregates daily `present_count` and `late_count` by date.
- **Stage 4 (`$densify`):** MongoDB native densification: fills in missing calendar days (including weekends and zero-activity days) across the date range bounds `[from_dt, to_dt]`.
- **Stage 5 (`$set`):** Sets `is_working_day` via `$dayOfWeek` in `[2, 3, 4, 5, 6]`.
- **Stage 6 (`$lookup`):** Sub-pipeline counting employees in the department with `joined_on <= date` to establish daily `headcount`.
- **Stage 7 (`$set`):** Computes `attendance_rate` (`present_count / headcount`) for working days with headcount > 0; otherwise `null`.
- **Stage 8 (`$setWindowFields`):** Computes rolling 7-day average `moving_avg_7d` using window `documents: [-6, "current"]` over non-null `attendance_rate`.

---

## 4. Indexing & Concurrency Architecture

### Index Strategy
1. `employees.(emp_code)` [UNIQUE]: Enforces unique employee codes at database level.
2. `attendance_logs.(emp_code, date)` [UNIQUE]: Enforces at most one attendance record per employee per day; prevents double punch-ins.
3. `attendance_logs.(date DESC, emp_code ASC)`: Powers paginated `GET /attendance` list queries without in-memory sorting.
4. `attendance_logs.(emp_code, punch_in DESC)`: Allows rapid lookup of an open punch-in for `POST /attendance/punch-out`.
5. `attendance_logs.(date, late_minutes, emp_code)`: Provides a covered index scan for monthly late-comers.
6. `employees.(joined_on, department)` & `(department, joined_on, emp_code)`: Powers department summary and trend headcount lookups with zero collection scans.

### Concurrency & Race Condition Guarantees
- **Concurrent identical punch-ins:**
  Both requests attempt `insert_one`. MongoDB's unique index raises a `DuplicateKeyError` on the second request. The API catches it and returns HTTP 409 (exactly one 201, rest 409).
- **Concurrent punch-outs:**
  Uses an atomic conditional update: `db.attendance_logs.update_one({"_id": record["_id"], "punch_out": None}, {"$set": ...})`. The first request updates `punch_out`; the second finds `modified_count == 0` and returns HTTP 409 (exactly one 200).
- **Concurrent PATCH regularizations:**
  Uses optimistic locking: updates condition on current history size `"$size": {"$ifNull": ["$history", []]}` while pushing the new history entry. Conflicting updates return 409, preventing lost audit history.

---

## 5. Top 15 Live-Interview Questions & Answers

### Q1: Why did you use Decimal instead of Python's built-in `round()`?
**Answer:** Python's built-in `round()` implements banker's rounding (round half to even). For example, `round(2.5, 0)` is 2, while `round(3.5, 0)` is 4. Rule R4 and R8 require standard half-up rounding (where .5 always rounds away from zero). Furthermore, IEEE-754 floats have representation issues (such as `2.675` actually being `2.67499999999999982236...`), causing `round(2.675, 2)` to give `2.67`. Using `Decimal` with `ROUND_HALF_UP` avoids these floating point artifacts.

### Q2: What happens if an employee on an overnight shift (22:00 to 06:00) punches in at 05:45 IST?
**Answer:** Under Rule R1, because the shift is overnight (`shift_end <= shift_start`) and 05:45 is strictly earlier than `shift_end` (06:00), this punch-in belongs to the shift that started the previous calendar day. If today is 2026-07-07, the record's `date` is recorded as `2026-07-06`.

### Q3: How do you prevent check-then-insert race conditions on punch-in?
**Answer:** Check-then-insert (`find_one` then `insert_one`) is inherently vulnerable to race conditions because two simultaneous requests can both execute `find_one`, find nothing, and then both attempt `insert_one`. Instead, we rely on MongoDB's unique compound index on `(emp_code, date)`. We attempt the insert directly. If a concurrent duplicate arrives, the database engine enforces serialization and raises a `DuplicateKeyError`, which we catch and translate into an HTTP 409 response.

### Q4: How does your leaderboard handle ties at the `limit` boundary?
**Answer:** The leaderboard uses MongoDB's `$rank` window function, which produces standard competition ranking (1, 2, 2, 4). The `$match: {"rank": {"$lte": limit}}` stage is evaluated *after* ranking. For example, if `limit=10` and employees ranked 10 and 11 tie with identical late minutes, both have `rank: 10`, so both satisfy `rank <= 10`. The response will return 11 items.

### Q5: How does department summary account for employees who logged zero attendance records?
**Answer:** The aggregation pipeline initiates from the `employees` collection rather than `attendance_logs`. It matches all employees where `joined_on <= end_of_month`. It groups by department to establish `headcount` before executing a `$lookup` to `attendance_logs`. Employees with zero records have an empty `logs` array, contributing +1 to headcount without generating phantom attendance counts.

### Q6: Why did you use `$densify` in the department trend pipeline?
**Answer:** Rule R10 and the department trend contract require *exactly one row per calendar day* across the requested date range, including weekends and days where no employees worked. `$densify` with range bounds `[from_dt, to_dt]` guarantees that MongoDB generates every calendar day in the database layer rather than pulling sparse data into Python and looping in memory.

### Q7: How does your code ensure internal MongoDB `_id` values are never exposed?
**Answer:** All read operations pass documents through `serialize_employee` or `serialize_attendance`, which explicitly pop `_id` (and any legacy `id` attributes). Projections in aggregation pipelines also explicitly specify `_id: 0`.

### Q8: What is the difference between an unrecorded weekday and an `ABSENT` status?
**Answer:** An unrecorded weekday has no document in `attendance_logs` at all. An `ABSENT` record is an explicit document in `attendance_logs` with `status: "ABSENT"`, `punch_in: null`, `punch_out: null`, `late_minutes: 0`, and `work_hours: null`. Neither counts towards `present_days`, but `ABSENT` represents an audited status entry.

### Q9: How are audit history changes stored in MongoDB vs returned in the API?
**Answer:** In MongoDB, `history[].at`, `changes.punch_in`, and `changes.punch_out` are stored as BSON UTC datetimes. At the API boundary, they are converted to integer epoch milliseconds. Only fields that strictly changed value (`old != new`) are stored in `changes`.

### Q10: How do you ensure all queries use index scans (IXSCAN) on a collection of 100,000+ documents?
**Answer:** Every query and aggregation pipeline begins with a `$match` stage (or find filter) covered by an appropriate index:
- `attendance_list`: indexed by `(date DESC, emp_code ASC)`
- `employee_monthly`: indexed by `(emp_code, date)`
- `late_leaderboard`: indexed by `(date, late_minutes, emp_code)`
- `department_summary`: indexed on `employees` by `(joined_on, department)`
- `department_trend`: indexed on `attendance_logs` by `(date, emp_code)`
The `GET /admin/explain/{endpoint}` route confirms execution stats show `IXSCAN` and 0 `COLLSCAN`.

---

### "Make this small change" Live-Interview Scenarios

#### Scenario 11: "Change grace period from 10 minutes to 15 minutes."
**Where to change:** `compute_late_minutes` in `app/main.py`.
```python
# Before:
if diff_seconds > 600:
    return diff_seconds // 60

# After:
if diff_seconds > 900:  # 15 minutes = 900 seconds
    return diff_seconds // 60
```

#### Scenario 12: "Change half-day threshold from 4.5 hours to 5.0 hours."
**Where to change:** `compute_half_day` in `app/main.py`.
```python
# Before:
def compute_half_day(work_hours: Optional[float]) -> bool:
    if work_hours is None:
        return False
    return work_hours < 4.50

# After:
def compute_half_day(work_hours: Optional[float]) -> bool:
    if work_hours is None:
        return False
    return work_hours < 5.00
```

#### Scenario 13: "Change overtime minimum threshold from 30 minutes to 45 minutes."
**Where to change:** `compute_overtime` in `app/main.py`.
```python
# Before:
return diff_minutes if diff_minutes >= 30 else 0

# After:
return diff_minutes if diff_minutes >= 45 else 0
```

#### Scenario 14: "Change leaderboard ranking from standard competition ranking ($rank) to dense ranking ($denseRank)."
**Where to change:** `build_late_leaderboard_pipeline` in `app/main.py`.
```python
# Before:
"output": {
    "rank": {"$rank": {}}
}

# After:
"output": {
    "rank": {"$denseRank": {}}
}
```

#### Scenario 15: "Support an optional department filter on GET /employees."
**Where to check:** Already implemented in `list_employees`!
```python
@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    q: Dict[str, Any] = {}
    if department:
        q["department"] = department
...
```
Supported by index `[("department", ASCENDING), ("joined_on", ASCENDING), ("emp_code", ASCENDING)]`.
