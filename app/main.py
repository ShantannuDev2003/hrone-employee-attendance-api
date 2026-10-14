"""
Employee Attendance & Analytics API

Run: uvicorn app.main:app --port 8000
Env: MONGO_URI, MONGO_DB (.env used as fallback only)
"""
import calendar
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import json
import os
import re
from typing import Annotated, Any, Dict, List, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, BeforeValidator, Field, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

# --------------------------------------------------------------------------- #
# Environment & Configuration
# --------------------------------------------------------------------------- #
# Load .env only as fallback; real environment variables must win
load_dotenv(override=False)

MONGO_URI = os.environ.get("MONGO_URI") or "mongodb://localhost:27017"
MONGO_DB = os.environ.get("MONGO_DB") or "attendance_db"

client: MongoClient = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


# --------------------------------------------------------------------------- #
# Startup & Lifespan (Idempotent Index Creation)
# --------------------------------------------------------------------------- #
def ensure_indexes():
    """Create all required indexes idempotently at startup."""
    # Employees indexes:
    # 1. Unique index on emp_code
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="idx_emp_code_unique")
    # 2. Query indexes for department, joined_on, emp_code
    db.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING), ("emp_code", ASCENDING)],
        name="idx_emp_dept_joined_code"
    )
    db.employees.create_index(
        [("joined_on", ASCENDING), ("department", ASCENDING)],
        name="idx_emp_joined_dept"
    )

    # Attendance logs indexes:
    # 1. UNIQUE compound index on (emp_code, date)
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="idx_emp_date_unique"
    )
    # 2. Index for list_attendance sort and date filtering
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="idx_att_date_desc_emp_asc"
    )
    # 3. Index for punch-out lookup
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)],
        name="idx_att_emp_punchin_desc"
    )
    # 4. Index for late leaderboard & monthly analytics
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("late_minutes", ASCENDING), ("emp_code", ASCENDING)],
        name="idx_att_date_late_emp"
    )
    # 5. Index for department trend matching
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("emp_code", ASCENDING)],
        name="idx_att_date_asc_emp_asc"
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create indexes on startup
    ensure_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan
)





# --------------------------------------------------------------------------- #
# Helpers: Time & Business Math (R1 - R5, R8)
# --------------------------------------------------------------------------- #
def validate_epoch_millis(v: Any) -> Optional[int]:
    """Validate that value is integer epoch milliseconds, rejecting bools, floats, and seconds."""
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError("Instant must be an integer count of epoch milliseconds")
    if v < 100000000000 or v > 4102444800000:
        raise ValueError("Instant out of valid epoch milliseconds range")
    return v


EpochMillis = Annotated[int, BeforeValidator(validate_epoch_millis)]
EpochMillisNullable = Annotated[Optional[int], BeforeValidator(validate_epoch_millis)]


def truncate_to_seconds(ms: int) -> int:
    """R1: Instants are truncated to whole seconds before any calculation."""
    return (ms // 1000) * 1000


def ms_to_datetime(ms: Optional[int]) -> Optional[datetime]:
    if ms is None:
        return None
    # Truncate to seconds first
    trunc_ms = truncate_to_seconds(ms)
    return datetime.fromtimestamp(trunc_ms / 1000, tz=timezone.utc)


def datetime_to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp()) * 1000


def round_half_up(val: float | Decimal, decimals: int = 2) -> float:
    """R8: Rounding is HALF-UP via Decimal, never Python round()."""
    d = Decimal(str(val))
    target = Decimal("10") ** (-decimals)
    rounded = d.quantize(target, rounding=ROUND_HALF_UP)
    return float(rounded)


def is_overnight_shift(shift_start: str, shift_end: str) -> bool:
    h_s, m_s = map(int, shift_start.split(":"))
    h_e, m_e = map(int, shift_end.split(":"))
    return (h_e, m_e) <= (h_s, m_s)


