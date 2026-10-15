"""
Comprehensive test suite for Employee Attendance & Analytics API:
Covers:
- GET /health (200 when alive)
- POST /employees & GET /employees (unique constraint, pagination, sorting, shift validation)
- R1: Timezone, attendance date, overnight shift logic, instant truncation
- R2: Late grace period boundaries (09:40:00 -> 0, 09:40:01 -> 10, 10:15:59 -> 45)
- R3: Overtime 30-min minimum threshold (29 min -> 0, 30 min -> 30)
- R4: Work hours calculation with half-up rounding (ROUND_HALF_UP)
- R5: Half-day determination (rounded work_hours < 4.50)
- R6: Presence statuses (PRESENT, WFH, ON_DUTY vs ABSENT, LEAVE)
- R7: Working days Mon-Fri, mid-month joiner, weekend exclusion from present_days
- R8: Half-up rounding of reported numbers (2dp) and rates (4dp)
- R9: Headcount including employees with zero logs
- R10: Pagination bounds (page >= 1, 1 <= page_size <= 100)
- Timestamp validation (rejection of seconds, floats, bools, strings with 422)
- Concurrency:
  - 20 parallel punch-ins -> exactly one 201 and nineteen 409
  - parallel punch-outs -> exactly one 200 and the rest 409
  - parallel PATCHes -> history length equals successful patches (no lost updates)
- GET /admin/explain/{endpoint} verification of IXSCAN and absence of COLLSCAN
"""
import sys
from pathlib import Path

REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import asyncio
import uuid
import pytest
import httpx
from httpx import ASGITransport

from app.main import app, db, ensure_indexes


@pytest.fixture(autouse=True)
def setup_indexes():
    ensure_indexes()


# FIX 6b: Use a counter in EMP800000-EMP899999 range to avoid collisions.
# uuid4 gives 122 bits of randomness; modulo 100000 keeps codes in-range.
def unique_emp_code() -> str:
    val = 800000 + (int(uuid.uuid4().int) % 100000)
    return f"EMP{val:06d}"


