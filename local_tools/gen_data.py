"""
Seed ~100,000 attendance records plus all edge and awkward cases:
- Employees with no logs at all
- Ties on late minutes (for leaderboard rank validation)
- Weekend attendance records (Mon-Fri vs weekend counting)
- Mid-month joiners
- Overnight shift employees
- Missing weekdays (no record at all)
- Legacy documents missing history and half_day
"""
import calendar
from datetime import date, datetime, timedelta, timezone
import os
import random
from pymongo import MongoClient
from dotenv import load_dotenv

load_dotenv(override=False)

MONGO_URI = os.environ.get("MONGO_URI") or "mongodb://localhost:27017"
MONGO_DB = os.environ.get("MONGO_DB") or "attendance_db"

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


def seed():
    print(f"Connecting to {MONGO_URI}, db={MONGO_DB}...")
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})

    print("Seeding employees...")
    # Awkward case employees:
    employees = [
        # Normal employee with full logs
        {
            "emp_code": "EMP0001",
            "name": "Asha Rao",
            "email": "asha@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-05",
            "created_at": datetime(2026, 1, 5, 4, 30, tzinfo=timezone.utc),
        },
        # Mid-month joiner
        {
            "emp_code": "EMP0002",
            "name": "Bikram Roy",
            "email": "bikram@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-07-15",
            "created_at": datetime(2026, 7, 15, 4, 30, tzinfo=timezone.utc),
        },
        # Late tie employee 1
        {
            "emp_code": "EMP0003",
            "name": "Chitra Sen",
            "email": "chitra@example.com",
            "department": "Sales",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-02-01",
            "created_at": datetime(2026, 2, 1, 4, 30, tzinfo=timezone.utc),
        },
        # Late tie employee 2 (ties with EMP0003)
        {
            "emp_code": "EMP0004",
            "name": "Deepak Shah",
            "email": "deepak@example.com",
            "department": "Sales",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-02-01",
            "created_at": datetime(2026, 2, 1, 4, 30, tzinfo=timezone.utc),
        },
        # Overnight shift employee
        {
            "emp_code": "EMP0005",
            "name": "Esha Nair",
            "email": "esha@example.com",
            "department": "Support",
            "shift_start": "22:00",
            "shift_end": "06:00",
            "joined_on": "2026-03-01",
            "created_at": datetime(2026, 3, 1, 4, 30, tzinfo=timezone.utc),
        },
        # Employee with ZERO logs
        {
            "emp_code": "EMP0006",
            "name": "Farhan Ali",
            "email": "farhan@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-06-01",
            "created_at": datetime(2026, 6, 1, 4, 30, tzinfo=timezone.utc),
        },
        # Employee who joins AFTER the test month (joined_on 2026-08-01)
        {
            "emp_code": "EMP0007",
            "name": "Gita Verma",
            "email": "gita@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-08-01",
            "created_at": datetime(2026, 8, 1, 4, 30, tzinfo=timezone.utc),
        },
    ]

    # Add more background employees to reach ~500 employees for realistic distribution
    departments = ["Engineering", "Sales", "Support", "Marketing", "HR", "Finance", "Operations", "Product"]
    for i in range(8, 501):
        emp_code = f"EMP{i:04d}"
        dept = departments[i % len(departments)]
        employees.append({
            "emp_code": emp_code,
            "name": f"Employee {i}",
            "email": f"emp{i}@example.com",
            "department": dept,
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2025-01-01",
            "created_at": datetime(2025, 1, 1, 4, 30, tzinfo=timezone.utc),
        })

    db.employees.insert_many(employees)
    print(f"Inserted {len(employees)} employees.")

    print("Generating attendance logs including awkward cases...")
    logs = []

    # 1. Awkward case: EMP0003 and EMP0004 tie on late minutes in July 2026 (both exactly 60 minutes)
    logs.append({
        "emp_code": "EMP0003",
        "date": "2026-07-01",
        "status": "PRESENT",
        "punch_in": datetime(2026, 7, 1, 4, 30, tzinfo=timezone.utc), # 10:00 IST (+30 late)
        "punch_out": datetime(2026, 7, 1, 13, 0, tzinfo=timezone.utc),
        "work_hours": 8.5,
        "late_minutes": 30,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    })
    logs.append({
        "emp_code": "EMP0003",
        "date": "2026-07-02",
        "status": "PRESENT",
        "punch_in": datetime(2026, 7, 2, 4, 30, tzinfo=timezone.utc), # +30 late -> total 60
        "punch_out": datetime(2026, 7, 2, 13, 0, tzinfo=timezone.utc),
        "work_hours": 8.5,
        "late_minutes": 30,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    })

    logs.append({
        "emp_code": "EMP0004",
        "date": "2026-07-01",
        "status": "PRESENT",
        "punch_in": datetime(2026, 7, 1, 5, 0, tzinfo=timezone.utc), # 10:30 IST (+60 late) -> total 60
        "punch_out": datetime(2026, 7, 1, 13, 0, tzinfo=timezone.utc),
        "work_hours": 8.0,
        "late_minutes": 60,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    })

    # 2. Awkward case: Weekend records (EMP0001 on Saturday 2026-07-04 and Sunday 2026-07-05)
    logs.append({
        "emp_code": "EMP0001",
        "date": "2026-07-04", # Saturday
        "status": "PRESENT",
        "punch_in": datetime(2026, 7, 4, 4, 15, tzinfo=timezone.utc), # 09:45 IST (+15 late)
        "punch_out": datetime(2026, 7, 4, 14, 0, tzinfo=timezone.utc), # Overtime
        "work_hours": 9.75,
        "late_minutes": 15,
        "overtime_minutes": 60,
        "half_day": False,
        "history": [],
    })
    logs.append({
        "emp_code": "EMP0001",
        "date": "2026-07-05", # Sunday
        "status": "PRESENT",
        "punch_in": datetime(2026, 7, 5, 4, 0, tzinfo=timezone.utc),
        "punch_out": datetime(2026, 7, 5, 13, 0, tzinfo=timezone.utc),
        "work_hours": 9.0,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    })

    # 3. Awkward case: Overnight shift record (EMP0005)
    # Punched in 21:55 IST on 2026-07-06, punched out 06:40 IST on 2026-07-07
    logs.append({
        "emp_code": "EMP0005",
        "date": "2026-07-06",
        "status": "PRESENT",
        "punch_in": datetime(2026, 7, 6, 16, 25, tzinfo=timezone.utc),
        "punch_out": datetime(2026, 7, 7, 1, 10, tzinfo=timezone.utc),
        "work_hours": 8.75,
        "late_minutes": 0,
        "overtime_minutes": 40,
        "half_day": False,
        "history": [],
    })

    # 4. Awkward case: Legacy record missing history and half_day
    logs.append({
        "emp_code": "EMP0002",
        "date": "2026-07-15",
        "status": "PRESENT",
        "punch_in": datetime(2026, 7, 15, 4, 0, tzinfo=timezone.utc),
        "punch_out": datetime(2026, 7, 15, 13, 0, tzinfo=timezone.utc),
        "work_hours": 9.0,
        "late_minutes": 0,
        "overtime_minutes": 0,
        # omitted half_day and history
    })

    # 5. Awkward case: Regularized record with history
    logs.append({
        "emp_code": "EMP0001",
        "date": "2026-07-06",
        "status": "PRESENT",
        "punch_in": datetime(2026, 7, 6, 3, 58, tzinfo=timezone.utc),
        "punch_out": datetime(2026, 7, 6, 13, 0, tzinfo=timezone.utc),
        "work_hours": 9.03,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [
            {
                "at": datetime(2026, 7, 7, 5, 0, tzinfo=timezone.utc),
                "by": "admin",
                "reason": "badge scan issue",
                "changes": {
                    "punch_in": {
                        "from": datetime(2026, 7, 6, 4, 35, tzinfo=timezone.utc),
                        "to": datetime(2026, 7, 6, 3, 58, tzinfo=timezone.utc),
                    },
                    "late_minutes": {"from": 35, "to": 0}
                }
            }
        ]
    })

    # 6. Awkward case: Missing weekday (EMP0001 has no record for 2026-07-08 Wednesday)
    # 2026-07-07 is PRESENT, 2026-07-09 is PRESENT. 2026-07-08 has NO document.
    logs.append({
        "emp_code": "EMP0001",
        "date": "2026-07-07",
        "status": "PRESENT",
        "punch_in": datetime(2026, 7, 7, 4, 0, tzinfo=timezone.utc),
        "punch_out": datetime(2026, 7, 7, 13, 0, tzinfo=timezone.utc),
        "work_hours": 9.0,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    })
    logs.append({
        "emp_code": "EMP0001",
        "date": "2026-07-09",
        "status": "PRESENT",
        "punch_in": datetime(2026, 7, 9, 4, 0, tzinfo=timezone.utc),
        "punch_out": datetime(2026, 7, 9, 13, 0, tzinfo=timezone.utc),
        "work_hours": 9.0,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    })

    # Insert initial awkward cases
    db.attendance_logs.insert_many(logs)
    print(f"Inserted {len(logs)} explicit awkward cases.")

    # Now generate ~100,000 attendance records across background employees for dates in 2025 and 2026
    # 500 employees x 200 days = 100,000 records
    print("Generating remaining bulk attendance records (~100,000 documents)...")
    batch_size = 10000
    batch = []
    total_generated = len(logs)

    start_d = date(2025, 4, 1)
    num_days = 350 # ~350 days

    # Pre-generate dates
    date_list = [start_d + timedelta(days=d) for d in range(num_days)]

    emp_codes = [f"EMP{i:04d}" for i in range(10, 501)] # 491 employees

    for d in date_list:
        d_str = d.isoformat()
        is_wkend = d.weekday() >= 5
        for emp_c in emp_codes:
            # 85% attendance on weekdays, 5% on weekends
            if (not is_wkend and random.random() < 0.85) or (is_wkend and random.random() < 0.05):
                is_late = random.random() < 0.15
                late_m = random.randint(15, 60) if is_late else 0
                is_ot = random.random() < 0.20
                ot_m = random.randint(30, 90) if is_ot else 0
                is_half = random.random() < 0.05
                wh = 3.5 if is_half else round(8.0 + random.random() * 1.5, 2)

                p_in = datetime(d.year, d.month, d.day, 4, 0, tzinfo=timezone.utc)
                p_out = datetime(d.year, d.month, d.day, 13, 0, tzinfo=timezone.utc)

                batch.append({
                    "emp_code": emp_c,
                    "date": d_str,
                    "status": "PRESENT" if random.random() < 0.8 else ("WFH" if random.random() < 0.7 else "ON_DUTY"),
                    "punch_in": p_in,
                    "punch_out": p_out,
                    "work_hours": wh,
                    "late_minutes": late_m,
                    "overtime_minutes": ot_m,
                    "half_day": is_half,
                    "history": [],
                })

                if len(batch) >= batch_size:
                    db.attendance_logs.insert_many(batch)
                    total_generated += len(batch)
                    print(f"Seeded {total_generated} records...")
                    batch = []

            if total_generated >= 100000:
                break
        if total_generated >= 100000:
            break

    if batch:
        db.attendance_logs.insert_many(batch)
        total_generated += len(batch)

    print(f"Total attendance records seeded: {total_generated}")
    print("Seeding completed successfully!")


if __name__ == "__main__":
    seed()