def get_attendance_date(punch_in_dt: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Shift times and attendance dates are IST (UTC+05:30).
    A record's date is the IST calendar date of its punch_in.
    Overnight shifts (shift_end <= shift_start): a punch-in whose IST time is
    strictly earlier than shift_end belongs to the shift that started the previous day.
    """
    punch_in_ist = punch_in_dt.astimezone(IST)
    if is_overnight_shift(shift_start, shift_end):
        h_e, m_e = map(int, shift_end.split(":"))
        punch_time = (punch_in_ist.hour, punch_in_ist.minute, punch_in_ist.second)
        if punch_time < (h_e, m_e, 0):
            return (punch_in_ist.date() - timedelta(days=1)).isoformat()
    return punch_in_ist.date().isoformat()


def compute_late_minutes(punch_in_dt: datetime, shift_start: str, shift_end: str, date_str: str) -> int:
    """
    R2: Grace period 10 minutes. Late only if punch-in is strictly more than 10:00
    after shift_start; then late_minutes = whole minutes (floor) elapsed since shift_start.
    """
    h_s, m_s = map(int, shift_start.split(":"))
    att_date = date.fromisoformat(date_str)
    shift_start_dt = datetime.combine(att_date, time(h_s, m_s), tzinfo=IST)
    punch_in_ist = punch_in_dt.astimezone(IST)

    diff_seconds = int((punch_in_ist - shift_start_dt).total_seconds())
    # Strictly more than 10:00 (600 seconds)
    if diff_seconds > 600:
        return diff_seconds // 60
    return 0


def compute_overtime(punch_out_dt: datetime, shift_start: str, shift_end: str, date_str: str) -> int:
    """
    R3: Overtime = whole minutes (floor) between shift_end and punch_out,
    counted only when >= 30; otherwise 0.
    """
    h_e, m_e = map(int, shift_end.split(":"))
    att_date = date.fromisoformat(date_str)
    if is_overnight_shift(shift_start, shift_end):
        shift_end_date = att_date + timedelta(days=1)
    else:
        shift_end_date = att_date

    shift_end_dt = datetime.combine(shift_end_date, time(h_e, m_e), tzinfo=IST)
    punch_out_ist = punch_out_dt.astimezone(IST)

    diff_seconds = int((punch_out_ist - shift_end_dt).total_seconds())
    diff_minutes = diff_seconds // 60
    return diff_minutes if diff_minutes >= 30 else 0


def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    """R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up."""
    diff_seconds = int((punch_out_dt - punch_in_dt).total_seconds())
    hours = Decimal(diff_seconds) / Decimal(3600)
    rounded = hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(rounded)


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: After punch-out, half_day is true when rounded work_hours < 4.50."""
    if work_hours is None:
        return False
    return work_hours < 4.50


# --------------------------------------------------------------------------- #
# Serialization Helpers
# --------------------------------------------------------------------------- #
def serialize_employee(doc: dict) -> dict:
    res = dict(doc)
    res.pop("_id", None)
    created_at = res.get("created_at")
    if isinstance(created_at, datetime):
        res["created_at"] = datetime_to_ms(created_at)
    return res


def serialize_attendance(doc: dict) -> dict:
    res = dict(doc)
    res.pop("_id", None)
    res.pop("id", None)

    res["punch_in"] = datetime_to_ms(res.get("punch_in"))
    res["punch_out"] = datetime_to_ms(res.get("punch_out"))
    res["late_minutes"] = res.get("late_minutes", 0)
    res["overtime_minutes"] = res.get("overtime_minutes", 0)
    res["half_day"] = res.get("half_day", False)

    raw_history = res.get("history") or []
    formatted_history = []
    for h in raw_history:
        at_val = datetime_to_ms(h.get("at")) if isinstance(h.get("at"), datetime) else h.get("at")
        entry = {
            "at": at_val,
            "by": h.get("by", ""),
            "reason": h.get("reason", ""),
            "changes": {}
        }
        for k, v in h.get("changes", {}).items():
            chg = dict(v)
            if k in ("punch_in", "punch_out"):
                chg["from"] = datetime_to_ms(chg.get("from")) if isinstance(chg.get("from"), datetime) else chg.get("from")
                chg["to"] = datetime_to_ms(chg.get("to")) if isinstance(chg.get("to"), datetime) else chg.get("to")
            entry["changes"][k] = chg
        formatted_history.append(entry)
    res["history"] = formatted_history
    return res


# --------------------------------------------------------------------------- #
# Pydantic Schemas
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @model_validator(mode="after")
    def validate_shifts(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: str = Field(default="PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None


class RegularizeRequest(BaseModel):
    status: Optional[str] = Field(default=None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$")
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Reusable Query / Pipeline Builders (Used by endpoints and GET /admin/explain)
# --------------------------------------------------------------------------- #
def build_attendance_list_query(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status_filter: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status_filter:
        q["status"] = status_filter

    sort_spec = [("date", DESCENDING), ("emp_code", ASCENDING)]
    skip = (page - 1) * page_size
    return q, sort_spec, skip, page_size


def build_employee_monthly_pipeline(emp_code: str, month: str):
    start_date = f"{month}-01"
    end_date = f"{month}-31"
    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_date, "$lte": end_date}
            }
        },
        {
            "$project": {
                "date": 1,
                "status": 1,
                "half_day": {"$ifNull": ["$half_day", False]},
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "day_of_week": {
                    "$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}
                }
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]}
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"}
            }
        }
    ]
    return pipeline


