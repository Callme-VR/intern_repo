"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""

import calendar
import json
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, List, Dict, Any

import certifi
from bson import json_util
from bson.son import SON
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, Field, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

mongo_uri = os.getenv("MONGO_URI") or os.getenv("MONGODB_URI") or "mongodb://localhost:27017"
mongo_db = os.getenv("MONGO_DB") or os.getenv("DB_NAME") or "attendance_db"

client = MongoClient(
    mongo_uri,
    tlsCAFile=certifi.where() if "mongodb+srv:" in mongo_uri else None,
    tz_aware=True,
)
db = client[mongo_db]

IST = timezone(timedelta(hours=5, minutes=30))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure all required indexes exist at startup
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index("department")
    db.employees.create_index("joined_on")
    db.employees.create_index([("department", 1), ("joined_on", 1)])

    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("date", 1), ("late_minutes", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    db.attendance_logs.create_index("status")
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helpers (Rules R1 - R5, R8)
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    """Converts epoch milliseconds to timezone-aware UTC datetime truncated to whole seconds (R1)."""
    sec = ms // 1000
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    """Converts datetime to epoch milliseconds integer (R1)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp()) * 1000


def round_half_up(val: Optional[float | Decimal], decimals: int = 2) -> Optional[float]:
    """R8: 2 decimals half-up (rates: 4 decimals half-up)."""
    if val is None:
        return None
    d = Decimal(str(val)) if not isinstance(val, Decimal) else val
    target = Decimal("10") ** (-decimals)
    return float(d.quantize(target, rounding=ROUND_HALF_UP))


def get_attendance_date(punch_in_ist: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Attendance date is IST calendar date of punch_in.
    Overnight shifts (shift_end <= shift_start): punch-in earlier than shift_end belongs to the shift
    that started the previous day.
    """
    is_overnight = shift_end <= shift_start
    h_end, m_end = map(int, shift_end.split(":"))
    end_time_today = punch_in_ist.replace(hour=h_end, minute=m_end, second=0, microsecond=0)
    if is_overnight and punch_in_ist < end_time_today:
        return (punch_in_ist.date() - timedelta(days=1)).isoformat()
    return punch_in_ist.date().isoformat()


def compute_late_minutes(punch_in_ist: datetime, shift_start: str, attendance_date: str) -> int:
    """
    R2: Grace period 10 minutes. Late only if punch-in is strictly more than 10:00 after shift_start;
    then late_minutes = whole minutes (floor) since shift_start, not since grace period ended.
    """
    h, m = map(int, shift_start.split(":"))
    d = date.fromisoformat(attendance_date)
    start_dt = datetime(d.year, d.month, d.day, h, m, 0, tzinfo=IST)
    diff_sec = int((punch_in_ist - start_dt).total_seconds())
    if diff_sec > 600:
        return diff_sec // 60
    return 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    diff_sec = int((punch_out - punch_in).total_seconds())
    hours = Decimal(diff_sec) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out_ist: datetime, shift_end: str, shift_start: str, attendance_date: str) -> int:
    """
    R3: Whole minutes (floor) from shift_end to punch_out, counted only if at least 30; otherwise 0.
    Overnight shifts end on the next calendar day.
    """
    h, m = map(int, shift_end.split(":"))
    d = date.fromisoformat(attendance_date)
    if shift_end <= shift_start:
        d += timedelta(days=1)
    end_dt = datetime(d.year, d.month, d.day, h, m, 0, tzinfo=IST)
    diff_sec = int((punch_out_ist - end_dt).total_seconds())
    if diff_sec <= 0:
        return 0
    minutes = diff_sec // 60
    return minutes if minutes >= 30 else 0


def format_history_entry(h: dict) -> dict:
    changes = {}
    for field, change in h.get("changes", {}).items():
        from_val = change.get("from")
        to_val = change.get("to")
        if field in ("punch_in", "punch_out"):
            from_val = dt_to_ms(from_val) if isinstance(from_val, datetime) else from_val
            to_val = dt_to_ms(to_val) if isinstance(to_val, datetime) else to_val
        changes[field] = {"from": from_val, "to": to_val}
    at_val = h.get("at")
    return {
        "at": dt_to_ms(at_val) if isinstance(at_val, datetime) else at_val,
        "by": h.get("by"),
        "reason": h.get("reason"),
        "changes": changes,
    }


