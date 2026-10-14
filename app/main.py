import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import Body, FastAPI, HTTPException, Query
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
from pydantic import BaseModel, Field, StrictInt, field_validator

# IST is UTC+5:30 
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1)

# Employees get a 10-minute grace window before they're marked late
GRACE_SECONDS = 600

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



# Shift/attendance-date helpers

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

    return datetime(
        target_date.year,
        target_date.month,
        target_date.day,
        hour,
        minute,
        tzinfo=IST,
    )


def resolve_record_date(
    punch_ist: datetime,
    shift_start: str,
    shift_end: str,
) -> str:
    """
    Figure out which calendar date this punch belongs to.

    For overnight shifts a punch that lands in the early-morning window
    (after midnight but before shift_end) still belongs to the previous day.
    """
    attendance_date = punch_ist.date()

    if is_overnight(shift_start, shift_end):
        punch_time = (punch_ist.hour, punch_ist.minute, punch_ist.second)

        if punch_time < (*_hhmm(shift_end), 0):
            attendance_date -= timedelta(days=1)

    return attendance_date.isoformat()


def compute_late_minutes(
    punch_ist: datetime,
    attendance_date: str,
    shift_start: str,
) -> int:
    """
    How many minutes late is this punch?

    We give everyone a 10-minute grace period. If they arrive within that
    window they're considered on time and we return 0.
    """
    shift_start_ist = ist_on(attendance_date, shift_start)
    elapsed_seconds = (punch_ist - shift_start_ist).total_seconds()

    if elapsed_seconds <= GRACE_SECONDS:
        return 0

    return int(elapsed_seconds // 60)



# Response serialisers

def attendance_out(doc: dict) -> dict:
    """Shape an attendance log document into the public API response."""
    def to_ms(value):
        if value is None:
            return None
        return utc_naive_to_ms(value)

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


# database setup

load_dotenv(override=False)

MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB = os.getenv("MONGO_DB")

if not MONGO_URI or not MONGO_DB:
    raise RuntimeError("MONGO_URI and MONGO_DB must be configured.")

client = MongoClient(
    MONGO_URI,
    serverSelectionTimeoutMS=3000,
)

db = client[MONGO_DB]

employees = db["employees"]
attendance_logs = db["attendance_logs"]


def create_indexes():
    """Set up all the indexes the API relies on. Safe to call on every startup."""

    # Unique employee codes — prevents accidental duplicates
    employees.create_index(
        [("emp_code", ASCENDING)],
        unique=True,
        name="ux_employees_emp_code",
    )

    # Speeds up department-filtered employee listings
    employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)],
        name="ix_employees_department_emp_code",
    )

    # Used for monthly headcount and joining-date range queries
    employees.create_index(
        [("joined_on", ASCENDING)],
        name="ix_employees_joined_on",
    )

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
        [
            ("status", ASCENDING),
            ("date", DESCENDING),
            ("emp_code", ASCENDING),
        ],
        name="ix_logs_status_date_emp_code",
    )

    # Powers the late-comer leaderboard
    attendance_logs.create_index(
        [("late_minutes", DESCENDING), ("emp_code", ASCENDING)],
        name="ix_logs_late_minutes_emp_code",
    )


# Request models
#employee_in model
class EmployeeIn(BaseModel):

    emp_code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    email: str = Field(
        min_length=3,
        max_length=320,
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
    )
    department: str = Field(min_length=1, max_length=200)
    shift_start: str = Field(
        default="09:30",
        pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$",
    )
    shift_end: str = Field(
        default="18:30",
        pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$",
    )
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

#punch_in model
class PunchInIn(BaseModel):

    emp_code: str = Field(min_length=1, max_length=64)
    punched_at: StrictInt | None = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, value):
        if value is not None and not (MS_MIN <= value <= MS_MAX):
            raise ValueError(
                "punched_at must be an integer epoch timestamp in milliseconds"
            )
        return value

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



# routes

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
        raise HTTPException(
            status_code=409,
            detail=f"emp_code already exists: {body.emp_code}",
        )

    return employee_out(employee)


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
        raise HTTPException(
            status_code=404,
            detail=f"Unknown employee: {body.emp_code}",
        )

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
        raise HTTPException(
            status_code=409,
            detail="Already punched in for this attendance date",
        )

    return attendance_out(record)
