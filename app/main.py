import calendar
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError
from pydantic import BaseModel, Field, StrictInt, field_validator, AliasChoices

# IST is UTC+5:30 — all attendance dates are resolved in this timezone
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1)

# Employees get a 10-minute grace window before they're marked late
GRACE_SECONDS = 600

# Sanity bounds for epoch-millisecond timestamps (roughly year 1973 to 5138)
MS_MIN, MS_MAX = 10**11, 10**14


# Time helpers

def trunc_ms(ms: int) -> int:
    """Drop sub-second precision so punch times are always whole seconds."""
    return ms - ms % 1000


def utc_naive_to_ms(dt: datetime) -> int:
    """Turn a naive UTC datetime into an epoch-millisecond integer."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    delta = dt - EPOCH
    return (delta.days * 86400 + delta.seconds) * 1000


def now_ms() -> int:
    """Return the current time as epoch milliseconds, rounded to the second."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    return utc_naive_to_ms(now)


def ms_to_utc_naive(ms: int) -> datetime:
    """Convert epoch milliseconds back to a naive UTC datetime for MongoDB storage."""
    return EPOCH + timedelta(seconds=ms // 1000)


def ms_to_ist(ms: int) -> datetime:
    """Convert epoch milliseconds to an IST-aware datetime."""
    return ms_to_utc_naive(ms).replace(tzinfo=timezone.utc).astimezone(IST)


def utc_naive_to_ist(dt: datetime) -> datetime:
    """Take a naive UTC datetime from MongoDB and convert it to IST."""
    return dt.replace(tzinfo=timezone.utc).astimezone(IST)


# Shift / attendance-date helpers

def _hhmm(value: str) -> tuple[int, int]:
    hour, minute = value.split(":")
    return int(hour), int(minute)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    """Return True when a shift crosses midnight (e.g. 22:00 → 06:00)."""
    return _hhmm(shift_end) <= _hhmm(shift_start)


def ist_on(day: str, hhmm: str, day_offset: int = 0) -> datetime:
    """Build an IST-aware datetime for a given date and HH:MM time string."""
    target_date = date.fromisoformat(day) + timedelta(days=day_offset)
    hour, minute = _hhmm(hhmm)
    return datetime(target_date.year, target_date.month, target_date.day, hour, minute, tzinfo=IST)


def resolve_record_date(punch_ist: datetime, shift_start: str, shift_end: str) -> str:
    """
    Figure out which calendar date this punch belongs to.

    For overnight shifts, a punch in the early-morning window (after midnight
    but before shift_end) still belongs to the previous day.
    """
    attendance_date = punch_ist.date()

    if is_overnight(shift_start, shift_end):
        punch_time = (punch_ist.hour, punch_ist.minute, punch_ist.second)
        if punch_time < (*_hhmm(shift_end), 0):
            attendance_date -= timedelta(days=1)

    return attendance_date.isoformat()


def compute_late_minutes(punch_ist: datetime, attendance_date: str, shift_start: str) -> int:
    """
    How many minutes late is this punch?

    Everyone gets a 10-minute grace period. Arrive within that window and
    we return 0 — they're considered on time.
    """
    shift_start_ist = ist_on(attendance_date, shift_start)
    elapsed_seconds = (punch_ist - shift_start_ist).total_seconds()

    if elapsed_seconds <= GRACE_SECONDS:
        return 0

    return int(elapsed_seconds // 60)



def attendance_out(doc: dict) -> dict:
    """Shape an attendance log document into the public API response."""
    def to_ms(value):
        return utc_naive_to_ms(value) if value is not None else None

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms(doc.get("punch_in")),
        "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0) or 0,
        "overtime_minutes": doc.get("overtime_minutes", 0) or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": doc.get("history", []) or [],
    }


def employee_out(doc: dict) -> dict:
    """Shape an employee document into the public API response."""
    created_at = doc.get("created_at")
    created_at_ms = None

    if created_at is not None:
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        created_at_ms = int(created_at.timestamp() * 1000)

    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": created_at_ms,
    }


def month_bounds(month: str) -> tuple[str, str]:
    year, month_number = map(int, month.split("-"))
    first = date(year, month_number, 1)
    last = date(year, month_number, calendar.monthrange(year, month_number)[1])
    return first.isoformat(), last.isoformat()