def format_attendance_record(d: dict) -> dict:
    return {
        "emp_code": d["emp_code"],
        "date": d["date"],
        "status": d["status"],
        "punch_in": dt_to_ms(d.get("punch_in")),
        "punch_out": dt_to_ms(d.get("punch_out")),
        "work_hours": d.get("work_hours"),
        "late_minutes": d.get("late_minutes", 0) if d.get("late_minutes") is not None else 0,
        "overtime_minutes": d.get("overtime_minutes", 0) if d.get("overtime_minutes") is not None else 0,
        "half_day": d.get("half_day", False) if d.get("half_day") is not None else False,
        "history": [format_history_entry(h) for h in (d.get("history") or [])],
    }


def format_employee_record(d: dict) -> dict:
    created_at = d.get("created_at")
    if isinstance(created_at, datetime):
        created_at_ms = dt_to_ms(created_at)
    else:
        created_at_ms = created_at
    return {
        "emp_code": d["emp_code"],
        "name": d["name"],
        "email": d["email"],
        "department": d["department"],
        "shift_start": d["shift_start"],
        "shift_end": d["shift_end"],
        "joined_on": d["joined_on"],
        "created_at": created_at_ms,
    }


# --------------------------------------------------------------------------- #
# Aggregation Pipeline Builders (Shared by Analytics & Explain Endpoints)
# --------------------------------------------------------------------------- #
def build_employee_monthly_pipeline(emp_code: str, month: str, last_day: int) -> list:
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": f"{month}-01", "$lte": f"{month}-{last_day:02d}"},
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
                                    {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 5]},
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]


def build_department_summary_pipeline(month: str, last_day: int, department: Optional[str] = None) -> list:
    d_end = f"{month}-{last_day:02d}"
    match_stage = {"joined_on": {"$lte": d_end}}
    if department:
        match_stage["department"] = department

    return [
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
                                    {"$gte": ["$date", f"{month}-01"]},
                                    {"$lte": ["$date", d_end]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "department": 1,
                "emp_present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$$log.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$$log.date"}}}, 5]},
                                        ]
                                    },
                                    {"$cond": [{"$eq": ["$$log.half_day", True]}, 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "emp_work_hours_sum": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$$log.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {"$ne": ["$$log.work_hours", None]},
                                        ]
                                    },
                                    "$$log.work_hours",
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "emp_work_hours_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$$log.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {"$ne": ["$$log.work_hours", None]},
                                        ]
                                    },
                                    1,
                                    0,
                                ]
                            },
                        }
                    }
                },
                "emp_late_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {"$cond": [{"$gt": ["$$log.late_minutes", 0]}, 1, 0]},
                        }
                    }
                },
                "emp_total_late_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {"$ifNull": ["$$log.late_minutes", 0]},
                        }
                    }
                },
                "emp_leave_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {"$cond": [{"$eq": ["$$log.status", "LEAVE"]}, 1, 0]},
                        }
                    }
                },
                "emp_on_duty_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {"$cond": [{"$eq": ["$$log.status", "ON_DUTY"]}, 1, 0]},
                        }
                    }
                },
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$emp_present_days"},
                "work_hours_sum": {"$sum": "$emp_work_hours_sum"},
                "work_hours_count": {"$sum": "$emp_work_hours_count"},
                "late_count": {"$sum": "$emp_late_count"},
                "total_late_minutes": {"$sum": "$emp_total_late_minutes"},
                "leave_count": {"$sum": "$emp_leave_count"},
                "on_duty_count": {"$sum": "$emp_on_duty_count"},
            }
        },
        {"$sort": {"_id": 1}},
    ]


def build_late_leaderboard_pipeline(month: str, last_day: int, limit: int = 10, department: Optional[str] = None) -> list:
    d_end = f"{month}-{last_day:02d}"
    pipeline = [
        {
            "$match": {
                "date": {"$gte": f"{month}-01", "$lte": d_end},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                }
            }
        },
        {
            "$match": {
                "rank": {"$lte": limit}
            }
        },
        {
            "$sort": {
                "total_late_minutes": -1,
                "emp_code": 1
            }
        }
    ])
    return pipeline


