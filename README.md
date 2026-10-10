# Employee Attendance & Analytics API

A FastAPI + MongoDB backend service implementing employee attendance tracking, audit regularizations, and reporting analytics built with MongoDB aggregation pipelines.

## Requirements

- Python 3.11+
- MongoDB 6.0+ (MongoDB 7.0 recommended; Atlas or local instance)

## Setup & Running

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
# On Windows:
.venv\Scripts\activate
# On Linux / macOS:
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment variables (optional, defaults to mongodb://localhost:27017 and attendance_db)
# Real environment variables take priority over local .env
export MONGO_URI="mongodb://localhost:27017"
export MONGO_DB="attendance_db"

# 4. Seed sample data (optional)
python sample_seed.py

# 5. Start the API server
uvicorn app.main:app --port 8000 --reload
```

The service will be accessible at `http://127.0.0.1:8000`. Interactive OpenAPI documentation is available at `http://127.0.0.1:8000/docs`.

## Key Features & Guarantees

- **Startup Index Initialization:** All database indexes (unique keys, composite queries, sort orders) are created idempotently on startup.
- **Strict Business Rules (R1 - R10):**
  - **R1:** Proper IST handling, overnight shift date alignment, and instant truncation to whole seconds.
  - **R2:** Strict grace period calculation (late only if > 10:00 elapsed from shift start).
  - **R3:** Overtime floor minutes counted only when >= 30 minutes past shift end.
  - **R4 - R5:** Work hours rounded half-up via `Decimal`; half-day threshold evaluated at `< 4.50` hours.
  - **R6 - R9:** Working days (Mon-Fri) counted from join date; zero-log employees preserved in headcount; standard competition ranking for ties.
- **Race Condition & Concurrency Safety:**
  - Duplicate punch-ins return exactly one `201` and subsequent concurrent attempts receive `409` via unique compound index on `(emp_code, date)`.
  - Atomic `find_one_and_update` ensures concurrent punch-outs yield exactly one `200` and `409` for others.
  - Optimistic concurrency control on regularizations (`PATCH`) preserves full audit history without lost updates.
- **Database-Native Aggregations:** Analytics endpoints utilize MongoDB aggregation pipelines (`$densify`, `$setWindowFields`, `$lookup`, `$reduce`) without in-memory Python looping.
- **Query Performance & Explainability:** Main queries are verified via `GET /admin/explain/{endpoint}` with `executionStats` to confirm index utilization (`IXSCAN`).

## Included Documentation

- `REVIEW.md`: Line-by-line review of defects identified in the starter code, their symptoms, and the corresponding fixes.
- `DECISIONS.md`: Concise explanation of architectural decisions, indexes, concurrency handling, tie resolution, and scaling strategies.