def current_month() -> str:
    return datetime.now(IST).strftime("%Y-%m")


def ensure_date(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=f"{field_name} must use YYYY-MM-DD format",
        )
    if parsed.isoformat() != value:
        raise HTTPException(
            status_code=422,
            detail=f"{field_name} must use YYYY-MM-DD format",
        )
    return value


def working_days_in_month(month: str, joined_on: str) -> int:
    first_str, last_str = month_bounds(month)
    first = max(date.fromisoformat(first_str), date.fromisoformat(joined_on))
    last = date.fromisoformat(last_str)
    if first > last:
        return 0
    return sum(
        (first + timedelta(days=offset)).weekday() < 5
        for offset in range((last - first).days + 1)
    )


def round_half_up(value: int | float | Decimal, places: int) -> float:
    quantum = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def _weekday_of(date_str_expr):
    return {
        "$dayOfWeek": {
            "$dateFromString": {
                "dateString": date_str_expr,
                "format": "%Y-%m-%d",
            }
        }
    }


def _is_weekday(date_str_expr):
    # MongoDB: Sunday=1, Monday=2, ..., Friday=6, Saturday=7
    return {"$in": [_weekday_of(date_str_expr), [2, 3, 4, 5, 6]]}


def employee_monthly_pipeline(emp_code: str, month: str) -> list[dict]:
    first, last = month_bounds(month)

    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": first, "$lte": last},
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
                                    _is_weekday("$date"),
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": [{"$ifNull": ["$half_day", False]}, True]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$status", "LEAVE"]},
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
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


def department_summary_pipeline(month: str) -> list[dict]:
    """Aggregate monthly attendance by department, including employees with no logs."""
    first, last = month_bounds(month)

    return [
        {"$match": {"joined_on": {"$lte": last}}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": first, "$lte": last}}},
                    {
                        "$group": {
                            "_id": None,
                            "present_days": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {
                                                    "$in": [
                                                        "$status",
                                                        ["PRESENT", "WFH", "ON_DUTY"],
                                                    ]
                                                },
                                                _is_weekday("$date"),
                                            ]
                                        },
                                        {
                                            "$cond": [
                                                {
                                                    "$eq": [
                                                        {"$ifNull": ["$half_day", False]},
                                                        True,
                                                    ]
                                                },
                                                0.5,
                                                1,
                                            ]
                                        },
                                        0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$gt": [
                                                {"$ifNull": ["$late_minutes", 0]},
                                                0,
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "total_late_minutes": {
                                "$sum": {"$ifNull": ["$late_minutes", 0]}
                            },
                            "leave_count": {
                                "$sum": {
                                    "$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]
                                }
                            },
                            "on_duty_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$eq": ["$status", "ON_DUTY"]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "work_hours_sum": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {
                                                    "$in": [
                                                        "$status",
                                                        ["PRESENT", "WFH", "ON_DUTY"],
                                                    ]
                                                },
                                                {
                                                    "$ne": [
                                                        {"$ifNull": ["$work_hours", None]},
                                                        None,
                                                    ]
                                                },
                                            ]
                                        },
                                        "$work_hours",
                                        0,
                                    ]
                                }
                            },
                            "work_hours_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {
                                                    "$in": [
                                                        "$status",
                                                        ["PRESENT", "WFH", "ON_DUTY"],
                                                    ]
                                                },
                                                {
                                                    "$ne": [
                                                        {"$ifNull": ["$work_hours", None]},
                                                        None,
                                                    ]
                                                },
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
                "as": "monthly",
            }
        },
        {
            "$unwind": {
                "path": "$monthly",
                "preserveNullAndEmptyArrays": True,
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {
                    "$sum": {"$ifNull": ["$monthly.present_days", 0]}
                },
                "late_count": {
                    "$sum": {"$ifNull": ["$monthly.late_count", 0]}
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$monthly.total_late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {"$ifNull": ["$monthly.leave_count", 0]}
                },
                "on_duty_count": {
                    "$sum": {"$ifNull": ["$monthly.on_duty_count", 0]}
                },
                "work_hours_sum": {
                    "$sum": {"$ifNull": ["$monthly.work_hours_sum", 0]}
                },
                "work_hours_count": {
                    "$sum": {"$ifNull": ["$monthly.work_hours_count", 0]}
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
                "work_hours_sum": 1,
                "work_hours_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]



# Database setup
load_dotenv(override=False)

MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB = os.getenv("MONGO_DB")

if not MONGO_URI or not MONGO_DB:
    raise RuntimeError("MONGO_URI and MONGO_DB must be configured.")

client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
db = client[MONGO_DB]

employees = db["employees"]
attendance_logs = db["attendance_logs"]


def create_indexes():
    """Set up all the indexes the API relies on. Safe to call on every startup."""

    # Unique employee codes — prevents accidental duplicates
    employees.create_index([("emp_code", ASCENDING)], unique=True, name="ux_employees_emp_code")

    # Speeds up department-filtered employee listings
    employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)],
        name="ix_employees_department_emp_code",
    )

    # Used for monthly headcount and joining-date range queries
    employees.create_index([("joined_on", ASCENDING)], name="ix_employees_joined_on")

    # Enforces one attendance record per employee per day
    attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="ux_logs_emp_code_date",
    )

    # Default sort order for attendance listings (newest first)
    attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="ix_logs_date_desc_emp_code",
    )

    # Supports filtering by status (PRESENT, WFH, etc.)
    attendance_logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)],
        name="ix_logs_status_date_emp_code",
    )

    # Powers the late-comer leaderboard
    attendance_logs.create_index(
        [("late_minutes", DESCENDING), ("emp_code", ASCENDING)],
        name="ix_logs_late_minutes_emp_code",
    )