def build_department_trend_pipeline(department: str, d_from: date, d_to: date) -> list:
    from_dt = datetime(d_from.year, d_from.month, d_from.day, tzinfo=timezone.utc)
    to_dt = datetime(d_to.year, d_to.month, d_to.day, tzinfo=timezone.utc)
    bound_end_dt = to_dt + timedelta(days=1)

    return [
        {"$match": {"department": department}},
        {"$limit": 1},
        {
            "$project": {
                "dates": [
                    {"date_dt": from_dt, "is_boundary": True},
                    {"date_dt": to_dt, "is_boundary": True},
                ]
            }
        },
        {"$unwind": "$dates"},
        {"$replaceRoot": {"newRoot": "$dates"}},
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, bound_end_dt],
                },
            }
        },
        {
            "$project": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                "day_of_week": {"$isoDayOfWeek": "$date_dt"},
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": {"$lte": ["$day_of_week", 5]},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"cur_date": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$cur_date"]},
                                ]
                            }
                        }
                    }
                ],
                "as": "dept_emps",
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"cur_date": "$date", "valid_emps": "$dept_emps.emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$in": ["$emp_code", "$$valid_emps"]},
                                    {"$eq": ["$date", "$$cur_date"]},
                                ]
                            }
                        }
                    }
                ],
                "as": "day_logs",
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": {"$size": "$dept_emps"},
                "present_count": {
                    "$sum": {
                        "$map": {
                            "input": "$day_logs",
                            "as": "log",
                            "in": {
                                "$cond": [
                                    {"$in": ["$$log.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$cond": [{"$eq": ["$$log.half_day", True]}, 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "late_count": {
                    "$sum": {
                        "$map": {
                            "input": "$day_logs",
                            "as": "log",
                            "in": {"$cond": [{"$gt": ["$$log.late_minutes", 0]}, 1, 0]},
                        }
                    }
                },
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                },
            }
        },
        {"$sort": {"date": 1}},
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"],
                        },
                    }
                },
            }
        },
    ]


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @model_validator(mode="after")
    def validate_shifts(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, strict=True, ge=100000000000, le=4102444800000)
    status: str = Field("PRESENT", pattern="^(PRESENT|WFH|ON_DUTY)$")


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, strict=True, ge=100000000000, le=4102444800000)