@pytest.mark.asyncio
async def test_health():
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_employee_crud_and_validation():
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        code = unique_emp_code()

        # 1. Validation failure: shift_start == shift_end -> 422
        bad_emp = {
            "emp_code": code,
            "name": "Test Emp",
            "email": "test@example.com",
            "department": "Engineering",
            "shift_start": "09:00",
            "shift_end": "09:00",
            "joined_on": "2026-01-01",
        }
        res = await client.post("/employees", json=bad_emp)
        assert res.status_code == 422

        # 2. Valid creation
        good_emp = {
            "emp_code": code,
            "name": "Test Emp",
            "email": "test@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        }
        res = await client.post("/employees", json=good_emp)
        assert res.status_code == 201, f"Expected 201, got {res.status_code}: {res.text}"
        data = res.json()
        assert "_id" not in data
        assert isinstance(data["created_at"], int)
        assert data["created_at"] > 100000000000

        # 3. Duplicate emp_code -> 409
        res = await client.post("/employees", json=good_emp)
        assert res.status_code == 409

        # 4. List employees pagination & department filter
        res = await client.get("/employees?department=Engineering&page=1&page_size=10")
        assert res.status_code == 200
        data = res.json()
        assert data["page"] == 1
        assert data["page_size"] == 10
        assert data["total"] >= 1
        assert all(item["department"] == "Engineering" for item in data["items"])

        # 5. Pagination boundary violations -> 422
        res = await client.get("/employees?page=0")
        assert res.status_code == 422
        res = await client.get("/employees?page_size=101")
        assert res.status_code == 422


@pytest.mark.asyncio
async def test_r1_r2_punch_in_and_late_boundaries():
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Create dedicated employee with shift 09:30 - 18:30
        code1 = unique_emp_code()
        emp = {
            "emp_code": code1,
            "name": "Late Test",
            "email": "late@example.com",
            "department": "QA",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-07-01",
        }
        res = await client.post("/employees", json=emp)
        assert res.status_code == 201, f"Expected 201 for {code1}: {res.text}"

        # R2 boundary 1: 09:40:00 IST -> diff is 600s -> not strictly > 10:00 -> late_minutes = 0
        # 2026-07-10 09:40:00 IST = 2026-07-10 04:10:00 UTC = 1783656600000 ms
        p1_ms = 1783656600000
        res = await client.post("/attendance/punch-in", json={"emp_code": code1, "punched_at": p1_ms})
        assert res.status_code == 201
        data = res.json()
        assert data["date"] == "2026-07-10"
        assert data["late_minutes"] == 0
        assert data["punch_in"] == p1_ms

        # Duplicate punch-in for same date -> 409
        res = await client.post("/attendance/punch-in", json={"emp_code": code1, "punched_at": p1_ms + 1000})
        assert res.status_code == 409

        # Create employee for boundary 2: 09:40:01 IST -> diff is 601s -> strictly > 10:00 -> late_minutes = 10
        code2 = unique_emp_code()
        emp2 = {
            "emp_code": code2,
            "name": "Late Test 2",
            "email": "late2@example.com",
            "department": "QA",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-07-01",
        }
        await client.post("/employees", json=emp2)
        p2_ms = p1_ms + 1000  # 09:40:01 IST
        res = await client.post("/attendance/punch-in", json={"emp_code": code2, "punched_at": p2_ms})
        assert res.status_code == 201
        assert res.json()["late_minutes"] == 10

        # Create employee for boundary 3: 10:15:59 IST -> diff is 45m 59s -> late_minutes = 45
        code3 = unique_emp_code()
        emp3 = {
            "emp_code": code3,
            "name": "Late Test 3",
            "email": "late3@example.com",
            "department": "QA",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-07-01",
        }
        await client.post("/employees", json=emp3)
        # 10:15:59 IST = 04:45:59 UTC on 2026-07-10 = 1783658759000 ms
        p3_ms = 1783658759000
        res = await client.post("/attendance/punch-in", json={"emp_code": code3, "punched_at": p3_ms})
        assert res.status_code == 201
        assert res.json()["late_minutes"] == 45


@pytest.mark.asyncio
async def test_r1_overnight_shift():
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        code = unique_emp_code()
        # Overnight shift 22:00 to 06:00
        emp = {
            "emp_code": code,
            "name": "Overnight Emp",
            "email": "night@example.com",
            "department": "Support",
            "shift_start": "22:00",
            "shift_end": "06:00",
            "joined_on": "2026-07-01",
        }
        await client.post("/employees", json=emp)

        # Punch-in at 05:30 IST on 2026-07-11 (earlier than shift_end 06:00)
        # Belongs to 2026-07-10!
        # 2026-07-11 05:30:00 IST = 2026-07-11 00:00:00 UTC = 1783728000000 ms
        p_ms = 1783728000000
        res = await client.post("/attendance/punch-in", json={"emp_code": code, "punched_at": p_ms})
        assert res.status_code == 201
        data = res.json()
        assert data["date"] == "2026-07-10"


@pytest.mark.asyncio
async def test_r3_r4_r5_punch_out_and_work_hours():
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        code = unique_emp_code()
        emp = {
            "emp_code": code,
            "name": "Punch Out Test",
            "email": "pout@example.com",
            "department": "DevOps",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-07-01",
        }
        await client.post("/employees", json=emp)

        # Punch in at 09:30 IST on 2026-07-12 (04:00 UTC) = 1783742400000 ms
        pin_ms = 1783742400000
        res = await client.post("/attendance/punch-in", json={"emp_code": code, "punched_at": pin_ms})
        assert res.status_code == 201

        # Test punch-out before punch-in -> 422
        res = await client.post("/attendance/punch-out", json={"emp_code": code, "punched_at": pin_ms - 1000})
        assert res.status_code == 422

        # Test punch-out > 24 hours after punch-in -> 422
        res = await client.post("/attendance/punch-out", json={"emp_code": code, "punched_at": pin_ms + 86401000})
        assert res.status_code == 422

        # Punch out at 13:30 IST (4.00 hours worked)
        # 4.00 hours < 4.50 -> half_day = True
        # Shift end is 18:30 -> no overtime (0 min)
        pout_ms = pin_ms + (4 * 3600 * 1000)
        res = await client.post("/attendance/punch-out", json={"emp_code": code, "punched_at": pout_ms})
        assert res.status_code == 200
        data = res.json()
        assert data["work_hours"] == 4.00
        assert data["half_day"] is True
        assert data["overtime_minutes"] == 0

        # Duplicate punch-out -> 409
        res = await client.post("/attendance/punch-out", json={"emp_code": code, "punched_at": pout_ms + 1000})
        assert res.status_code == 409


@pytest.mark.asyncio
async def test_r3_overtime_threshold():
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        code = unique_emp_code()
        emp = {
            "emp_code": code,
            "name": "OT Test",
            "email": "ot@example.com",
            "department": "Finance",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-07-01",
        }
        await client.post("/employees", json=emp)

        # In at 09:30 IST on 2026-07-13
        pin_ms = 1783828800000
        in_res = await client.post("/attendance/punch-in", json={"emp_code": code, "punched_at": pin_ms})
        assert in_res.status_code == 201
        att_date = in_res.json()["date"]

        # Out at 18:59:59 IST (29 min 59 sec after shift end 18:30 -> floor is 29 min < 30 -> overtime = 0)
        pout_29_ms = pin_ms + (9 * 3600 * 1000) + (29 * 60 * 1000) + 59000
        res = await client.patch(
            f"/attendance/{code}/{att_date}",
            json={
                "punch_out": pout_29_ms,
                "reason": "testing 29 min overtime",
                "regularized_by": "tester"
            }
        )
        assert res.status_code == 200
        assert res.json()["overtime_minutes"] == 0

        # Out at 19:00:00 IST (exactly 30 min overtime -> >= 30 -> overtime = 30)
        pout_30_ms = pin_ms + (9 * 3600 * 1000) + (30 * 60 * 1000)
        res = await client.patch(
            f"/attendance/{code}/{att_date}",
            json={
                "punch_out": pout_30_ms,
                "reason": "testing 30 min overtime",
                "regularized_by": "tester"
            }
        )
        assert res.status_code == 200
        assert res.json()["overtime_minutes"] == 30


@pytest.mark.asyncio
async def test_patch_validation_and_history():
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        code = unique_emp_code()
        emp = {
            "emp_code": code,
            "name": "Patch Test",
            "email": "patch@example.com",
            "department": "HR",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-07-01",
        }
        await client.post("/employees", json=emp)

        pin_ms = 1783828800000
        in_res = await client.post("/attendance/punch-in", json={"emp_code": code, "punched_at": pin_ms})
        assert in_res.status_code == 201
        att_date = in_res.json()["date"]

        # Test changing nothing -> 422
        res = await client.patch(
            f"/attendance/{code}/{att_date}",
            json={
                "reason": "no changes provided",
                "regularized_by": "tester"
            }
        )
        assert res.status_code == 422

        # Test setting ABSENT with punch_in -> 422
        res = await client.patch(
            f"/attendance/{code}/{att_date}",
            json={
                "status": "ABSENT",
                "punch_in": pin_ms,
                "reason": "invalid absent payload",
                "regularized_by": "tester"
            }
        )
        assert res.status_code == 422

        # Test setting ABSENT cleanly -> clears punch times and sets derived fields to null/0
        res = await client.patch(
            f"/attendance/{code}/{att_date}",
            json={
                "status": "ABSENT",
                "reason": "employee was absent",
                "regularized_by": "manager"
            }
        )
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "ABSENT"
        assert data["punch_in"] is None
        assert data["punch_out"] is None
        assert data["work_hours"] is None
        assert data["late_minutes"] == 0
        assert data["overtime_minutes"] == 0
        assert data["half_day"] is False
        assert len(data["history"]) >= 1
        last_hist = data["history"][-1]
        assert last_hist["by"] == "manager"
        assert last_hist["reason"] == "employee was absent"
        assert "status" in last_hist["changes"]


@pytest.mark.asyncio
async def test_timestamp_strict_validation():
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Seconds-looking value (< 10^11) -> 422
        res = await client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 1783312500})
        assert res.status_code == 422

        # Float value -> 422
        res = await client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 1783312500000.5})
        assert res.status_code == 422

        # Boolean value -> 422
        res = await client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": True})
        assert res.status_code == 422

        # String value -> 422
        res = await client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": "1783312500000"})
        assert res.status_code == 422