# Request models

class EmployeeIn(BaseModel):

    emp_code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    email: str = Field(min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=200)
    shift_start: str = Field(default="09:30", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("emp_code", "name", "email", "department")
    @classmethod
    def reject_blank_strings(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Field cannot be blank")
        return value.strip()

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value: str) -> str:
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            raise ValueError("joined_on must be a valid YYYY-MM-DD date")

        if parsed.isoformat() != value:
            raise ValueError("joined_on must use YYYY-MM-DD format")

        return value


class PunchInIn(BaseModel):

    emp_code: str = Field(min_length=1, max_length=64)
    punched_at: StrictInt | None = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, value):
        if value is not None and not (MS_MIN <= value <= MS_MAX):
            raise ValueError("punched_at must be an integer epoch timestamp in milliseconds")
        return value


class PunchOutIn(BaseModel):
    emp_code: str = Field(min_length=1, max_length=64)
    punched_at: StrictInt | None = None

    @field_validator("punched_at")
    @classmethod
    def validate_timestamp(cls, value):
        if value is not None and not (10**11 <= value <= 10**14):
            raise ValueError("punched_at must be epoch milliseconds")
        return value


class AttendancePatch(BaseModel):
    status: Optional[
        Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]
    ] = None

    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None

    reason: str = Field(min_length=1, max_length=1000)
    corrected_by: str = Field(
        min_length=1,
        max_length=200,
        validation_alias=AliasChoices("corrected_by", "by"),
    )

    @field_validator("punch_in", "punch_out")
    @classmethod
    def validate_timestamp(cls, value):
        return _epoch_ms(value)

# App bootstrap


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Make sure the database is reachable before we start accepting requests
    client.admin.command("ping")
    create_indexes()

    yield

    client.close()


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="1.0.0",
    lifespan=lifespan,
)


# Routes


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    employee = employees.find_one(
        {"emp_code": emp_code},
        {"_id": 0, "joined_on": 1},
    )
    if employee is None:
        raise HTTPException(404, f"unknown emp_code: {emp_code}")

    row = next(
        attendance_logs.aggregate(
            employee_monthly_pipeline(emp_code, month),
            hint="ux_logs_emp_code_date",
        ),
        None,
    )

    # An employee may have no attendance records during this month.
    if row is None:
        row = {
            "present_days": 0,
            "leave_days": 0,
            "late_count": 0,
            "total_late_minutes": 0,
            "total_overtime_minutes": 0,
        }

    working_days = working_days_in_month(month, employee["joined_on"])
    present_days = round_half_up(row["present_days"], 2)
    attendance_pct = (
        round_half_up(Decimal(str(present_days)) * 100 / working_days, 4)
        if working_days
        else None
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": int(row["leave_days"]),
        "late_count": int(row["late_count"]),
        "total_late_minutes": int(row["total_late_minutes"]),
        "total_overtime_minutes": int(row["total_overtime_minutes"]),
        "attendance_pct": attendance_pct,
    }