def build_department_summary_pipeline(month: str, department: Optional[str] = None):
    start_date = f"{month}-01"
    # Find last day of month
    y, m = map(int, month.split("-"))
    _, last_day = calendar.monthrange(y, m)
    end_date = f"{month}-{last_day:02d}"

    match_stage: Dict[str, Any] = {"joined_on": {"$lte": end_date}}
    if department:
        match_stage["department"] = department

    pipeline = [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", start_date]},
                                    {"$lte": ["$date", end_date]}
                                ]
                            }
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {
            "$project": {
                "department": 1,
                "emp_code": 1,
                "logs": 1
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "employees": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {
                                        "$in": [
                                            {"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}},
                                            [2, 3, 4, 5, 6]
                                        ]
                                    }
                                ]
                            },
                            {"$cond": ["$logs.half_day", 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "valid_work_hours": {
                    "$push": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]}
                                ]
                            },
                            "$logs.work_hours",
                            "$$REMOVE"
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$gt": ["$logs.late_minutes", 0]}
                                ]
                            },
                            1,
                            0
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$cond": [
                            {"$ne": ["$logs", None]},
                            {"$ifNull": ["$logs.late_minutes", 0]},
                            0
                        ]
                    }
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$eq": ["$logs.status", "LEAVE"]}
                                ]
                            },
                            1,
                            0
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$eq": ["$logs.status", "ON_DUTY"]}
                                ]
                            },
                            1,
                            0
                        ]
                    }
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$employees"},
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": [{"$size": "$valid_work_hours"}, 0]},
                        {"$avg": "$valid_work_hours"},
                        None
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1
            }
        },
        {"$sort": {"department": ASCENDING}}
    ]
    return pipeline


def build_late_leaderboard_pipeline(month: str, limit: int, department: Optional[str] = None):
    start_date = f"{month}-01"
    end_date = f"{month}-31"
    pipeline = [
        {
            "$match": {
                "date": {"$gte": start_date, "$lte": end_date},
                "late_minutes": {"$gt": 0}
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp"
            }
        },
        {"$unwind": "$emp"}
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": DESCENDING},
                "output": {
                    "rank": {"$rank": {}}
                }
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1
            }
        }
    ])
    return pipeline