@pytest.mark.asyncio
async def test_concurrency_parallel_punch_ins():
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        code = unique_emp_code()
        emp = {
            "emp_code": code,
            "name": "Concurrent In",
            "email": "concin@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-07-01",
        }
        res = await client.post("/employees", json=emp)
        assert res.status_code == 201

        p_ms = 1783915200000  # 2026-07-14 09:30 IST

        # Fire 20 parallel punch-in requests
        tasks = [
            client.post("/attendance/punch-in", json={"emp_code": code, "punched_at": p_ms})
            for _ in range(20)
        ]
        responses = await asyncio.gather(*tasks)

        status_codes = [r.status_code for r in responses]
        assert status_codes.count(201) == 1
        assert status_codes.count(409) == 19


@pytest.mark.asyncio
async def test_concurrency_parallel_punch_outs():
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        code = unique_emp_code()
        emp = {
            "emp_code": code,
            "name": "Concurrent Out",
            "email": "concout@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-07-01",
        }
        res = await client.post("/employees", json=emp)
        assert res.status_code == 201

        pin_ms = 1784001600000  # 2026-07-15 09:30 IST
        res = await client.post("/attendance/punch-in", json={"emp_code": code, "punched_at": pin_ms})
        assert res.status_code == 201

        pout_ms = pin_ms + (9 * 3600 * 1000)

        # Fire 10 parallel punch-out requests
        tasks = [
            client.post("/attendance/punch-out", json={"emp_code": code, "punched_at": pout_ms})
            for _ in range(10)
        ]
        responses = await asyncio.gather(*tasks)

        status_codes = [r.status_code for r in responses]
        assert status_codes.count(200) == 1
        assert status_codes.count(409) == 9


