
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import PyMongoError
from datetime import date, datetime, timezone

from pydantic import BaseModel, Field, field_validator
from pymongo.errors import DuplicateKeyError

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
    """Create the indexes required by the API."""

    # Each employee code must be unique.
    employees.create_index(
        [("emp_code", ASCENDING)],
        unique=True,
        name="ux_employees_emp_code",
    )

    # Department filtering and employee-code sorting.
    employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)],
        name="ix_employees_department_emp_code",
    )

    # Monthly headcount and joining-date filters.
    employees.create_index(
        [("joined_on", ASCENDING)],
        name="ix_employees_joined_on",
    )

    # One attendance record per employee per attendance date.
    attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="ux_logs_emp_code_date",
    )

    # Attendance date filtering and default sorting.
    attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="ix_logs_date_desc_emp_code",
    )

    # Status-based attendance filtering.
    attendance_logs.create_index(
        [
            ("status", ASCENDING),
            ("date", DESCENDING),
            ("emp_code", ASCENDING),
        ],
        name="ix_logs_status_date_emp_code",
    )

    # Late-comer leaderboard queries.
    attendance_logs.create_index(
        [("late_minutes", DESCENDING), ("emp_code", ASCENDING)],
        name="ix_logs_late_minutes_emp_code",
    )

 
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
            raise ValueError(
                "joined_on must be a valid YYYY-MM-DD date"
            )

        if parsed.isoformat() != value:
            raise ValueError("joined_on must use YYYY-MM-DD format")

        return value
   


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Verify the database connection and create indexes at startup.
    client.admin.command("ping")
    create_indexes()

    yield

    client.close()


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/")
def root():
    return {
        "message": "Employee Attendance API is running"
    }


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(
            status_code=503,
            detail="Database unavailable",
        )




def employee_out(doc: dict) -> dict:
    """Convert a MongoDB employee document into API response JSON."""
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


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    employee = body.model_dump()

    employee["created_at"] = datetime.now(
        timezone.utc
    ).replace(microsecond=0, tzinfo=None)

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
    department: str | None = Query(
        default=None,
        min_length=1,
        max_length=200,
    ),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    query = {}

    if department is not None:
        query["department"] = department

    # Match the index to the query shape.
    index_name = (
        "ix_employees_department_emp_code"
        if department is not None
        else "ux_employees_emp_code"
    )

    total = employees.count_documents(
        query,
        hint=index_name,
    )

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