@app.get("/")
def root():
    return {"message": "Employee Attendance API is running"}


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    employee = body.model_dump()
    employee["created_at"] = datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)

    try:
        employees.insert_one(employee)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail=f"emp_code already exists: {body.emp_code}")

    return employee_out(employee)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    employee = employees.find_one(
        {"emp_code": body.emp_code},
        {"_id": 0, "shift_start": 1, "shift_end": 1},
    )

    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    shift_start = employee.get("shift_start") or "09:30"
    shift_end = employee.get("shift_end") or "18:30"

    timestamp_ms = (
        trunc_ms(body.punched_at)
        if body.punched_at is not None
        else now_ms()
    )

    punch_out_ist = ms_to_ist(timestamp_ms)
    punch_out_utc = ms_to_utc_naive(timestamp_ms)

    # Find today's attendance record using the punch-in-derived date.
    punch_in_record = attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_out": None}
    )

    if punch_in_record is None:
        existing = attendance_logs.find_one(
            {"emp_code": body.emp_code},
            sort=[("date", -1)],
        )

        if existing is None:
            raise HTTPException(
                status_code=404,
                detail="No attendance record found",
            )

        raise HTTPException(
            status_code=409,
            detail="Attendance record already punched out",
        )

    record_date = punch_in_record["date"]
    punch_in_ist = utc_naive_to_ist(punch_in_record["punch_in"])

    if timestamp_ms < utc_naive_to_ms(punch_in_record["punch_in"]):
        raise HTTPException(
            status_code=422,
            detail="Punch-out cannot be earlier than punch-in",
        )

    work_hours = compute_work_hours(punch_in_ist, punch_out_ist)
    overtime_minutes = compute_overtime_minutes(
        punch_out_ist,
        record_date,
        shift_start,
        shift_end,
    )
    half_day = work_hours < 4.50

    # Atomic compare-and-set: only one concurrent request can change null
    # punch_out to a timestamp.
    updated = attendance_logs.find_one_and_update(
        {
            "_id": punch_in_record["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": punch_out_utc,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if updated is None:
        raise HTTPException(
            status_code=409,
            detail="Attendance record already punched out",
        )

    return attendance_out(updated)



def department_trend_pipeline(
    department: str,
    date_from: str,
    date_to: str,
    window: int,
    seed_id: Any,
) -> list[dict]:
    num_days = (
        date.fromisoformat(date_to) - date.fromisoformat(date_from)
    ).days + 1
    start_dt = datetime.fromisoformat(date_from)

    return [
        {"$match": {"department": department}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {
                        "$match": {
                            "date": {"$gte": date_from, "$lte": date_to}
                        }
                    },
                    {
                        "$project": {
                            "_id": 0,
                            "date": 1,
                            "status": 1,
                            "half_day": 1,
                            "late_minutes": 1,
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$match": {"logs.date": {"$exists": True}}},
        {
            "$group": {
                "_id": "$logs.date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$in": [
                                    "$logs.status",
                                    ["PRESENT", "WFH", "ON_DUTY"],
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": [{"$ifNull": ["$logs.half_day", False]}, True]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$_id",
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$unionWith": {
                "coll": "employees",
                "pipeline": [
                    {"$match": {"_id": seed_id}},
                    {
                        "$project": {
                            "_id": 0,
                            "dates": {
                                "$map": {
                                    "input": {"$range": [0, num_days]},
                                    "as": "i",
                                    "in": {
                                        "$dateToString": {
                                            "format": "%Y-%m-%d",
                                            "date": {
                                                "$dateAdd": {
                                                    "startDate": start_dt,
                                                    "unit": "day",
                                                    "amount": "$$i",
                                                }
                                            },
                                        }
                                    },
                                }
                            },
                        }
                    },
                    {"$unwind": "$dates"},
                    {
                        "$project": {
                            "date": "$dates",
                            "present_count": {"$literal": 0},
                            "late_count": {"$literal": 0},
                        }
                    },
                ],
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {"$max": "$present_count"},
                "late_count": {"$max": "$late_count"},
            }
        },
        {"$sort": {"_id": 1}},
        {
            "$lookup": {
                "from": "employees",
                "let": {"trend_date": "$_id"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$trend_date"]},
                                ]
                            }
                        }
                    },
                    {"$count": "headcount"},
                ],
                "as": "active_employees",
            }
        },
        {
            "$addFields": {
                "is_working_day": {
                    "$in": [
                        {
                            "$dayOfWeek": {
                                "$dateFromString": {
                                    "dateString": "$_id",
                                    "format": "%Y-%m-%d",
                                }
                            }
                        },
                        [2, 3, 4, 5, 6],
                    ]
                },
                "headcount": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$active_employees.headcount", 0]},
                        0,
                    ]
                },
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"_id": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-window + 1, 0]},
                    },
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$_id",
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
    ]