def build_department_trend_pipeline(department: str, from_date: str, to_date: str):
    from_dt = datetime.fromisoformat(from_date)
    to_dt = datetime.fromisoformat(to_date) + timedelta(days=1)

    pipeline = [
        {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp"
            }
        },
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "date_str": "$_id",
                "date_obj": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1
            }
        },
        {
            "$densify": {
                "field": "date_obj",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt]
                }
            }
        },
        {
            "$project": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_obj"}},
                "date_obj": 1,
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]}
            }
        },
        {
            "$set": {
                "is_working_day": {
                    "$in": [
                        {"$dayOfWeek": "$date_obj"},
                        [2, 3, 4, 5, 6]
                    ]
                }
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"day_str": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$day_str"]}
                                ]
                            }
                        }
                    },
                    {"$count": "count"}
                ],
                "as": "hc"
            }
        },
        {
            "$set": {
                "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.count", 0]}, 0]}
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None
                    ]
                }
            }
        },
        {"$sort": {"date": ASCENDING}},
        {
            "$setWindowFields": {
                "sortBy": {"date": ASCENDING},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"]
                        }
                    }
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1
            }
        }
    ]
    return pipeline


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

# 1. GET /health
@app.get("/health", tags=["system"])
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=503, detail="Database unavailable")


# 2. POST /employees
@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeCreate):
    now_ms = truncate_to_seconds(int(datetime.now(timezone.utc).timestamp() * 1000))
    created_at_dt = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)

    doc = body.model_dump()
    doc["created_at"] = created_at_dt

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    return serialize_employee(doc)


# 3. GET /employees
@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    q: Dict[str, Any] = {}
    if department:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    items = [serialize_employee(doc) for doc in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size
    }


# 4. POST /attendance/punch-in
@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        p_ms = truncate_to_seconds(body.punched_at)
    else:
        p_ms = truncate_to_seconds(int(datetime.now(timezone.utc).timestamp() * 1000))

    punch_in_dt = datetime.fromtimestamp(p_ms / 1000, tz=timezone.utc)
    att_date = get_attendance_date(punch_in_dt, emp["shift_start"], emp["shift_end"])
    late_mins = compute_late_minutes(punch_in_dt, emp["shift_start"], emp["shift_end"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_mins,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Already punched in for this date")

    return serialize_attendance(doc)


# 5. POST /attendance/punch-out
@app.post("/attendance/punch-out", status_code=200, tags=["attendance"])
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        p_ms = truncate_to_seconds(body.punched_at)
    else:
        p_ms = truncate_to_seconds(int(datetime.now(timezone.utc).timestamp() * 1000))

    punch_out_dt = datetime.fromtimestamp(p_ms / 1000, tz=timezone.utc)

    # Find the employee's most recent record whose punch_in is not None
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None}},
        sort=[("punch_in", DESCENDING)]
    )

    if not record:
        raise HTTPException(status_code=404, detail="No punch-in found for employee")

    punch_in_dt = record["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)

    if punch_out_dt <= punch_in_dt:
        raise HTTPException(status_code=422, detail="punched_at must be strictly greater than punch_in")

    if (punch_out_dt - punch_in_dt).total_seconds() > 86400:
        raise HTTPException(status_code=422, detail="punched_at cannot be more than 24 hours after punch_in")

    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Attendance record is already punched out")

    work_hours = compute_work_hours(punch_in_dt, punch_out_dt)
    overtime = compute_overtime(punch_out_dt, emp["shift_start"], emp["shift_end"], record["date"])
    half_day = compute_half_day(work_hours)

    # Atomic conditional update
    res = db.attendance_logs.update_one(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hours,
                "overtime_minutes": overtime,
                "half_day": half_day,
            }
        }
    )

    if res.modified_count == 0:
        raise HTTPException(status_code=409, detail="Attendance record already punched out")

    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    return serialize_attendance(updated)


