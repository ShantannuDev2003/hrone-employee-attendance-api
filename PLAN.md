# Engineering Assignment Implementation Plan: Employee Attendance & Analytics API

## 1. Overview & Objectives
Build a production-grade, spec-compliant FastAPI + MongoDB service in `app/main.py`.
- Adheres strictly to `openapi.yaml`, `DATA_MODEL.md`, and `PROBLEM_STATEMENT.md`.
- Handles high-throughput concurrency (idempotent, race-safe, unique index backed).
- All analytics powered by index-backed MongoDB aggregation pipelines (no Python loops).
- Zero 500 errors; precise 404, 409, 422 responses.

---

## 2. Conventions & Ground Rules

### Identification & Mongo Internal IDs
- `emp_code`: Unique customer-generated business key (e.g. `EMP0001`). Identifies employees everywhere.
- `_id`: Strictly internal to MongoDB. Never accepted in requests; never returned in responses.
- Attendance natural key: `(emp_code, date)` uniquely identifies an attendance record.

### Instants & Time Representation
- API instants: Integer epoch **milliseconds** (`punched_at`, `punch_in`, `punch_out`, `created_at`, `history[].at`, and inside `history[].changes`).
- Strict validation: Must reject values looking like seconds (< 100,000,000,000), floats, strings, bools, and out-of-range values with HTTP 422.
- Instant truncation: Instants must be truncated to whole seconds (`ms // 1000 * 1000`) before any calculation; stored and returned as truncated values.
- PyMongo conversion: MongoDB stores BSON UTC datetimes. PyMongo returns naive datetimes by default; we ensure timezone awareness (`UTC`) and proper conversion to/from epoch milliseconds.

### Calendar Dates & Shifts
- Dates: `YYYY-MM-DD` (IST calendar date, UTC+05:30).
- Months: `YYYY-MM`.
- Shift times: `HH:MM` (24-hour, IST).

### Stored Derived Fields
- `work_hours`, `late_minutes`, `overtime_minutes`, `half_day` are computed exclusively by the API and stored.
- Analytics queries MUST read the stored derived values; never recompute on the fly.
- Missing legacy fields in MongoDB: missing `history` -> `[]`, missing `half_day` -> `false`.

---

## 3. Business Rules (R1 - R10) - Exact Wording

- **R1 Time zone and attendance date:**
  Shift times and attendance dates are IST (UTC+05:30). A record's `date` is the IST calendar date of its `punch_in` instant. Exception - overnight shifts (`shift_end` <= `shift_start`, e.g. 22:00-06:00): a punch-in whose IST time is *earlier than* `shift_end` belongs to the shift that started the previous day. The shift end of an overnight shift falls on the next calendar day. Before any calculation, instants are truncated to whole seconds (09:40:00.900 counts as 09:40:00), and the truncated value is what is stored and returned.

- **R2 Late:**
  Grace period is 10 minutes. A punch-in is late only if it is *strictly more than* 10:00 after `shift_start`. When late, `late_minutes` = whole minutes (floor) elapsed since `shift_start` - not since the end of the grace period. Otherwise 0. Example (shift 09:30): 09:40:00 -> 0; 09:40:01 -> 10; 10:15:59 -> 45.

- **R3 Overtime:**
  `overtime_minutes` = whole minutes (floor) between `shift_end` and `punch_out`, counted only when that is >= 30; otherwise 0.

