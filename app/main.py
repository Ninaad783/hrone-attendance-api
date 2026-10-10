"""
Employee Attendance & Analytics API

Implementation of FastAPI + MongoDB attendance service meeting all contract specifications:
- Complete business rules R1 - R10
- Concurrency and race condition safety via unique indexes and atomic updates
- Audit trail on regularizations (PATCH)
- Pure MongoDB aggregation pipelines for analytics
- Index-backed queries verified via explain endpoint
"""
import calendar
import json
import os
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, time as dt_time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, status
from pydantic import BaseModel, Field, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]
admin_db = client.admin

IST = timezone(timedelta(hours=5, minutes=30))


def init_db_indexes():
    """Create all required indexes idempotently at startup."""
    # employees collection
    db.employees.create_index([("emp_code", 1)], unique=True)
    db.employees.create_index([("department", 1), ("joined_on", 1)])
    db.employees.create_index([("joined_on", 1)])

    # attendance_logs collection
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("date", -1)])
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    db.attendance_logs.create_index([("date", 1), ("late_minutes", 1), ("emp_code", 1)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        init_db_indexes()
    except Exception:
        pass
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Date & Time Helpers
# --------------------------------------------------------------------------- #
def parse_time_hhmm(t_str: str) -> dt_time:
    parts = t_str.split(":")
    return dt_time(hour=int(parts[0]), minute=int(parts[1]))


def truncate_ms_to_seconds(ms: int) -> int:
    return (ms // 1000) * 1000


def ms_to_utc_dt(epoch_ms: int) -> datetime:
    truncated_ms = truncate_ms_to_seconds(epoch_ms)
    return datetime.fromtimestamp(truncated_ms / 1000, tz=timezone.utc)


def dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def round_half_up(value: float, decimals: int) -> float:
    d = Decimal(str(value))
    target = Decimal("10") ** (-decimals)
    return float(d.quantize(target, rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------- #
# Business Rules Helpers (R1 - R5)
# --------------------------------------------------------------------------- #
def get_attendance_date(punch_in_dt: datetime, shift_start: str, shift_end: str) -> str:
    """R1: Determine the attendance calendar date (IST).

    Overnight shifts: a punch-in earlier than shift_end belongs to the shift that started the previous day.
    """
    dt_ist = punch_in_dt.astimezone(IST)
    is_overnight = shift_end <= shift_start
    shift_end_t = parse_time_hhmm(shift_end)
    if is_overnight and dt_ist.time() < shift_end_t:
        att_date = dt_ist.date() - timedelta(days=1)
    else:
        att_date = dt_ist.date()
    return att_date.isoformat()


def compute_late_minutes(punch_in_dt: datetime, att_date_str: str, shift_start: str) -> int:
    """R2: Grace period 10 minutes.

    Late only if strictly more than 10:00 after shift_start.
    Then late_minutes = whole minutes (floor) since shift_start.
    """
    dt_ist = punch_in_dt.astimezone(IST)
    att_date = date.fromisoformat(att_date_str)
    start_t = parse_time_hhmm(shift_start)
    shift_start_dt = datetime.combine(att_date, start_t, tzinfo=IST)
    elapsed_seconds = int((dt_ist - shift_start_dt).total_seconds())
    if elapsed_seconds > 600:
        return elapsed_seconds // 60
    return 0


def compute_overtime(punch_out_dt: datetime, att_date_str: str, shift_start: str, shift_end: str) -> int:
    """R3: Whole minutes (floor) from shift_end to punch_out, counted only if >= 30; otherwise 0."""
    dt_ist = punch_out_dt.astimezone(IST)
    att_date = date.fromisoformat(att_date_str)
    end_t = parse_time_hhmm(shift_end)
    is_overnight = shift_end <= shift_start
    if is_overnight:
        shift_end_dt = datetime.combine(att_date + timedelta(days=1), end_t, tzinfo=IST)
    else:
        shift_end_dt = datetime.combine(att_date, end_t, tzinfo=IST)
    diff_seconds = int((dt_ist - shift_end_dt).total_seconds())
    if diff_seconds > 0:
        minutes = diff_seconds // 60
        return minutes if minutes >= 30 else 0
    return 0


def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    """R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up."""
    seconds = int((punch_out_dt - punch_in_dt).total_seconds())
    hours_dec = Decimal(seconds) / Decimal(3600)
    rounded = hours_dec.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(rounded)


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: After punch-out, half_day is true when rounded work_hours < 4.50."""
    if work_hours is None:
        return False
    return work_hours < 4.50


# --------------------------------------------------------------------------- #
# Pydantic Schemas
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
    def validate_rules(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        try:
            date.fromisoformat(self.joined_on)
        except ValueError:
            raise ValueError("joined_on must be a valid date in YYYY-MM-DD format")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, strict=True, ge=100_000_000_000, le=4_102_444_800_000)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, strict=True, ge=100_000_000_000, le=4_102_444_800_000)


class RegularizeIn(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = Field(None, strict=True, ge=100_000_000_000, le=4_102_444_800_000)
    punch_out: Optional[int] = Field(None, strict=True, ge=100_000_000_000, le=4_102_444_800_000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Serialization Helpers
# --------------------------------------------------------------------------- #
def serialize_employee(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_epoch_ms(doc.get("created_at")),
    }


def serialize_history_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    raw_changes = entry.get("changes", {})
    serialized_changes = {}
    for key, change in raw_changes.items():
        if key in ("punch_in", "punch_out"):
            from_val = change.get("from")
            to_val = change.get("to")
            serialized_changes[key] = {
                "from": dt_to_epoch_ms(from_val) if isinstance(from_val, datetime) else from_val,
                "to": dt_to_epoch_ms(to_val) if isinstance(to_val, datetime) else to_val,
            }
        else:
            serialized_changes[key] = {
                "from": change.get("from"),
                "to": change.get("to"),
            }
    return {
        "at": dt_to_epoch_ms(entry.get("at")),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": serialized_changes,
    }


def serialize_attendance_record(doc: Dict[str, Any]) -> Dict[str, Any]:
    history = doc.get("history") or []
    serialized_history = [serialize_history_entry(h) for h in history]
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_epoch_ms(doc.get("punch_in")),
        "punch_out": dt_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": serialized_history,
    }


# --------------------------------------------------------------------------- #
# Aggregation Pipeline Builders (Shared with /admin/explain)
# --------------------------------------------------------------------------- #
def build_employee_monthly_pipeline(emp_code: str, month: str) -> List[Dict[str, Any]]:
    parts = month.split("-")
    year, mon = int(parts[0]), int(parts[1])
    num_days = calendar.monthrange(year, mon)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": month_start, "$lte": month_end},
            }
        },
        {
            "$project": {
                "date": 1,
                "status": 1,
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "half_day": {"$ifNull": ["$half_day", False]},
                "dow": {
                    "$dayOfWeek": {
                        "$dateFromString": {"dateString": "$date"}
                    }
                },
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
                                    {"$gt": ["$dow", 1]},
                                    {"$lt": ["$dow", 7]},
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]
                    }
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]


def build_department_summary_pipeline(month: str, department: Optional[str] = None) -> List[Dict[str, Any]]:
    parts = month.split("-")
    year, mon = int(parts[0]), int(parts[1])
    num_days = calendar.monthrange(year, mon)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    emp_match: Dict[str, Any] = {"joined_on": {"$lte": month_end}}
    if department is not None:
        emp_match["department"] = department

    return [
        {"$match": emp_match},
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
                                    {"$gte": ["$date", month_start]},
                                    {"$lte": ["$date", month_end]},
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "date": 1,
                            "status": 1,
                            "work_hours": 1,
                            "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                            "half_day": {"$ifNull": ["$half_day", False]},
                            "dow": {
                                "$dayOfWeek": {
                                    "$dateFromString": {"dateString": "$date"}
                                }
                            },
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "department": 1,
                "emp_code": 1,
                "emp_present_days": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": 0.0,
                        "in": {
                            "$add": [
                                "$$value",
                                {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$$this.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                {"$gt": ["$$this.dow", 1]},
                                                {"$lt": ["$$this.dow", 7]},
                                            ]
                                        },
                                        {"$cond": ["$$this.half_day", 0.5, 1.0]},
                                        0.0,
                                    ]
                                },
                            ]
                        },
                    }
                },
                "emp_work_hours": {
                    "$map": {
                        "input": {
                            "$filter": {
                                "input": "$logs",
                                "as": "l",
                                "cond": {
                                    "$and": [
                                        {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$ne": ["$$l.work_hours", None]},
                                    ]
                                },
                            }
                        },
                        "as": "item",
                        "in": "$$item.work_hours",
                    }
                },
                "emp_late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$gt": ["$$l.late_minutes", 0]},
                        }
                    }
                },
                "emp_late_minutes": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": 0,
                        "in": {"$add": ["$$value", "$$this.late_minutes"]},
                    }
                },
                "emp_leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "LEAVE"]},
                        }
                    }
                },
                "emp_on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "ON_DUTY"]},
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
                "work_hours_nested": {"$push": "$emp_work_hours"},
                "late_count": {"$sum": "$emp_late_count"},
                "total_late_minutes": {"$sum": "$emp_late_minutes"},
                "leave_count": {"$sum": "$emp_leave_count"},
                "on_duty_count": {"$sum": "$emp_on_duty_count"},
            }
        },
        {
            "$project": {
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "all_work_hours": {
                    "$reduce": {
                        "input": "$work_hours_nested",
                        "initialValue": [],
                        "in": {"$concatArrays": ["$$value", "$$this"]},
                    }
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {
            "$project": {
                "department": 1,
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": [{"$size": "$all_work_hours"}, 0]},
                        {"$round": [{"$avg": "$all_work_hours"}, 2]},
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]


def build_late_leaderboard_pipeline(
    month: str,
    limit: int = 10,
    department: Optional[str] = None,
) -> List[Dict[str, Any]]:
    parts = month.split("-")
    year, mon = int(parts[0]), int(parts[1])
    num_days = calendar.monthrange(year, mon)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": month_start, "$lte": month_end},
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
        {"$match": {"emp.0": {"$exists": True}}},
        {
            "$project": {
                "emp_code": "$_id",
                "name": {"$arrayElemAt": ["$emp.name", 0]},
                "department": {"$arrayElemAt": ["$emp.department", 0]},
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]

    if department:
        pipeline.append({"$match": {"department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                },
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": 1,
                "name": 1,
                "department": 1,
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ])
    return pipeline


def build_department_trend_pipeline(
    department: str,
    from_date_str: str,
    to_date_str: str,
    dept_emps: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    from_d = date.fromisoformat(from_date_str)
    to_d = date.fromisoformat(to_date_str)
    emp_codes = [e["emp_code"] for e in dept_emps]
    joined_on_list = [e["joined_on"] for e in dept_emps]

    from_dt = datetime.combine(from_d, dt_time(0, 0), tzinfo=timezone.utc)
    to_next_dt = datetime.combine(to_d + timedelta(days=1), dt_time(0, 0), tzinfo=timezone.utc)
    to_dt = datetime.combine(to_d, dt_time(0, 0), tzinfo=timezone.utc)

    return [
        {
            "$match": {
                "emp_code": {"$in": emp_codes},
                "date": {"$gte": from_date_str, "$lte": to_date_str},
            }
        },
        {
            "$project": {
                "date": 1,
                "status": 1,
                "half_day": {"$ifNull": ["$half_day", False]},
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "date_val": {"$dateFromString": {"dateString": "$date"}},
            }
        },
        {
            "$unionWith": {
                "pipeline": [
                    {
                        "$documents": [
                            {"date_val": from_dt, "is_dummy": True},
                            {"date_val": to_dt, "is_dummy": True},
                        ]
                    }
                ]
            }
        },
        {
            "$densify": {
                "field": "date_val",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_next_dt],
                },
            }
        },
        {
            "$project": {
                "date_str": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_val"}},
                "is_dummy": {"$ifNull": ["$is_dummy", False]},
                "status": 1,
                "half_day": 1,
                "late_minutes": 1,
            }
        },
        {
            "$group": {
                "_id": "$date_str",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$eq": ["$is_dummy", False]},
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$eq": ["$is_dummy", False]},
                                    {"$gt": ["$late_minutes", 0]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {"$sort": {"_id": 1}},
        {
            "$project": {
                "date": "$_id",
                "dow": {
                    "$dayOfWeek": {
                        "$dateFromString": {"dateString": "$_id"}
                    }
                },
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": joined_on_list,
                            "as": "j",
                            "cond": {"$lte": ["$$j", "$_id"]},
                        }
                    }
                },
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": {
                    "$and": [{"$gt": ["$dow", 1]}, {"$lt": ["$dow", 7]}]
                },
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$gt": ["$dow", 1]},
                                {"$lt": ["$dow", 7]},
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {
                            "$round": [
                                {"$divide": ["$present_count", "$headcount"]},
                                4,
                            ]
                        },
                        None,
                    ]
                },
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d_raw": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"]
                        },
                    }
                },
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
                "moving_avg_7d": {
                    "$cond": [
                        {"$ne": ["$moving_avg_7d_raw", None]},
                        {"$round": ["$moving_avg_7d_raw", 4]},
                        None,
                    ]
                },
            }
        },
        {"$sort": {"date": 1}},
    ]


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["system"])
def health():
    """Readiness probe. Returns 200 only if MongoDB answers ping, else 503."""
    try:
        admin_db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeIn):
    """Create an employee. Enforces unique emp_code via MongoDB unique index."""
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(409, "emp_code already exists")
    now_ms = truncate_ms_to_seconds(int(time.time() * 1000))
    created_at_dt = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
    doc = body.model_dump()
    doc["created_at"] = created_at_dt
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List employees sorted by emp_code ascending."""
    q: Dict[str, Any] = {}
    if department is not None:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [serialize_employee(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInIn):
    """Record a punch-in. Race-safe via unique compound index on (emp_code, date)."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    if body.punched_at is not None:
        punch_ms = truncate_ms_to_seconds(body.punched_at)
    else:
        punch_ms = truncate_ms_to_seconds(int(time.time() * 1000))

    punch_in_dt = datetime.fromtimestamp(punch_ms / 1000, tz=timezone.utc)
    att_date = get_attendance_date(punch_in_dt, emp["shift_start"], emp["shift_end"])
    late_mins = compute_late_minutes(punch_in_dt, att_date, emp["shift_start"])

    if db.attendance_logs.find_one({"emp_code": body.emp_code, "date": att_date}):
        raise HTTPException(409, "already punched in for this date")

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
        raise HTTPException(409, "already punched in for this date")

    return serialize_attendance_record(doc)


@app.post("/attendance/punch-out", tags=["attendance"])
def punch_out(body: PunchOutIn):
    """Record a punch-out. Closes most recent record where punch_in <= punched_at."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    if body.punched_at is not None:
        punch_ms = truncate_ms_to_seconds(body.punched_at)
    else:
        punch_ms = truncate_ms_to_seconds(int(time.time() * 1000))
    punch_out_dt = datetime.fromtimestamp(punch_ms / 1000, tz=timezone.utc)

    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_dt}},
        sort=[("punch_in", -1)],
    )
    if not record:
        raise HTTPException(404, "No punch-in found")

    if record.get("punch_out") is not None:
        raise HTTPException(409, "that record is already punched out")

    punch_in_dt = record["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)

    diff_sec = (punch_out_dt - punch_in_dt).total_seconds()
    if diff_sec <= 0 or diff_sec > 24 * 3600:
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours")

    work_hours = compute_work_hours(punch_in_dt, punch_out_dt)
    half_day = compute_half_day(work_hours)
    overtime_mins = compute_overtime(punch_out_dt, record["date"], emp["shift_start"], emp["shift_end"])

    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hours,
                "overtime_minutes": overtime_mins,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(409, "that record is already punched out")

    return serialize_attendance_record(updated)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List attendance records sorted by date descending, then emp_code ascending."""
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be greater than date_to")
    if date_from:
        try:
            date.fromisoformat(date_from)
        except ValueError:
            raise HTTPException(422, "Invalid date_from format")
    if date_to:
        try:
            date.fromisoformat(date_to)
        except ValueError:
            raise HTTPException(422, "Invalid date_to format")

    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if status:
        q["status"] = status
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )
    items = [serialize_attendance_record(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date_str}", tags=["attendance"])
def regularize_attendance(
    emp_code: str,
    date_str: str,
    body: RegularizeIn,
):
    """Regularize (manually correct) one day's record with audit history."""
    try:
        date.fromisoformat(date_str)
    except ValueError:
        raise HTTPException(422, "Invalid date format")

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_str})
    if not record:
        raise HTTPException(404, "Attendance record not found")

    old_status = record.get("status")
    new_status = body.status if body.status is not None else old_status

    # Rule: setting status ABSENT or LEAVE clears both punch times, so supplying punch_in or punch_out with it is 422
    if new_status in ("ABSENT", "LEAVE") and (body.punch_in is not None or body.punch_out is not None):
        raise HTTPException(422, "Cannot supply punch_in or punch_out when setting status to ABSENT or LEAVE")

    old_punch_in = record.get("punch_in")
    if old_punch_in and old_punch_in.tzinfo is None:
        old_punch_in = old_punch_in.replace(tzinfo=timezone.utc)

    old_punch_out = record.get("punch_out")
    if old_punch_out and old_punch_out.tzinfo is None:
        old_punch_out = old_punch_out.replace(tzinfo=timezone.utc)

    if new_status in ("ABSENT", "LEAVE"):
        new_punch_in = None
        new_punch_out = None
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        # Presence status requires punch_in
        if body.punch_in is not None:
            new_punch_in = ms_to_utc_dt(body.punch_in)
        else:
            new_punch_in = old_punch_in

        if new_punch_in is None:
            raise HTTPException(422, "Presence status requires a punch_in")

        # Rule: punch_in must stay on the record's attendance date (R1)
        calc_date = get_attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"])
        if calc_date != date_str:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")

        if body.punch_out is not None:
            new_punch_out = ms_to_utc_dt(body.punch_out)
        else:
            new_punch_out = old_punch_out

        if new_punch_out is not None:
            diff_sec = (new_punch_out - new_punch_in).total_seconds()
            if diff_sec <= 0 or diff_sec > 24 * 3600:
                raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_half_day = compute_half_day(new_work_hours)
            new_overtime_minutes = compute_overtime(
                new_punch_out, date_str, emp["shift_start"], emp["shift_end"]
            )
        else:
            new_work_hours = None
            new_half_day = False
            new_overtime_minutes = 0

        new_late_minutes = compute_late_minutes(new_punch_in, date_str, emp["shift_start"])

    # Determine changes
    changes: Dict[str, Any] = {}
    if new_status != old_status:
        changes["status"] = {"from": old_status, "to": new_status}

    if new_punch_in != old_punch_in:
        changes["punch_in"] = {"from": old_punch_in, "to": new_punch_in}

    if new_punch_out != old_punch_out:
        changes["punch_out"] = {"from": old_punch_out, "to": new_punch_out}

    old_work_hours = record.get("work_hours")
    if new_work_hours != old_work_hours:
        changes["work_hours"] = {"from": old_work_hours, "to": new_work_hours}

    old_late_minutes = record.get("late_minutes", 0)
    if new_late_minutes != old_late_minutes:
        changes["late_minutes"] = {"from": old_late_minutes, "to": new_late_minutes}

    old_overtime_minutes = record.get("overtime_minutes", 0)
    if new_overtime_minutes != old_overtime_minutes:
        changes["overtime_minutes"] = {"from": old_overtime_minutes, "to": new_overtime_minutes}

    old_half_day = record.get("half_day", False)
    if new_half_day != old_half_day:
        changes["half_day"] = {"from": old_half_day, "to": new_half_day}

    if not changes:
        raise HTTPException(422, "Request changes nothing")

    now_ms = truncate_ms_to_seconds(int(time.time() * 1000))
    history_at = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
    history_entry = {
        "at": history_at,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    if "history" in record and record["history"] is not None:
        history_filter = {"history": record["history"]}
    else:
        history_filter = {"$or": [{"history": {"$exists": False}}, {"history": []}]}

    update_filter = {"_id": record["_id"]}
    update_filter.update(history_filter)

    updated = db.attendance_logs.find_one_and_update(
        update_filter,
        {
            "$set": {
                "status": new_status,
                "punch_in": new_punch_in,
                "punch_out": new_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(409, "Concurrent modification detected")

    return serialize_attendance_record(updated)


@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    """Monthly attendance summary for one employee backed by MongoDB aggregation."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    parts = month.split("-")
    year, mon = int(parts[0]), int(parts[1])
    num_days = calendar.monthrange(year, mon)[1]
    first_day = date(year, mon, 1)
    last_day = date(year, mon, num_days)
    emp_joined = date.fromisoformat(emp["joined_on"])

    if emp_joined > last_day:
        working_days = 0
    else:
        start_day = max(first_day, emp_joined)
        working_days = sum(
            1 for d in range((last_day - start_day).days + 1)
            if (start_day + timedelta(days=d)).weekday() < 5
        )

    pipeline = build_employee_monthly_pipeline(emp_code, month)
    res = list(db.attendance_logs.aggregate(pipeline))

    if res:
        data = res[0]
        present_days = float(data.get("present_days", 0.0))
        leave_days = int(data.get("leave_days", 0))
        late_count = int(data.get("late_count", 0))
        total_late_minutes = int(data.get("total_late_minutes", 0))
        total_overtime_minutes = int(data.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    if working_days > 0:
        raw_pct = (present_days / working_days) * 100
        attendance_pct = round_half_up(raw_pct, 2)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    """Department summary for a month via MongoDB aggregation across both collections."""
    pipeline = build_department_summary_pipeline(month, department)
    items = list(db.employees.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    """Late-comers leaderboard for a month with standard competition ranking."""
    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str,
    from_: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    """Daily attendance trend for a department with gap-filling ($densify) and 7-day moving average."""
    dept_emps = list(db.employees.find({"department": department}, {"emp_code": 1, "joined_on": 1}))
    if not dept_emps:
        raise HTTPException(404, "Department not found")

    try:
        from_d = date.fromisoformat(from_)
        to_d = date.fromisoformat(to)
    except ValueError:
        raise HTTPException(422, "Invalid date format")

    if to_d < from_d:
        raise HTTPException(422, "to must not be earlier than from")

    if (to_d - from_d).days + 1 > 92:
        raise HTTPException(422, "Date range exceeds 92 days")

    pipeline = build_department_trend_pipeline(department, from_, to, dept_emps)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: Optional[int] = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: Optional[int] = Query(1, ge=1),
    page_size: Optional[int] = Query(20, ge=1, le=100),
):
    """Runs MongoDB explain with executionStats verbosity for an endpoint's main query."""
    if endpoint == "attendance_list":
        q: Dict[str, Any] = {}
        if emp_code:
            q["emp_code"] = emp_code
        if status:
            q["status"] = status
        d_from = from_ or date_from
        d_to = to or date_to
        if d_from or d_to:
            q["date"] = {}
            if d_from:
                q["date"]["$gte"] = d_from
            if d_to:
                q["date"]["$lte"] = d_to
        p = page or 1
        ps = page_size or 20
        skip = (p - 1) * ps
        cmd = {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": skip,
            "limit": ps,
        }
        raw_explain = db.command("explain", cmd, verbosity="executionStats")
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": json.loads(json_util.dumps(raw_explain)),
        }

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required for employee_monthly")
        pipeline = build_employee_monthly_pipeline(emp_code, month)
        cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }
        raw_explain = db.command("explain", cmd, verbosity="executionStats")
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": json.loads(json_util.dumps(raw_explain)),
        }

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required for department_summary")
        pipeline = build_department_summary_pipeline(month, department)
        cmd = {
            "aggregate": "employees",
            "pipeline": pipeline,
            "cursor": {},
        }
        raw_explain = db.command("explain", cmd, verbosity="executionStats")
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": json.loads(json_util.dumps(raw_explain)),
        }

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required for late_leaderboard")
        pipeline = build_late_leaderboard_pipeline(month, limit or 10, department)
        cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }
        raw_explain = db.command("explain", cmd, verbosity="executionStats")
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": json.loads(json_util.dumps(raw_explain)),
        }

    elif endpoint == "department_trend":
        d_from = from_ or date_from
        d_to = to or date_to
        if not department or not d_from or not d_to:
            raise HTTPException(422, "department, from, and to are required for department_trend")
        dept_emps = list(db.employees.find({"department": department}, {"emp_code": 1, "joined_on": 1}))
        pipeline = build_department_trend_pipeline(department, d_from, d_to, dept_emps)
        cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }
        raw_explain = db.command("explain", cmd, verbosity="executionStats")
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": json.loads(json_util.dumps(raw_explain)),
        }