# 6. GET /attendance
@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[str] = Query(default=None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be on or before date_to")

    q, sort_spec, skip, limit = build_attendance_list_query(
        emp_code=emp_code,
        date_from=date_from,
        date_to=date_to,
        status_filter=status,
        page=page,
        page_size=page_size
    )

    total = db.attendance_logs.count_documents(q)
    cursor = db.attendance_logs.find(q).sort(sort_spec).skip(skip).limit(limit)
    items = [serialize_attendance(doc) for doc in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size
    }


# 7. PATCH /attendance/{emp_code}/{date}
@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(
    emp_code: str = Path(...),
    date_str: str = Path(..., alias="date", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeRequest = ...,
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_str})
    if not record:
        raise HTTPException(status_code=404, detail="Attendance record not found for date")

    # Validation: setting ABSENT or LEAVE clears both punch times; supplying punch times with it is 422
    if body.status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(status_code=422, detail="Cannot provide punch times when setting status to ABSENT or LEAVE")

    final_status = body.status if body.status is not None else record.get("status")

    if final_status in ("ABSENT", "LEAVE"):
        final_punch_in_dt = None
        final_punch_out_dt = None
        final_work_hours = None
        final_late_minutes = 0
        final_overtime_minutes = 0
        final_half_day = False
    else:
        # Presence status requires punch_in
        if body.punch_in is not None:
            final_punch_in_dt = ms_to_datetime(body.punch_in)
        else:
            final_punch_in_dt = record.get("punch_in")
            if final_punch_in_dt is not None and final_punch_in_dt.tzinfo is None:
                final_punch_in_dt = final_punch_in_dt.replace(tzinfo=timezone.utc)

        if final_punch_in_dt is None:
            raise HTTPException(status_code=422, detail="Presence status requires a punch_in")

        # punch_in must stay on the record's attendance date (R1)
        calc_date = get_attendance_date(final_punch_in_dt, emp["shift_start"], emp["shift_end"])
        if calc_date != date_str:
            raise HTTPException(status_code=422, detail="punch_in does not belong to the record's attendance date")

        # punch_out
        if body.punch_out is not None:
            final_punch_out_dt = ms_to_datetime(body.punch_out)
        else:
            final_punch_out_dt = record.get("punch_out")
            if final_punch_out_dt is not None and final_punch_out_dt.tzinfo is None:
                final_punch_out_dt = final_punch_out_dt.replace(tzinfo=timezone.utc)

        if final_punch_out_dt is not None:
            if final_punch_out_dt <= final_punch_in_dt:
                raise HTTPException(status_code=422, detail="punch_out must be strictly greater than punch_in")
            if (final_punch_out_dt - final_punch_in_dt).total_seconds() > 86400:
                raise HTTPException(status_code=422, detail="punch_out cannot be more than 24 hours after punch_in")

            final_work_hours = compute_work_hours(final_punch_in_dt, final_punch_out_dt)
            final_overtime_minutes = compute_overtime(final_punch_out_dt, emp["shift_start"], emp["shift_end"], date_str)
            final_half_day = compute_half_day(final_work_hours)
        else:
            final_work_hours = None
            final_overtime_minutes = 0
            final_half_day = False

        final_late_minutes = compute_late_minutes(final_punch_in_dt, emp["shift_start"], emp["shift_end"], date_str)

    # Detect actual changes
    changes_mongo: Dict[str, Any] = {}

    def check_diff(field_name: str, old_val: Any, new_val: Any):
        if old_val != new_val:
            changes_mongo[field_name] = {"from": old_val, "to": new_val}

    check_diff("status", record.get("status"), final_status)
    old_pin = record.get("punch_in")
    if old_pin is not None and old_pin.tzinfo is None:
        old_pin = old_pin.replace(tzinfo=timezone.utc)
    check_diff("punch_in", old_pin, final_punch_in_dt)

    old_pout = record.get("punch_out")
    if old_pout is not None and old_pout.tzinfo is None:
        old_pout = old_pout.replace(tzinfo=timezone.utc)
    check_diff("punch_out", old_pout, final_punch_out_dt)

    check_diff("work_hours", record.get("work_hours"), final_work_hours)
    check_diff("late_minutes", record.get("late_minutes", 0), final_late_minutes)
    check_diff("overtime_minutes", record.get("overtime_minutes", 0), final_overtime_minutes)
    check_diff("half_day", record.get("half_day", False), final_half_day)

    if not changes_mongo:
        raise HTTPException(status_code=422, detail="A request that changes nothing is rejected")

    now_ms = truncate_to_seconds(int(datetime.now(timezone.utc).timestamp() * 1000))
    now_dt = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)

    history_entry = {
        "at": now_dt,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes_mongo
    }

    # Concurrent regularizations must not lose history entries
    # Match against current history length to prevent lost updates
    current_history = record.get("history") or []
    filter_q = {
        "_id": record["_id"],
        "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, len(current_history)]}
    }

    update_doc = {
        "$set": {
            "status": final_status,
            "punch_in": final_punch_in_dt,
            "punch_out": final_punch_out_dt,
            "work_hours": final_work_hours,
            "late_minutes": final_late_minutes,
            "overtime_minutes": final_overtime_minutes,
            "half_day": final_half_day,
        },
        "$push": {
            "history": history_entry
        }
    }

    res = db.attendance_logs.update_one(filter_q, update_doc)
    if res.modified_count == 0:
        raise HTTPException(status_code=409, detail="Concurrent regularization conflict")

    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    return serialize_attendance(updated)


