
import os

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pymongo import MongoClient
from pymongo.errors import PyMongoError

# Load local environment variables.
# Environment variables already provided by the system take priority.
load_dotenv(override=False)

MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB = os.getenv("MONGO_DB")

if not MONGO_URI or not MONGO_DB:
    raise RuntimeError(
        "MONGO_URI and MONGO_DB must be configured."
    )

# Create the MongoDB client.
client = MongoClient(
    MONGO_URI,
    serverSelectionTimeoutMS=3000,
)

db = client[MONGO_DB]

# Create the FastAPI application.
app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="1.0.0",
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