def _default_range() -> tuple[str, str]:
    return month_bounds(current_month())


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    date_from: Optional[str] = Query(None, alias="from"),
    date_to: Optional[str] = Query(None, alias="to"),
    window: int = Query(7, ge=1, le=92),
):
    default_from, default_to = _default_range()
    start_date = ensure_date(date_from, "date_from") or default_from
    end_date = ensure_date(date_to, "date_to") or default_to

    if start_date > end_date:
        raise HTTPException(
            status_code=422,
            detail="date_from must be on or before date_to",
        )
    if (date.fromisoformat(end_date) - date.fromisoformat(start_date)).days + 1 > 92:
        raise HTTPException(
            status_code=422,
            detail="date range cannot exceed 92 days",
        )

    seed = employees.find_one(
        {"department": department},
        {"_id": 1},
    )
    if seed is None:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown department: {department}",
        )

    rows = employees.aggregate(
        department_trend_pipeline(
            department,
            start_date,
            end_date,
            window,
            seed["_id"],
        ),
        hint="ix_employees_department_emp_code",
        allowDiskUse=True,
    )

    items = []
    for row in rows:
        items.append({
            "date": row["date"],
            "is_working_day": row["is_working_day"],
            "headcount": int(row["headcount"]),
            "present_count": round_half_up(row["present_count"], 2),
            "late_count": int(row["late_count"]),
            "attendance_rate": (
                round_half_up(row["attendance_rate"], 4)
                if row["attendance_rate"] is not None
                else None
            ),
            "moving_avg_7d": (
                round_half_up(row["moving_avg_7d"], 4)
                if row["moving_avg_7d"] is not None
                else None
            ),
        })

    return {
        "department": department,
        "items": items,
    }

def _leaderboard_query(
    month: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
) -> dict:
    query: dict[str, Any] = {"late_minutes": {"$gt": 0}}

    if month is not None:
        first, last = month_bounds(month)
        query["date"] = {"$gte": first, "$lte": last}
    elif date_from is not None or date_to is not None:
        query["date"] = {}
        if date_from is not None:
            query["date"]["$gte"] = date_from
        if date_to is not None:
            query["date"]["$lte"] = date_to

    return query


def _leaderboard_hint(query: dict) -> str:
    return (
        "ix_logs_date_desc_emp_code"
        if "date" in query
        else "ix_logs_late_minutes_emp_code"
    )


def late_leaderboard_pipeline(
    limit: int,
    query: dict,
    department: str | None = None,
) -> list[dict]:
    pipeline = [
        {"$match": query},
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend([
        {
            "$group": {
                "_id": "$emp_code",
                "name": {"$first": "$employee.name"},
                "department": {"$first": "$employee.department"},
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "late_count": {"$sum": 1},
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": 1,
                "department": 1,
                "total_late_minutes": 1,
                "late_count": 1,
                "rank": 1,
            }
        },
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ])
    return pipeline

@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str | None = Query(
        default=None,
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
    ),
    limit: int = Query(10, ge=1, le=50),
    department: str | None = Query(default=None),
):
    selected_month = month or datetime.now(IST).strftime("%Y-%m")
    query = _leaderboard_query(selected_month, None, None)
    rows = attendance_logs.aggregate(
        late_leaderboard_pipeline(limit, query, department),
        hint=_leaderboard_hint(query),
        allowDiskUse=True,
    )

    return {
        "month": selected_month,
        "items": [
            {
                "name": row["name"],
                "department": row["department"],
                "emp_code": row["emp_code"],
                "total_late_minutes": int(row["total_late_minutes"]),
                "late_count": int(row["late_count"]),
                "rank": int(row["rank"]),
            }
            for row in rows
        ],
    }