# 8. GET /analytics/employees/{emp_code}/monthly
@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    # Working days calculation per R7
    y, m = map(int, month.split("-"))
    _, num_days = calendar.monthrange(y, m)
    month_start = date(y, m, 1)
    month_end = date(y, m, num_days)

    emp_joined = date.fromisoformat(emp["joined_on"])
    start_calc = max(month_start, emp_joined)

    working_days = 0
    if start_calc <= month_end:
        cur = start_calc
        while cur <= month_end:
            if cur.weekday() < 5:  # Monday to Friday
                working_days += 1
            cur += timedelta(days=1)

    pipeline = build_employee_monthly_pipeline(emp_code, month)
    agg_res = list(db.attendance_logs.aggregate(pipeline))

    if agg_res:
        row = agg_res[0]
        present_days = float(row.get("present_days", 0.0))
        leave_days = int(row.get("leave_days", 0))
        late_count = int(row.get("late_count", 0))
        total_late = int(row.get("total_late_minutes", 0))
        total_ot = int(row.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if working_days > 0:
        att_pct_raw = (present_days / working_days) * 100
        attendance_pct = round_half_up(att_pct_raw, 2)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct
    }


# 9. GET /analytics/departments/summary
@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    pipeline = build_department_summary_pipeline(month, department)
    results = list(db.employees.aggregate(pipeline))

    items = []
    for r in results:
        avg_wh = r.get("avg_work_hours")
        if avg_wh is not None:
            avg_wh = round_half_up(avg_wh, 2)
        items.append({
            "department": r["department"],
            "headcount": int(r["headcount"]),
            "present_days": float(r["present_days"]),
            "avg_work_hours": avg_wh,
            "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": int(r["leave_count"]),
            "on_duty_count": int(r["on_duty_count"]),
        })

    return {
        "month": month,
        "items": items
    }


# 10. GET /analytics/leaderboard/late
@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    results = list(db.attendance_logs.aggregate(pipeline))

    items = []
    for r in results:
        items.append({
            "rank": int(r["rank"]),
            "emp_code": r["emp_code"],
            "name": r["name"],
            "department": r["department"],
            "total_late_minutes": int(r["total_late_minutes"]),
            "late_count": int(r["late_count"]),
        })

    return {
        "month": month,
        "items": items
    }