class RegularizeIn(BaseModel):
    status: Optional[str] = Field(None, pattern="^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$")
    punch_in: Optional[int] = Field(None, strict=True, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(None, strict=True, ge=100000000000, le=4102444800000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    now_sec = int(datetime.now(timezone.utc).timestamp())
    created_dt = datetime.fromtimestamp(now_sec, tz=timezone.utc)
    doc = body.model_dump()
    doc["created_at"] = created_dt
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return format_employee_record(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [format_employee_record(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        ts_utc = ms_to_dt(body.punched_at)
    else:
        now_sec = int(datetime.now(timezone.utc).timestamp())
        ts_utc = datetime.fromtimestamp(now_sec, tz=timezone.utc)

    ts_ist = ts_utc.astimezone(IST)
    att_date = get_attendance_date(ts_ist, emp["shift_start"], emp["shift_end"])
    late_minutes = compute_late_minutes(ts_ist, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": ts_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")

    return format_attendance_record(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must not be after date_to")

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )
    items = [format_attendance_record(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        ts_utc = ms_to_dt(body.punched_at)
    else:
        now_sec = int(datetime.now(timezone.utc).timestamp())
        ts_utc = datetime.fromtimestamp(now_sec, tz=timezone.utc)

    # Find the employee's most recent attendance record with a punch_in
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None}},
        sort=[("punch_in", -1)],
    )

    if not record:
        raise HTTPException(status_code=404, detail="No punch-in record found")

    # 422 if punched_at <= punch_in
    if ts_utc <= record["punch_in"]:
        raise HTTPException(status_code=422, detail="punched_at must be strictly after punch_in")

    # 409 if already punched out
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Record already punched out")

    # 422 if > 24 hours after punch_in
    diff_sec = int((ts_utc - record["punch_in"]).total_seconds())
    if diff_sec > 24 * 3600:
        raise HTTPException(status_code=422, detail="punched_at must be within 24 hours of punch_in")

    work_hours = compute_work_hours(record["punch_in"], ts_utc)
    ts_ist = ts_utc.astimezone(IST)
    overtime_min = compute_overtime(ts_ist, emp["shift_end"], emp["shift_start"], record["date"])
    half_day = work_hours < 4.50

    # Atomic update guards against concurrent punch-outs
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": ts_utc,
                "work_hours": work_hours,
                "overtime_minutes": overtime_min,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(status_code=409, detail="Record already punched out")

    return format_attendance_record(updated)


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    target_status = body.status if body.status is not None else record["status"]

    if target_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(status_code=422, detail="Cannot supply punch times for ABSENT or LEAVE")
        target_punch_in = None
        target_punch_out = None
        target_work_hours = None
        target_late_minutes = 0
        target_overtime_minutes = 0
        target_half_day = False
    else:
        if body.punch_in is not None:
            target_punch_in = ms_to_dt(body.punch_in)
        else:
            target_punch_in = record.get("punch_in")

        if target_punch_in is None:
            raise HTTPException(status_code=422, detail="Presence status requires punch_in")

        punch_in_ist = target_punch_in.astimezone(IST)
        if get_attendance_date(punch_in_ist, emp["shift_start"], emp["shift_end"]) != date:
            raise HTTPException(status_code=422, detail="punch_in does not match attendance date")

        if body.punch_out is not None:
            target_punch_out = ms_to_dt(body.punch_out)
        else:
            target_punch_out = record.get("punch_out")

        target_late_minutes = compute_late_minutes(punch_in_ist, emp["shift_start"], date)
        if target_punch_out is not None:
            diff_sec = int((target_punch_out - target_punch_in).total_seconds())
            if diff_sec <= 0:
                raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
            if diff_sec > 24 * 3600:
                raise HTTPException(status_code=422, detail="punch_out must be within 24h of punch_in")
            target_work_hours = compute_work_hours(target_punch_in, target_punch_out)
            punch_out_ist = target_punch_out.astimezone(IST)
            target_overtime_minutes = compute_overtime(punch_out_ist, emp["shift_end"], emp["shift_start"], date)
            target_half_day = target_work_hours < 4.50
        else:
            target_work_hours = None
            target_overtime_minutes = 0
            target_half_day = False

    old_fields = {
        "status": record["status"],
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": record.get("overtime_minutes", 0),
        "half_day": record.get("half_day", False),
    }
    new_fields = {
        "status": target_status,
        "punch_in": target_punch_in,
        "punch_out": target_punch_out,
        "work_hours": target_work_hours,
        "late_minutes": target_late_minutes,
        "overtime_minutes": target_overtime_minutes,
        "half_day": target_half_day,
    }

    changes = {}
    for key, new_val in new_fields.items():
        old_val = old_fields[key]
        if old_val != new_val:
            changes[key] = {"from": old_val, "to": new_val}

    if not changes:
        raise HTTPException(status_code=422, detail="Request changes nothing")

    now_sec = int(datetime.now(timezone.utc).timestamp())
    now_utc = datetime.fromtimestamp(now_sec, tz=timezone.utc)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    cur_history_len = len(record.get("history", []))
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "history": {"$size": cur_history_len},  # optimistic concurrency lock
        },
        {
            "$set": new_fields,
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status_code=409, detail="Concurrent regularization conflict")

    return format_attendance_record(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    year, m = map(int, month.split("-"))
    last_day = calendar.monthrange(year, m)[1]
    d_start = date(year, m, 1)
    d_end = date(year, m, last_day)

    # R7: Mon-Fri working days, counted from joined_on for mid-month joiners
    emp_joined = date.fromisoformat(emp["joined_on"])
    actual_start = max(d_start, emp_joined)
    working_days = 0
    if actual_start <= d_end:
        cur = actual_start
        while cur <= d_end:
            if cur.weekday() < 5:  # Mon-Fri
                working_days += 1
            cur += timedelta(days=1)

    pipeline = build_employee_monthly_pipeline(emp_code, month, last_day)
    agg_res = list(db.attendance_logs.aggregate(pipeline))
    if agg_res:
        row = agg_res[0]
        present_days = float(row.get("present_days", 0.0))
        leave_days = int(row.get("leave_days", 0))
        late_count = int(row.get("late_count", 0))
        total_late_minutes = int(row.get("total_late_minutes", 0))
        total_overtime_minutes = int(row.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    if working_days > 0:
        attendance_pct = round_half_up((present_days / working_days) * 100, 2)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up(present_days, 2),
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    year, m = map(int, month.split("-"))
    last_day = calendar.monthrange(year, m)[1]

    pipeline = build_department_summary_pipeline(month, last_day, department)
    results = list(db.employees.aggregate(pipeline))
    items = []
    for r in results:
        count = r.get("work_hours_count", 0)
        avg_wh = round_half_up(r.get("work_hours_sum", 0.0) / count, 2) if count > 0 else None
        items.append({
            "department": r["_id"],
            "headcount": r["headcount"],
            "present_days": round_half_up(r.get("present_days", 0.0), 2),
            "avg_work_hours": avg_wh,
            "late_count": r.get("late_count", 0),
            "total_late_minutes": r.get("total_late_minutes", 0),
            "leave_count": r.get("leave_count", 0),
            "on_duty_count": r.get("on_duty_count", 0),
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    year, m = map(int, month.split("-"))
    last_day = calendar.monthrange(year, m)[1]

    pipeline = build_late_leaderboard_pipeline(month, last_day, limit, department)
    results = list(db.attendance_logs.aggregate(pipeline))
    items = [
        {
            "rank": r["rank"],
            "emp_code": r["emp_code"],
            "name": r["name"],
            "department": r["department"],
            "total_late_minutes": r["total_late_minutes"],
            "late_count": r["late_count"],
        }
        for r in results
    ]
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    dept_emp_count = db.employees.count_documents({"department": department})
    if dept_emp_count == 0:
        raise HTTPException(status_code=404, detail="Department not found")

    try:
        d_from = date.fromisoformat(from_date)
        d_to = date.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date format")

    if d_to < d_from:
        raise HTTPException(status_code=422, detail="'to' date must not be before 'from' date")

    days_count = (d_to - d_from).days + 1
    if days_count > 92:
        raise HTTPException(status_code=422, detail="Date range must not exceed 92 days")

    pipeline = build_department_trend_pipeline(department, d_from, d_to)
    rows = list(db.employees.aggregate(pipeline))
    items = []
    for r in rows:
        att_rate = round_half_up(r.get("attendance_rate"), 4)
        m_avg = round_half_up(r.get("moving_avg_7d"), 4)
        items.append({
            "date": r["date"],
            "is_working_day": r["is_working_day"],
            "headcount": r["headcount"],
            "present_count": round_half_up(r.get("present_count", 0.0), 2),
            "late_count": r.get("late_count", 0),
            "attendance_rate": att_rate,
            "moving_avg_7d": m_avg,
        })

    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(...),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must not be after date_to")
        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status
        skip = (page - 1) * page_size
        cmd = SON([
            ("explain", SON([
                ("find", "attendance_logs"),
                ("filter", q),
                ("sort", SON([("date", -1), ("emp_code", 1)])),
                ("skip", skip),
                ("limit", page_size),
            ])),
            ("verbosity", "executionStats"),
        ])
        raw_explain = db.command(cmd)
        clean_explain = json.loads(json_util.dumps(raw_explain))
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": clean_explain,
        }

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        year, m = map(int, month.split("-"))
        last_day = calendar.monthrange(year, m)[1]
        pipeline = build_employee_monthly_pipeline(emp_code, month, last_day)
        cmd = SON([
            ("explain", SON([
                ("aggregate", "attendance_logs"),
                ("pipeline", pipeline),
                ("cursor", {}),
            ])),
            ("verbosity", "executionStats"),
        ])
        raw_explain = db.command(cmd)
        clean_explain = json.loads(json_util.dumps(raw_explain))
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": clean_explain,
        }

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        year, m = map(int, month.split("-"))
        last_day = calendar.monthrange(year, m)[1]
        pipeline = build_department_summary_pipeline(month, last_day, department)
        cmd = SON([
            ("explain", SON([
                ("aggregate", "employees"),
                ("pipeline", pipeline),
                ("cursor", {}),
            ])),
            ("verbosity", "executionStats"),
        ])
        raw_explain = db.command(cmd)
        clean_explain = json.loads(json_util.dumps(raw_explain))
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": clean_explain,
        }

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        year, m = map(int, month.split("-"))
        last_day = calendar.monthrange(year, m)[1]
        pipeline = build_late_leaderboard_pipeline(month, last_day, limit, department)
        cmd = SON([
            ("explain", SON([
                ("aggregate", "attendance_logs"),
                ("pipeline", pipeline),
                ("cursor", {}),
            ])),
            ("verbosity", "executionStats"),
        ])
        raw_explain = db.command(cmd)
        clean_explain = json.loads(json_util.dumps(raw_explain))
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": clean_explain,
        }

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(status_code=422, detail="department, from, and to are required")
        try:
            d_from = date.fromisoformat(from_date)
            d_to = date.fromisoformat(to_date)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid date format")

        if d_to < d_from:
            raise HTTPException(status_code=422, detail="'to' date must not be before 'from' date")
        if (d_to - d_from).days + 1 > 92:
            raise HTTPException(status_code=422, detail="Date range must not exceed 92 days")

        pipeline = build_department_trend_pipeline(department, d_from, d_to)
        cmd = SON([
            ("explain", SON([
                ("aggregate", "employees"),
                ("pipeline", pipeline),
                ("cursor", {}),
            ])),
            ("verbosity", "executionStats"),
        ])
        raw_explain = db.command(cmd)
        clean_explain = json.loads(json_util.dumps(raw_explain))
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": clean_explain,
        }

    else:
        raise HTTPException(status_code=422, detail="Invalid endpoint")