@app.patch("/attendance/{emp_code}/{date}")
def patch_attendance(emp_code: str, date: str, body: AttendancePatch):
    day = ensure_date(date, "date")

    emp = employees.find_one(
        {"emp_code": emp_code},
        {"_id": 0, "shift_start": 1, "shift_end": 1},
    )
    if emp is None:
        raise HTTPException(404, f"unknown emp_code: {emp_code}")

    shift_start = emp.get("shift_start") or "09:30"
    shift_end = emp.get("shift_end") or "18:30"

    # Retry optimistic updates if another request modifies the record.
    for _ in range(6):
        doc = logs.find_one({"emp_code": emp_code, "date": day})
        if doc is None:
            raise HTTPException(
                404, f"no attendance record for {emp_code} on {day}"
            )

        old = {
            "status": doc.get("status"),
            "punch_in": doc.get("punch_in"),
            "punch_out": doc.get("punch_out"),
            "work_hours": doc.get("work_hours"),
            "late_minutes": doc.get("late_minutes") or 0,
            "overtime_minutes": doc.get("overtime_minutes") or 0,
            "half_day": bool(doc.get("half_day", False)),
        }

        new_status = body.status if body.status is not None else old["status"]

        if new_status in ("ABSENT", "LEAVE"):
            if body.punch_in is not None or body.punch_out is not None:
                raise HTTPException(
                    422, "ABSENT/LEAVE records cannot carry punch times"
                )
            new_punch_in = None
            new_punch_out = None
        else:
            new_punch_in = (
                ms_to_utc_naive(trunc_ms(body.punch_in))
                if body.punch_in is not None
                else old["punch_in"]
            )
            new_punch_out = (
                ms_to_utc_naive(trunc_ms(body.punch_out))
                if body.punch_out is not None
                else old["punch_out"]
            )

        if new_punch_in is None and new_punch_out is not None:
            raise HTTPException(422, "punch_out cannot be set without punch_in")

        if new_punch_in is not None and new_punch_out is not None:
            in_ist = utc_naive_to_ist(new_punch_in)
            out_ist = utc_naive_to_ist(new_punch_out)

            if out_ist < in_ist:
                raise HTTPException(422, "punch_out is before punch_in")

            work_hours = compute_work_hours(in_ist, out_ist)
            overtime = compute_overtime_minutes(
                out_ist, day, shift_start, shift_end
            )
            half_day = work_hours < HALF_DAY_THRESHOLD
        else:
            work_hours = None
            overtime = 0
            half_day = False

        late = (
            compute_late_minutes(
                utc_naive_to_ist(new_punch_in), day, shift_start
            )
            if new_punch_in is not None
            else 0
        )

        new = {
            "status": new_status,
            "punch_in": new_punch_in,
            "punch_out": new_punch_out,
            "work_hours": work_hours,
            "late_minutes": late,
            "overtime_minutes": overtime,
            "half_day": half_day,
        }

        changes = {
            field: {"from": old[field], "to": new[field]}
            for field in old
            if old[field] != new[field]
        }

        history_entry = {
            "at": now_utc_naive(),
            "by": body.corrected_by,
            "reason": body.reason,
            "changes": changes,
        }

        result = logs.update_one(
            {
                "_id": doc["_id"],
                "status": old["status"],
                "punch_in": old["punch_in"],
                "punch_out": old["punch_out"],
            },
            {
                "$set": new,
                "$push": {"history": history_entry},
            },
        )

        if result.matched_count == 1:
            updated = logs.find_one({"_id": doc["_id"]})
            return record_out(updated)

    raise HTTPException(
        409, "concurrent modification; please retry"
    )


