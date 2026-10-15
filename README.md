# Employee Attendance & Analytics API

A FastAPI application that tracks employee attendance and exposes analytics endpoints, backed by MongoDB.

## Requirements

- Python 3.11+
- MongoDB 6.0+ (7.0 recommended — Atlas free tier M0 works)

## Setup

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Create a `.env` file in the project root (never commit this):

```
MONGO_URI=mongodb+srv://<user>:<password>@<cluster>.mongodb.net/
MONGO_DB=employee_attendance
```

## Run

```bash
uvicorn app.main:app --port 8000 --reload
```

API docs available at `http://127.0.0.1:8000/docs`.

## Seed sample data (optional)

```bash
python sample_seed.py
```

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Readiness probe |
| POST | `/employees` | Create employee |
| GET | `/employees` | List employees |
| POST | `/attendance/punch-in` | Record punch-in |
| POST | `/attendance/punch-out` | Record punch-out |
| GET | `/attendance` | List attendance records |
| PATCH | `/attendance/{emp_code}/{date}` | Regularize a record |
| GET | `/analytics/employees/{emp_code}/monthly` | Monthly summary per employee |
| GET | `/analytics/departments/summary` | Monthly summary per department |
| GET | `/analytics/leaderboard/late` | Late-comers leaderboard |
| GET | `/analytics/departments/{department}/trend` | Daily attendance trend |
| GET | `/admin/explain/{endpoint}` | MongoDB explain for any endpoint |