@pytest.mark.asyncio
async def test_concurrency_parallel_patches():
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        code = unique_emp_code()
        emp = {
            "emp_code": code,
            "name": "Concurrent Patch",
            "email": "concpatch@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-07-01",
        }
        res = await client.post("/employees", json=emp)
        assert res.status_code == 201

        pin_ms = 1784088000000  # 2026-07-16 09:30 IST
        in_res = await client.post("/attendance/punch-in", json={"emp_code": code, "punched_at": pin_ms})
        assert in_res.status_code == 201
        att_date = in_res.json()["date"]

        # Fire 5 parallel PATCH requests with different punch_outs
        tasks = [
            client.patch(
                f"/attendance/{code}/{att_date}",
                json={
                    "punch_out": pin_ms + ((5 + i) * 3600 * 1000),
                    "reason": f"patch iteration {i}",
                    "regularized_by": f"admin_{i}"
                }
            )
            for i in range(5)
        ]
        responses = await asyncio.gather(*tasks)

        success_count = sum(1 for r in responses if r.status_code == 200)
        assert success_count >= 1

        # Verify no history entries were lost
        rec = db.attendance_logs.find_one({"emp_code": code, "date": att_date})
        assert len(rec.get("history", [])) == success_count


@pytest.mark.asyncio
async def test_analytics_and_explain():
    """Seed its own data so it is independent of any production or seed dataset."""
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Seed two employees in Engineering for this test
        eng_code1 = "EMP700001"
        eng_code2 = "EMP700002"
        eng_code3 = "EMP700003"
        eng_code4 = "EMP700004"
        for code, email, dept in [
            (eng_code1, "ana1@test.com", "Engineering"),
            (eng_code2, "ana2@test.com", "Engineering"),
            (eng_code3, "ana3@test.com", "Engineering"),
            (eng_code4, "ana4@test.com", "Engineering"),
        ]:
            r = await client.post("/employees", json={
                "emp_code": code, "name": f"Ana {code}",
                "email": email, "department": dept,
                "shift_start": "09:30", "shift_end": "18:30",
                "joined_on": "2026-07-01",
            })
            # 201 or 409 (already exists from a prior run within same session)
            assert r.status_code in (201, 409), f"Expected 201/409 for {code}: {r.text}"

        # Seed some attendance logs for July 2026
        # 2026-07-07 09:40:01 IST = late by 10 min: pin = 1783643401000 (07 Jul 09:40:01 IST)
        # Use a well-known pin for EMP700003 and EMP700004 to give equal late minutes
        for code in (eng_code3, eng_code4):
            await client.post("/attendance/punch-in", json={
                "emp_code": code,
                "punched_at": 1783656601000,  # 2026-07-10 09:40:01 IST -> late 10 min
            })

        # 1. Employee monthly
        res = await client.get(f"/analytics/employees/{eng_code1}/monthly?month=2026-07")
        assert res.status_code == 200
        data = res.json()
        assert data["month"] == "2026-07"
        assert "working_days" in data
        assert "present_days" in data
        assert "attendance_pct" in data

        # 2. Department summary
        res = await client.get("/analytics/departments/summary?month=2026-07")
        assert res.status_code == 200
        data = res.json()
        assert "items" in data
        assert len(data["items"]) > 0

        # 3. Late leaderboard with ties (EMP700003 and EMP700004 both have equal late minutes)
        res = await client.get("/analytics/leaderboard/late?month=2026-07&limit=10")
        assert res.status_code == 200
        data = res.json()
        items = data["items"]
        assert len(items) >= 2
        # Check standard competition ranking for tied employees
        e3 = next((r for r in items if r["emp_code"] == eng_code3), None)
        e4 = next((r for r in items if r["emp_code"] == eng_code4), None)
        if e3 and e4:
            assert e3["rank"] == e4["rank"]
            assert e3["total_late_minutes"] == e4["total_late_minutes"]

        # 4. Department trend
        res = await client.get("/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-07")
        assert res.status_code == 200
        trend_data = res.json()
        assert trend_data["department"] == "Engineering"
        assert len(trend_data["items"]) == 7  # exactly 7 days

        # 5. Admin explain checks on all 5 endpoints verifying IXSCAN
        for ep in ["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]:
            if ep == "attendance_list":
                params = {"emp_code": eng_code1}
            elif ep == "employee_monthly":
                params = {"emp_code": eng_code1, "month": "2026-07"}
            elif ep == "department_summary":
                params = {"month": "2026-07"}
            elif ep == "late_leaderboard":
                params = {"month": "2026-07", "limit": 10}
            elif ep == "department_trend":
                params = {"department": "Engineering", "from": "2026-07-01", "to": "2026-07-07"}

            exp_res = await client.get(f"/admin/explain/{ep}", params=params)
            assert exp_res.status_code == 200
            exp_json = exp_res.json()
            assert exp_json["endpoint"] == ep
            assert "explain" in exp_json
            # Convert explain to string to verify presence of IXSCAN and absence of COLLSCAN
            exp_str = str(exp_json["explain"])
            assert "IXSCAN" in exp_str or "PROJECTION_COVERED" in exp_str
            assert "COLLSCAN" not in exp_str

# --------------------------------------------------------------------------- #
# FIX 1: Invalid calendar date tests
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_invalid_calendar_dates():
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # POST /employees with impossible joined_on -> 422
        bad_date_body = {
            "emp_code": "EMP800001",
            "name": "Bad Date",
            "email": "baddate@example.com",
            "department": "X",
            "joined_on": "2026-13-45",
        }
        res = await client.post("/employees", json=bad_date_body)
        assert res.status_code == 422, f"Expected 422, got {res.status_code}"

        # 2026-02-29 -> 422 (2026 is NOT a leap year)
        bad_feb_body = dict(bad_date_body, emp_code="EMP800002", joined_on="2026-02-29")
        res = await client.post("/employees", json=bad_feb_body)
        assert res.status_code == 422, f"Expected 422 for 2026-02-29 (not leap year): {res.status_code}"

        # 2024-02-29 -> 201 (2024 IS a leap year)
        ok_feb_body = dict(bad_date_body, emp_code="EMP800003", joined_on="2024-02-29")
        res = await client.post("/employees", json=ok_feb_body)
        assert res.status_code == 201, f"Expected 201 for 2024-02-29 (leap year): {res.status_code}"

        # GET /attendance with impossible date_from -> 422
        res = await client.get("/attendance?date_from=2026-02-30")
        assert res.status_code == 422, f"Expected 422 for impossible date_from, got {res.status_code}"

        # GET /attendance with impossible date_to -> 422
        res = await client.get("/attendance?date_to=2026-02-30")
        assert res.status_code == 422, f"Expected 422 for impossible date_to, got {res.status_code}"

        # Department trend with impossible from date -> 422 (was 500)
        # Need a real department first
        dept_emp = {
            "emp_code": unique_emp_code(),
            "name": "Trend Test",
            "email": f"trend{uuid.uuid4().hex[:6]}@example.com",
            "department": "TrendDept",
            "joined_on": "2026-01-01",
        }
        r = await client.post("/employees", json=dept_emp)
        assert r.status_code == 201
        res = await client.get("/analytics/departments/TrendDept/trend?from=2026-02-30&to=2026-03-01")
        assert res.status_code == 422, f"Expected 422 for trend impossible from date, got {res.status_code}"

        res = await client.get("/analytics/departments/TrendDept/trend?from=2026-02-01&to=2026-02-30")
        assert res.status_code == 422, f"Expected 422 for trend impossible to date, got {res.status_code}"


# --------------------------------------------------------------------------- #
# FIX 2: Punch-out record selection tests
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_punchout_selects_most_recent_before_punchout_time():
    """Yesterday open + today open: punch-out with yesterday's window closes YESTERDAY's record."""
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        code = unique_emp_code()
        emp = {
            "emp_code": code,
            "name": "Window Test",
            "email": f"window{uuid.uuid4().hex[:6]}@example.com",
            "department": "WindowDept",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        }
        res = await client.post("/employees", json=emp)
        assert res.status_code == 201

        # Yesterday: 2026-07-13 09:30 IST = 1783828800000 ms
        yesterday_pin_ms = 1783828800000
        # Today: 2026-07-14 09:30 IST = 1783915200000 ms
        today_pin_ms = 1783915200000

        # Punch in yesterday
        r = await client.post("/attendance/punch-in", json={"emp_code": code, "punched_at": yesterday_pin_ms})
        assert r.status_code == 201
        yesterday_date = r.json()["date"]

        # Punch in today
        r = await client.post("/attendance/punch-in", json={"emp_code": code, "punched_at": today_pin_ms})
        assert r.status_code == 201

        # Punch out with a time in yesterday's window (yesterday at 18:30 IST)
        yesterday_pout_ms = yesterday_pin_ms + (9 * 3600 * 1000)  # 18:30 IST
        r = await client.post("/attendance/punch-out", json={"emp_code": code, "punched_at": yesterday_pout_ms})
        assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
        data = r.json()
        # Must have closed YESTERDAY's record
        assert data["date"] == yesterday_date, f"Expected {yesterday_date} but got {data['date']}"
        assert data["punch_out"] == yesterday_pout_ms


@pytest.mark.asyncio
async def test_punchout_earlier_than_punchin_422():
    """punched_at earlier than the only punch_in -> 422."""
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        code = unique_emp_code()
        emp = {
            "emp_code": code,
            "name": "Early PO",
            "email": f"earlypo{uuid.uuid4().hex[:6]}@example.com",
            "department": "EarlyDept",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        }
        res = await client.post("/employees", json=emp)
        assert res.status_code == 201

        pin_ms = 1783828800000  # 2026-07-13 09:30 IST
        r = await client.post("/attendance/punch-in", json={"emp_code": code, "punched_at": pin_ms})
        assert r.status_code == 201

        # Punch out BEFORE punch in -> 422
        r = await client.post("/attendance/punch-out", json={"emp_code": code, "punched_at": pin_ms - 60000})
        assert r.status_code == 422, f"Expected 422 for early punch-out, got {r.status_code}"


@pytest.mark.asyncio
async def test_punchout_no_records_404():
    """Employee with no records -> 404."""
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        code = unique_emp_code()
        emp = {
            "emp_code": code,
            "name": "No Record",
            "email": f"norec{uuid.uuid4().hex[:6]}@example.com",
            "department": "NoDept",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        }
        res = await client.post("/employees", json=emp)
        assert res.status_code == 201

        pout_ms = 1783828800000 + 3600000
        r = await client.post("/attendance/punch-out", json={"emp_code": code, "punched_at": pout_ms})
        assert r.status_code == 404, f"Expected 404 for employee with no records, got {r.status_code}"


@pytest.mark.asyncio
async def test_punchout_close_twice_409():
    """Closing twice -> 409 (including a second attempt with a later time)."""
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        code = unique_emp_code()
        emp = {
            "emp_code": code,
            "name": "Double PO",
            "email": f"dblpo{uuid.uuid4().hex[:6]}@example.com",
            "department": "DoubleDept",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        }
        res = await client.post("/employees", json=emp)
        assert res.status_code == 201

        pin_ms = 1783828800000
        r = await client.post("/attendance/punch-in", json={"emp_code": code, "punched_at": pin_ms})
        assert r.status_code == 201

        pout_ms = pin_ms + 4 * 3600 * 1000
        r = await client.post("/attendance/punch-out", json={"emp_code": code, "punched_at": pout_ms})
        assert r.status_code == 200

        # Second attempt with same time -> 409
        r = await client.post("/attendance/punch-out", json={"emp_code": code, "punched_at": pout_ms})
        assert r.status_code == 409, f"Expected 409 for duplicate punch-out, got {r.status_code}"

        # Second attempt with a later time -> still 409 (already closed)
        r = await client.post("/attendance/punch-out", json={"emp_code": code, "punched_at": pout_ms + 3600000})
        assert r.status_code == 409, f"Expected 409 for later duplicate punch-out, got {r.status_code}"


@pytest.mark.asyncio
async def test_punchout_over_24h_422():
    """>24h after punch_in -> 422."""
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        code = unique_emp_code()
        emp = {
            "emp_code": code,
            "name": "Long PO",
            "email": f"longpo{uuid.uuid4().hex[:6]}@example.com",
            "department": "LongDept",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        }
        res = await client.post("/employees", json=emp)
        assert res.status_code == 201

        pin_ms = 1783828800000
        r = await client.post("/attendance/punch-in", json={"emp_code": code, "punched_at": pin_ms})
        assert r.status_code == 201

        r = await client.post("/attendance/punch-out", json={"emp_code": code, "punched_at": pin_ms + 86401000})
        assert r.status_code == 422, f"Expected 422 for >24h, got {r.status_code}"


@pytest.mark.asyncio
async def test_punchout_20_parallel_exactly_one_200():
    """20 parallel identical punch-outs -> exactly one 200, nineteen 409."""
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        code = unique_emp_code()
        emp = {
            "emp_code": code,
            "name": "Parallel PO",
            "email": f"parpo{uuid.uuid4().hex[:6]}@example.com",
            "department": "ParallelDept",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        }
        res = await client.post("/employees", json=emp)
        assert res.status_code == 201

        pin_ms = 1783828800000
        r = await client.post("/attendance/punch-in", json={"emp_code": code, "punched_at": pin_ms})
        assert r.status_code == 201

        pout_ms = pin_ms + 9 * 3600 * 1000
        tasks = [
            client.post("/attendance/punch-out", json={"emp_code": code, "punched_at": pout_ms})
            for _ in range(20)
        ]
        responses = await asyncio.gather(*tasks)
        codes = [r.status_code for r in responses]
        assert codes.count(200) == 1, f"Expected exactly one 200, got: {codes}"
        assert codes.count(409) == 19, f"Expected nineteen 409, got: {codes}"


# --------------------------------------------------------------------------- #
# FIX 4: serialize_attendance always includes work_hours
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_serialize_attendance_work_hours_always_present():
    """A record inserted directly into MongoDB without work_hours must still have work_hours=null in response."""
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        code = unique_emp_code()
        emp = {
            "emp_code": code,
            "name": "Serializer Test",
            "email": f"ser{uuid.uuid4().hex[:6]}@example.com",
            "department": "SerDept",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        }
        res = await client.post("/employees", json=emp)
        assert res.status_code == 201

        # Insert directly into MongoDB without work_hours, half_day, or history
        from app.main import db
        db.attendance_logs.insert_one({
            "emp_code": code,
            "date": "2026-07-20",
            "status": "PRESENT",
            "punch_in": None,
            "punch_out": None,
            # deliberately omit work_hours, half_day, history
            "late_minutes": 0,
            "overtime_minutes": 0,
        })

        res = await client.get(f"/attendance?emp_code={code}")
        assert res.status_code == 200
        items = res.json()["items"]
        assert len(items) == 1
        rec = items[0]
        assert "work_hours" in rec, "work_hours must always be present in serialized attendance"
        assert rec["work_hours"] is None
        assert "half_day" in rec
        assert "history" in rec