@app.get("/employees")
def list_employees(
    department: str | None = Query(default=None, min_length=1, max_length=200),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    query = {}

    if department is not None:
        query["department"] = department

    # Pick the index that matches the query shape for best performance
    index_name = (
        "ix_employees_department_emp_code"
        if department is not None
        else "ux_employees_emp_code"
    )

    total = employees.count_documents(query, hint=index_name)

    cursor = (
        employees.find(query, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
        .hint(index_name)
    )

    return {
        "items": [employee_out(doc) for doc in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    # Look up the employee so we know their shift times
    employee = employees.find_one(
        {"emp_code": body.emp_code},
        {"_id": 0, "shift_start": 1, "shift_end": 1},
    )

    if employee is None:
        raise HTTPException(status_code=404, detail=f"Unknown employee: {body.emp_code}")

    shift_start = employee.get("shift_start") or "09:30"
    shift_end = employee.get("shift_end") or "18:30"

    # Use the provided timestamp if given, otherwise punch them in right now
    timestamp_ms = trunc_ms(body.punched_at) if body.punched_at is not None else now_ms()
    punch_in_ist = ms_to_ist(timestamp_ms)

    # Work out which attendance date this punch belongs to and how late they are
    attendance_date = resolve_record_date(punch_in_ist, shift_start, shift_end)
    late_minutes = compute_late_minutes(punch_in_ist, attendance_date, shift_start)

    record = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": ms_to_utc_naive(timestamp_ms),
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    # The unique index on (emp_code, date) handles concurrent duplicate punches safely
    try:
        attendance_logs.insert_one(record)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Already punched in for this attendance date")

    return attendance_out(record)

@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: str | None = Query(default=None),
):
    _, last = month_bounds(month)
    employee_filter = {"joined_on": {"$lte": last}}
    if department is not None:
        employee_filter["department"] = department

    items = []
    results = employees.aggregate(
        [
            {"$match": employee_filter},
            *department_summary_pipeline(month)[1:],
        ],
        hint="ix_employees_joined_on",
        allowDiskUse=True,
    )

    for result in results:
        present_days = round_half_up(result["present_days"], 2)
        items.append({
            "department": result["department"],
            "headcount": int(result["headcount"]),
            "present_days": present_days,
            "avg_work_hours": (
                round_half_up(
                    Decimal(str(result["work_hours_sum"]))
                    / int(result["work_hours_count"]),
                    2,
                )
                if result["work_hours_count"]
                else None
            ),
            "late_count": int(result["late_count"]),
            "total_late_minutes": int(result["total_late_minutes"]),
            "leave_count": int(result["leave_count"]),
            "on_duty_count": int(result["on_duty_count"]),
        })

    return {"month": month, "items": items}


@app.get("/attendance")
def list_attendance(
    emp_code: str | None = Query(default=None, min_length=1, max_length=64),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    ALLOWED_STATUSES = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}

    # Validate any date strings before touching the database
    for value, field_name in ((date_from, "date_from"), (date_to, "date_to")):
        if value is not None:
            try:
                parsed = date.fromisoformat(value)
            except ValueError:
                raise HTTPException(status_code=422, detail=f"{field_name} must use YYYY-MM-DD format")
            if parsed.isoformat() != value:
                raise HTTPException(status_code=422, detail=f"{field_name} must use YYYY-MM-DD format")

    if status is not None and status not in ALLOWED_STATUSES:
        raise HTTPException(status_code=422, detail=f"status must be one of {sorted(ALLOWED_STATUSES)}")

    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from cannot be after date_to")

    query = {}

    if emp_code is not None:
        query["emp_code"] = emp_code

    if date_from is not None or date_to is not None:
        query["date"] = {}
        if date_from is not None:
            query["date"]["$gte"] = date_from
        if date_to is not None:
            query["date"]["$lte"] = date_to

    if status is not None:
        query["status"] = status

    # Choose the most selective index based on what filters are active
    if emp_code is not None:
        index_hint = "ux_logs_emp_code_date"
    elif status is not None:
        index_hint = "ix_logs_status_date_emp_code"
    else:
        index_hint = "ix_logs_date_desc_emp_code"

    total = attendance_logs.count_documents(query, hint=index_hint)

    records = (
        attendance_logs.find(query)
        .sort([("date", -1), ("emp_code", 1)])
        .skip((page - 1) * page_size)
        .limit(page_size)
        .hint(index_hint)
    )

    return {
        "items": [attendance_out(r) for r in records],
        "total": total,
        "page": page,
        "page_size": page_size,
    }
