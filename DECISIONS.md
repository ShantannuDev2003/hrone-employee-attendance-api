# DECISIONS.md

1. **Indexes.** I created a unique index on `employees.emp_code` and a unique compound index on `attendance_logs.(emp_code, date)` to enforce business uniqueness and eliminate write races. For queries, I created `(date, emp_code)` for sorted pagination and trend matching, `(emp_code, punch_in)` for punch-out resolution, `(date, late_minutes, emp_code)` for covered leaderboard scans, and `(department, joined_on, emp_code)` on `employees`. I considered indexing `status` but rejected it because its low cardinality offers minimal selectivity compared to date-scoped indexes.

2. **Punch-in race.** Both concurrent requests calculate the attendance date and attempt `db.attendance_logs.insert_one()`. The database engine's unique index on `(emp_code, date)` serializes the insert: exactly one document succeeds and returns HTTP 201. The concurrent request raises a PyMongo `DuplicateKeyError`, which my handler catches to return HTTP 409.

3. **Ties.** My aggregation pipeline computes rank using MongoDB's `$rank` window function, producing competition ranks (e.g., 1, 2, 2, 4). The `$match: {"rank": {"$lte": limit}}` stage runs after ranking. Tied employees at the limit boundary both satisfy the predicate and are returned, ordered by `total_late_minutes` descending and `emp_code` ascending.

4. **Headcount.** The department summary pipeline queries the `employees` collection first, matching `joined_on <= end_of_month`. I aggregate headcount from joined employees and perform a `$lookup` to `attendance_logs`. Employees with zero attendance logs retain empty arrays, ensuring they are fully counted in headcount.

5. **One thing you would change.** At 100x scale (~10M attendance documents), I would maintain daily and monthly summary rollups incrementally using MongoDB Change Streams instead of aggregating raw punch documents on every request.