# 11. GET /analytics/departments/{department}/trend
@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    d_from = date.fromisoformat(from_date)
    d_to = date.fromisoformat(to_date)
    if d_to < d_from:
        raise HTTPException(status_code=422, detail="'to' date must be on or after 'from' date")
    if (d_to - d_from).days > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

    # Check if department exists in employees
    emp_count = db.employees.count_documents({"department": department})
    if emp_count == 0:
        raise HTTPException(status_code=404, detail="Department not found")

    pipeline = build_department_trend_pipeline(department, from_date, to_date)
    results = list(db.attendance_logs.aggregate(pipeline))

    items = []
    for r in results:
        att_rate = r.get("attendance_rate")
        if att_rate is not None:
            att_rate = round_half_up(att_rate, 4)

        mov_avg = r.get("moving_avg_7d")
        if mov_avg is not None:
            mov_avg = round_half_up(mov_avg, 4)

        items.append({
            "date": r["date"],
            "is_working_day": bool(r["is_working_day"]),
            "headcount": int(r["headcount"]),
            "present_count": float(r["present_count"]),
            "late_count": int(r["late_count"]),
            "attendance_rate": att_rate,
            "moving_avg_7d": mov_avg,
        })

    return {
        "department": department,
        "items": items
    }


# 12. GET /admin/explain/{endpoint}
@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: str = Path(..., pattern=r"^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"),
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: Optional[int] = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status_filter: Optional[str] = Query(default=None, alias="status"),
    from_date: Optional[str] = Query(default=None, alias="from"),
    to_date: Optional[str] = Query(default=None, alias="to"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    month_regex = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must be on or before date_to")
        q, sort_spec, skip, page_sz = build_attendance_list_query(
            emp_code=emp_code,
            date_from=date_from,
            date_to=date_to,
            status_filter=status_filter,
            page=page,
            page_size=page_size
        )
        sort_dict = dict(sort_spec)
        explain_raw = db.command(
            "explain",
            {"find": "attendance_logs", "filter": q, "sort": sort_dict, "skip": skip, "limit": page_sz},
            verbosity="executionStats"
        )
        collection_name = "attendance_logs"

    elif endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(status_code=422, detail="emp_code is required for employee_monthly explain")
        if not month or not month_regex.match(month):
            raise HTTPException(status_code=422, detail="Valid month is required for employee_monthly explain")
        pipeline = build_employee_monthly_pipeline(emp_code, month)
        explain_raw = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats"
        )
        collection_name = "attendance_logs"

    elif endpoint == "department_summary":
        if not month or not month_regex.match(month):
            raise HTTPException(status_code=422, detail="Valid month is required for department_summary explain")
        pipeline = build_department_summary_pipeline(month, department)
        explain_raw = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats"
        )
        collection_name = "employees"

    elif endpoint == "late_leaderboard":
        if not month or not month_regex.match(month):
            raise HTTPException(status_code=422, detail="Valid month is required for late_leaderboard explain")
        if limit is None or limit < 1 or limit > 50:
            raise HTTPException(status_code=422, detail="limit must be between 1 and 50")
        pipeline = build_late_leaderboard_pipeline(month, limit, department)
        explain_raw = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats"
        )
        collection_name = "attendance_logs"

    elif endpoint == "department_trend":
        if not department:
            raise HTTPException(status_code=422, detail="department is required for department_trend explain")
        if not from_date or not to_date:
            raise HTTPException(status_code=422, detail="'from' and 'to' dates are required for department_trend explain")
        try:
            d_from = date.fromisoformat(from_date)
            d_to = date.fromisoformat(to_date)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid date format")
        if d_to < d_from:
            raise HTTPException(status_code=422, detail="'to' date must be on or after 'from' date")
        if (d_to - d_from).days > 92:
            raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")
        pipeline = build_department_trend_pipeline(department, from_date, to_date)
        explain_raw = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats"
        )
        collection_name = "attendance_logs"
    else:
        raise HTTPException(status_code=422, detail="Unknown explain endpoint")

    # Clean explain output to native JSON
    clean_explain = json.loads(json_util.dumps(explain_raw))
    return {
        "endpoint": endpoint,
        "collection": collection_name,
        "explain": clean_explain
    }