- **R4 Work hours:**
  (`punch_out` - `punch_in`) in seconds / 3600, rounded to 2 decimals **half-up** (not banker's rounding, not float rounding). `null` until punch-out.

- **R5 Half day:**
  After punch-out, `half_day` = true when the **rounded** `work_hours` < 4.50. A half day counts as 0.5 of a present day.

- **R6 Presence:**
  `PRESENT`, `WFH` and `ON_DUTY` count as present. `ABSENT` and `LEAVE` do not.

- **R7 Working days:**
  Mon-Fri, no holiday calendar. For an employee who joined mid-month, working days are counted from `joined_on`. `present_days` only counts records that fall on Mon-Fri (weekend records still contribute to late and overtime totals).

- **R8 Rounding of reported numbers:**
  2 decimals half-up (rates: 4 decimals half-up).

- **R9 Headcount:**
  Employees whose `joined_on` is on or before the end of the period (the last day of the month, or the given day for the trend), whether or not they have any logs.

- **R10 Pagination:**
  `page` starts at 1. `page_size` max is 100 (default 20). `total` always reflects the applied filters.

---

## 4. Endpoints & Status Codes Summary

1. `GET /health`
   - 200: Mongo ping succeeds
   - 503: Mongo ping fails / unreachable
2. `POST /employees`
   - 201: Employee created
   - 409: Duplicate `emp_code` (unique index violation)
   - 422: Validation error (`shift_start == shift_end`, bad regex, etc.)
3. `GET /employees`
   - 200: Paginated employees, sorted by `emp_code` ASC
   - 422: Validation error (page < 1, page_size < 1 or > 100)
4. `POST /attendance/punch-in`
   - 201: Attendance record created
   - 404: Unknown employee
   - 409: Record already exists for (`emp_code`, `date`)
   - 422: Invalid body, invalid timestamp, bad status
5. `POST /attendance/punch-out`
   - 200: Attendance record closed & derived fields computed
   - 404: Unknown employee, or no open punch-in found
   - 409: Record already punched out (atomic update condition)
   - 422: `punched_at <= punch_in` or `punched_at > punch_in + 24 hours`
6. `GET /attendance`
   - 200: Paginated attendance records, sorted by `date` DESC, `emp_code` ASC
   - 422: `date_from > date_to`, page bounds violation
7. `PATCH /attendance/{emp_code}/{date}`
   - 200: Attendance record updated, history appended
   - 404: Unknown employee or record not found for that date
   - 409: Concurrency conflict
   - 422: Setting ABSENT/LEAVE with punch times; presence without punch_in; punch_in changed to different attendance date; punch_out <= punch_in or > 24h; empty changes
8. `GET /analytics/employees/{emp_code}/monthly`
   - 200: Monthly attendance summary
   - 404: Unknown employee
   - 422: Invalid month format
9. `GET /analytics/departments/summary`
   - 200: Aggregated department summary
   - 422: Invalid month format
10. `GET /analytics/leaderboard/late`
    - 200: Late-comers leaderboard with standard competition ranking ($rank / ties)
    - 422: Invalid query params
11. `GET /analytics/departments/{department}/trend`
    - 200: Gap-filled daily attendance trend with 7-day moving average
    - 404: Unknown department (no employees in department)
    - 422: `to < from` or date range > 92 days
12. `GET /admin/explain/{endpoint}`
    - 200: Raw executionStats explain document verifying IXSCAN / no COLLSCAN
    - 422: Missing query parameters or invalid endpoint name

---

## 5. Ambiguities & Decisions Taken

1. **Rounding of `attendance_pct` in `EmployeeMonthly`:**
   - Spec text: "`attendance_pct` = `present_days` / `working_days` x 100, R8 rounding, or `null` when `working_days` is 0."
   - Rule R8: "2 decimals half-up (rates: 4 decimals half-up)."
   - Decision: `attendance_pct` is expressed as a percentage (0 to 100), not a 0..1 rate. Therefore, per R8, reported numbers are rounded to 2 decimals half-up. Rates (`attendance_rate`, `moving_avg_7d` in trend) are 0..1 rates and rounded to 4 decimals half-up. If `working_days == 0`, `attendance_pct` is `null`.

2. **Overnight Shifts - Punch-in and Shift Bounds:**
   - Shift: `shift_start > shift_end` (e.g. 22:00 to 06:00).
   - If punch-in IST time < `shift_end` (e.g. 05:30 on 2026-07-07):
     - The attendance date is `2026-07-06` (previous day).
     - The shift started on `2026-07-06 22:00 IST` and ended on `2026-07-07 06:00 IST`.
     - Late minutes: punch-in (05:30) vs shift start (previous day 22:00) -> > 10 min, late.
   - If punch-in IST time >= `shift_start` (e.g. 21:55 on 2026-07-06):
     - Attendance date is `2026-07-06`. Shift end is `2026-07-07 06:00 IST`. Overtime is measured against `2026-07-07 06:00 IST`.

3. **`punch-out` Matching Logic:**
   - Finds the employee's most recent record whose `punch_in <= punched_at` (sorting by `punch_in` descending).
   - If no record exists with `punch_in <= punched_at`: return 404.
   - If the matching record already has `punch_out is not None`: return 409.
   - If `punched_at <= punch_in` or `punched_at - punch_in > 24 hours`: return 422.
   - Concurrency: Atomically update with `db.attendance_logs.update_one({"_id": doc["_id"], "punch_out": None}, {"$set": ...})`. If `modified_count == 0`, return 409.

4. **`PATCH` / Regularization Validation & Changes:**
   - Setting `ABSENT` or `LEAVE`: clears `punch_in`, `punch_out`, `work_hours`, `late_minutes`=0, `overtime_minutes`=0, `half_day`=False.
   - If client sends `punch_in` or `punch_out` along with `status in ("ABSENT", "LEAVE")`: reject with 422.
   - Setting a presence status (`PRESENT`, `WFH`, `ON_DUTY`): requires `punch_in` (either in payload or existing).
   - `punch_in` modification: MUST preserve the record's attendance date under R1. If modified `punch_in` belongs to a different date, reject with 422.
   - Changes object in history: Record only fields whose values strictly changed (`before != after`). If no fields changed, return 422.
   - Concurrency: Atomically update with conditional match on version or state to prevent lost updates, pushing history in the same update.

5. **Headcount in Department Summary & Trend:**
   - Department summary includes employees with `joined_on <= last_day_of_month`.
   - Employees who have 0 attendance records must still contribute 1 to `headcount`.
   - Trend includes employees with `joined_on <= day`.
   - If a department has 0 employees active for the month, it is omitted from the department summary.

6. **Leaderboard Ranking:**
   - Standard competition ranking: ties share rank (1, 2, 2, 4).
   - Implemented using MongoDB `$setWindowFields` with `rank` window function.
   - Filtering `rank <= limit` happens AFTER ranking.

7. **Department Trend Gap Filling:**
   - Handled in MongoDB using `$densify` on the date field across the requested `[from, to]` range, followed by `$setWindowFields` for rolling 7-day window average.

---

## 6. Build Order

1. **Core Utilities & Time/Math Helpers:**
   - Strict EpochMillis Pydantic validation (rejecting floats, strings, bools, seconds < 10^11).
   - Decimal half-up rounding (2dp for work_hours/numbers, 4dp for rates).
   - R1-R5 calculations handling IST, overnight shifts, floor minutes, truncation.
2. **Database Lifecycle & Indexes:**
   - Lifespan handler creating unique index on `employees.emp_code`, unique index on `attendance_logs.(emp_code, date)`, and query indexes.
   - `GET /health` with `db.command("ping")`.
3. **Employee Endpoints:**
   - `POST /employees` (unique index conflict handling).
   - `GET /employees` (pagination bounds, department filter, sorting).
4. **Attendance Endpoints:**
   - `POST /attendance/punch-in` (race-safe, 404/409/422).
   - `POST /attendance/punch-out` (race-safe atomic update, 404/409/422).
   - `GET /attendance` (sorting, pagination, date filter validation).
   - `PATCH /attendance/{emp_code}/{date}` (regularization, history recording, derived recalculation).
5. **Analytics Pipelines (Reusable Builders):**
   - Helper 1: Employee monthly summary pipeline.
   - Helper 2: Department summary cross-collection pipeline.
   - Helper 3: Late leaderboard window pipeline.
   - Helper 4: Department trend densify & moving average pipeline.
6. **Explain Endpoint:**
   - `GET /admin/explain/{endpoint}` running executionStats on each builder.
7. **Verification & Testing:**
   - `local_tools/gen_data.py`: 100k records dataset generator with all edge cases.
   - `local_tools/test_api.py`: Automated pytest suite covering R1-R10, race conditions, pagination, rounding.
8. **Documentation & Review:**
   - Fill `REVIEW.md` detailing 10+ defects and resolutions.
   - Fill `DECISIONS.md` addressing the 5 template questions.
   - Fill `README.md`.
   - Write comprehensive `WALKTHROUGH.md`.
